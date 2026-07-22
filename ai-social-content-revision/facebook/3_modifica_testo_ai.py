#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 3: Modifica testo con AI
Legge i post dall'output della fase meccanica, applica la riscrittura condizionale
tramite un modello di AI e salva i post pronti alla pubblicazione.

Uso:
    python 3_modifica_testo_ai.py \
      --input output/modificati/posts_con_stato_meccanico.json \
      --start-from 1 \
      --checkpoint-interval 5 \
      [--no-ai]
"""

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# ==========================
# CONFIGURAZIONE (DA .env)
# ==========================


def load_env_from_file(env_file: str = ".env") -> None:
    """
    Carica chiavi=valore da un file .env locale impostandole nell'ambiente.
    """
    env_path = Path(__file__).parent / env_file
    if not env_path.exists():
        return

    current_key: Optional[str] = None
    current_lines: List[str] = []
    quote_char: Optional[str] = None

    for raw_line in env_path.read_text().splitlines():
        if current_key:
            current_lines.append(raw_line)
            if quote_char and raw_line.rstrip().endswith(quote_char):
                value = "\n".join(current_lines)
                if value.startswith(quote_char):
                    value = value[1:]
                if value.endswith(quote_char):
                    value = value[:-1]
                os.environ[current_key] = value
                current_key = None
                current_lines = []
                quote_char = None
            continue

        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue

        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.lstrip()

        if (value.startswith('"') or value.startswith("'")) and not value.rstrip().endswith(value[0]):
            current_key = key
            quote_char = value[0]
            current_lines = [value]
            continue

        value = value.strip().strip('"').strip("'")
        os.environ[key] = value

    if current_key and current_lines:
        value = "\n".join(current_lines)
        if quote_char and value.startswith(quote_char):
            value = value[1:]
        os.environ[current_key] = value


def parse_json_env(key: str, default):
    """
    Prova a leggere JSON da env; in caso di errore o assenza restituisce default.
    """
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[WARN] {key} non è JSON valido, uso il default.")
        return default


def parse_word_pairs_env(key: str, default: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    Legge una lista di coppie da env (JSON). Formati supportati:
      - [["topicA","topicB"], ["concettoA","concettoB"], ...]
      - [{"a":"topicA","b":"topicB"}, ...]
    """
    raw = os.getenv(key)
    if not raw:
        return default

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[WARN] {key} non è JSON valido, uso il default.")
        return default

    pairs: List[Tuple[str, str]] = []
    if isinstance(loaded, list):
        for item in loaded:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict) and "a" in item and "b" in item:
                pairs.append((str(item["a"]), str(item["b"])))

    return pairs or default


def env_bool(key: str, default: bool = False) -> bool:
    """
    Legge un booleano da env con fallback.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ["1", "true", "yes", "si", "on"]


load_env_from_file()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON su file in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


class AuditLogger:
    """
    Logger JSONL (una riga JSON per post) per capire:
    - quali trigger hanno attivato l'AI
    - se l'AI è stata chiamata
    - perché un post è rimasto invariato
    """

    def __init__(self, path: Path, include_text: bool = False) -> None:
        self.path = path
        self.include_text = include_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", encoding="utf-8")

    def log(self, record: Dict[str, Any]) -> None:
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


# Config AI e sostituzioni
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4.1-mini")
AI_EXTRA_CONTEXT = os.getenv("AI_EXTRA_CONTEXT", "")

REPLACEMENTS: Dict[str, str] = parse_json_env(
    "REPLACEMENTS",
    {
        "catalogo": "brochure",
        "link in bio": "link nel primo commento",
    },
)
AI_KEYWORDS: List[str] = parse_json_env("AI_KEYWORDS", [])
PROTECTED_PHRASES: List[str] = parse_json_env("PROTECTED_PHRASES", ["frase-preservata"])
DEFAULT_AI_WORD_PAIRS: List[Tuple[str, str]] = [
    ("concettoA", "concettoB"),
    ("topicA", "topicB"),
    ("concettoB", "concettoA"),
    ("concettoB", "topicA"),
    ("concettoA", "concettoC"),
    ("topicA", "topicC"),
    ("concettoD", "concettoB"),
    ("concettoB", "concettoD"),
]
AI_WORD_PAIRS: List[Tuple[str, str]] = parse_word_pairs_env("AI_WORD_PAIRS", DEFAULT_AI_WORD_PAIRS)

# Cartelle di gestione
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "ai"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
MODIFICATI_SUBDIR = os.getenv("MODIFICATI_SUBDIR", "modificati")
PRONTI_SUBDIR = os.getenv("PRONTI_SUBDIR", "pronti_per_pubblicazione")

# Parametri di gestione elaborazione
DEFAULT_START_FROM = int(os.getenv("START_FROM_POST", "1"))
DEFAULT_CHECKPOINT_INTERVAL = int(os.getenv("CHECKPOINT_INTERVAL", "5"))
DEFAULT_ENABLE_AI = env_bool("ENABLE_AI", True)

# Percorsi di input/output
DEFAULT_INPUT_FILE = os.getenv(
    "DEFAULT_INPUT_FILE",
    f"{OUTPUT_DIR}/{MODIFICATI_SUBDIR}/posts_con_stato_meccanico.json",
)
DEFAULT_PRONTI_FILENAME = "pronti.json"


# ==========================
# CHECKPOINTING
# ==========================


def get_checkpoint_path(input_path: str) -> Path:
    """
    Ritorna il percorso del file di checkpoint nella cartella dedicata.
    """
    input_file = Path(input_path)
    base_dir = Path(__file__).parent
    checkpoint_dir = base_dir / CHECKPOINT_DIR / CHECKPOINT_SUBDIR
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_name = f"checkpoint_ai_{input_file.stem}.json"
    return checkpoint_dir / checkpoint_name


def save_checkpoint(input_path: str, last_processed: int, results: List[Dict[str, Any]]) -> None:
    """
    Salva un checkpoint con l'ultimo post elaborato e i risultati parziali.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    checkpoint_data = {
        "last_processed": last_processed,
        "timestamp": dt.datetime.now().isoformat(),
        "results": results,
    }
    write_json_atomic(checkpoint_path, checkpoint_data)


def load_checkpoint(input_path: str) -> Optional[Dict[str, Any]]:
    """
    Carica un checkpoint esistente, se presente.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    if not checkpoint_path.exists():
        return None

    try:
        data = json.loads(checkpoint_path.read_text())
        return data
    except (json.JSONDecodeError, KeyError):
        print("[WARN] Checkpoint corrotto, verrà ignorato.")
        return None


def delete_checkpoint(input_path: str) -> None:
    """
    Elimina il file di checkpoint.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"[INFO] Checkpoint eliminato: {checkpoint_path.name}")


# ==========================
# FUNZIONI CORE
# ==========================


def load_posts(input_path: str) -> List[Dict[str, Any]]:
    """
    Carica i post da un file JSON intermedio (fase meccanica).
    Risolve percorsi relativi rispetto alla cartella dello script se necessario.
    """
    path = Path(input_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    if not path.exists():
        print(f"[ERRORE] File non trovato: {path}")
        print("[INFO] Esegui prima lo script 2_modifica_testo_meccanica.py per generare il file intermedio.")
        sys.exit(1)

    data = json.loads(path.read_text())
    posts = data.get("posts") or data.get("results", [])
    print(f"[INFO] Caricati {len(posts)} post da {path.name}")
    return posts


def normalize_text_whitespace(s: str) -> str:
    """
    Collassa sequenze di whitespace e rimuove spazi iniziali/finali per
    confronti che ignorino differenze puramente di formattazione.
    """
    if not s:
        return ""
    import re

    return re.sub(r"\s+", " ", s).strip()


def needs_ai_rewrite(text: str, keywords: List[str]) -> bool:
    """
    Ritorna True se il testo contiene almeno una delle keywords (case-insensitive).
    """
    for k in keywords:
        pattern = r"\b" + re.escape(k) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def check_keywords_in_text(text: str, keywords: List[str]) -> List[str]:
    """
    Ritorna la lista di keywords ancora presenti nel testo (case-insensitive).
    """
    present: List[str] = []
    for k in keywords:
        pattern = r"\b" + re.escape(k) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            present.append(k)
    return present


def contains_word_pair(text: str, pairs: List[Tuple[str, str]]) -> bool:
    """
    True se il testo contiene entrambe le parole della coppia (match su intera parola, case-insensitive).
    """
    if not text:
        return False
    for first, second in pairs:
        p1 = r"\b" + re.escape(first) + r"\b"
        p2 = r"\b" + re.escape(second) + r"\b"
        if re.search(p1, text, flags=re.IGNORECASE) and re.search(p2, text, flags=re.IGNORECASE):
            return True
    return False


def find_triggered_pairs(text: str, pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    Ritorna le coppie (first, second) trovate nel testo.
    Una coppia è "triggerata" se entrambe le parole compaiono nel testo
    (match su intera parola, case-insensitive).
    """
    if not text:
        return []

    triggered: List[Tuple[str, str]] = []
    for first, second in pairs:
        p1 = r"\b" + re.escape(first) + r"\b"
        p2 = r"\b" + re.escape(second) + r"\b"
        if re.search(p1, text, flags=re.IGNORECASE) and re.search(p2, text, flags=re.IGNORECASE):
            triggered.append((first, second))
    return triggered


def protect_phrases(text: str, phrases: List[str]) -> Tuple[str, Dict[str, str]]:
    """
    Sostituisce le frasi protette con placeholder per impedirne la modifica; restituisce
    testo con placeholder e mappa per il ripristino.
    """
    replacements: Dict[str, str] = {}
    result = text
    counter = 0

    for phrase in phrases:
        if not phrase:
            continue
        pattern = re.compile(re.escape(phrase), flags=re.IGNORECASE)

        def _repl(match: re.Match) -> str:
            nonlocal counter
            token = f"__PROTECTED_PHRASE_{counter}__"
            replacements[token] = match.group(0)
            counter += 1
            return token

        result = pattern.sub(_repl, result)

    return result, replacements


def restore_protected_phrases(text: str, replacements: Dict[str, str]) -> str:
    """
    Ripristina i placeholder con le frasi protette originali.
    """
    result = text
    for token, original in replacements.items():
        result = result.replace(token, original)
    return result


def ensure_protected_presence(original: str, candidate: str, phrases: List[str]) -> str:
    """
    Se il candidato ha un conteggio diverso (mancato o aggiunto) delle frasi protette rispetto
    all'originale, restituisce l'originale per evitare rimozioni, modifiche o aggiunte.
    """
    for phrase in phrases:
        if not phrase:
            continue
        pattern = re.compile(re.escape(phrase), flags=re.IGNORECASE)
        orig_count = len(pattern.findall(original or ""))
        new_count = len(pattern.findall(candidate or ""))
        # Vietiamo sia rimozioni sia aggiunte.
        if orig_count == 0 and new_count > 0:
            return original
        if orig_count > 0 and new_count != orig_count:
            return original
    return candidate


def find_protected_phrase_mismatches(
    original: str, candidate: str, phrases: List[str]
) -> List[Dict[str, Any]]:
    """
    Ritorna una lista di mismatch sulle frasi protette (conteggio diverso o aggiunte).
    Utile per log/audit: quando l'AI propone testo che introduce una frase protetta
    (es. 'esempio-frase-4') che non era presente, il risultato viene scartato.
    """
    mismatches: List[Dict[str, Any]] = []
    for phrase in phrases:
        if not phrase:
            continue
        pattern = re.compile(re.escape(phrase), flags=re.IGNORECASE)
        orig_count = len(pattern.findall(original or ""))
        new_count = len(pattern.findall(candidate or ""))
        if orig_count == 0 and new_count > 0:
            mismatches.append(
                {
                    "phrase": phrase,
                    "original_count": orig_count,
                    "candidate_count": new_count,
                    "type": "added",
                }
            )
        elif orig_count > 0 and new_count != orig_count:
            mismatches.append(
                {
                    "phrase": phrase,
                    "original_count": orig_count,
                    "candidate_count": new_count,
                    "type": "count_changed",
                }
            )
    return mismatches


def unwrap_triple_quotes(text: str) -> str:
    """
    Rimuove eventuali triple quote che racchiudono tutto il testo.
    """
    if not text:
        return text
    m = re.match(r'^\s*"{3}(.*)"{3}\s*$', text, flags=re.DOTALL)
    if m:
        return m.group(1)
    m = re.match(r"^\s*'{3}(.*)'{3}\s*$", text, flags=re.DOTALL)
    if m:
        return m.group(1)
    return text


def rewrite_with_ai(
    original_text: str,
    extra_context: Optional[str] = None,
    replacements: Optional[Dict[str, str]] = None,
    keywords_to_remove: Optional[List[str]] = None,
    triggered_word_pairs: Optional[List[Tuple[str, str]]] = None,
    protected_phrases: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Riscrive il testo usando un modello di AI.
    keywords_to_remove: lista di parole che hanno attivato la revisione (usate solo come focus, non come divieto).
    protected_phrases: frasi da non modificare o tradurre (case-insensitive).
    """
    if not AI_API_KEY:
        raise RuntimeError(
            "AI_API_KEY non impostata, ma è stata richiesta la riscrittura AI."
        )

    debug: Dict[str, Any] = {
        "model": AI_MODEL,
        "protected_mismatches": [],
        "reverted_due_to_protected_phrases": False,
    }

    replacements_prompt = ""
    if replacements:
        pairs = "\n".join([f"- '{old}' -> '{new}'" for old, new in replacements.items()])
        replacements_prompt = (
            "Applica queste sostituzioni in modo naturale e grammaticalmente corretto "
            "(adatta genere, numero, preposizioni, tempi verbali) e riformula se serve "
            "per mantenere fluidità:\n"
            f"{pairs}\n\n"
        )

    keywords_prompt = ""
    if keywords_to_remove:
        keywords_list = ", ".join([f"'{k}'" for k in keywords_to_remove])
        keywords_prompt = (
            f"Parole chiave che hanno attivato la revisione: {keywords_list}. "
            "Usale come guida per individuare le frasi da adeguare secondo il contesto, ma applica modifiche solo se richiesto dal contesto stesso.\n\n"
        )

    pairs_prompt = ""
    if triggered_word_pairs:
        pairs_list = ", ".join([f"('{a}', '{b}')" for a, b in triggered_word_pairs])
        pairs_prompt = (
            f"Coppie di parole che hanno attivato la revisione: {pairs_list}. "
            "Se nel testo trovi una frase in cui le due parole di una coppia co-occorrono nella stessa frase "
            "(anche non adiacenti), riscrivi SOLO quella frase per rimuovere/evitare l'associazione tra i concetti "
            "(es. evitare formulazioni tipo 'concetto-A come concetto-B', 'topic-B topic-A', 'concetto-B concetto-A', "
            "'concetto-A come concetto-C'). "
            "La frase riscritta deve risultare diversa dall'originale. "
            "Dopo la riscrittura, evita che entrambe le parole della coppia compaiano nella stessa frase (puoi usare una perifrasi). "
            "Non modificare frasi che contengono solo una delle due parole.\n\n"
        )

    protected_list = [p for p in (protected_phrases or []) if p]
    protected_prompt = ""
    if protected_list:
        protected_str = ", ".join([f"'{p}'" for p in protected_list])
        protected_prompt = (
            f"Non modificare, tradurre o rimuovere queste espressioni protette: {protected_str}. "
            "Devono rimanere identiche se già presenti nel testo originale. "
            "Non introdurre nuove occorrenze di queste espressioni se non erano già presenti nel testo originale.\n\n"
        )

    text_for_ai = original_text
    protected_map: Dict[str, str] = {}
    if protected_list:
        text_for_ai, protected_map = protect_phrases(original_text, protected_list)

    prompt = (
        "Applica le regole del contesto: se servono modifiche, riscrivi le frasi interessate; se non servono modifiche, lascia il testo IDENTICO. "
        "Non aggiungere nuove frasi o informazioni. Mantieni formattazione, a capo, punteggiatura, emoji, hashtag, link e ordine. "
        "Mantieni SEMPRE la stessa lingua di ogni frase (non tradurre nulla, nemmeno parti miste). "
        "Se il post è misto italiano/inglese, ogni frase rimane nella lingua originale mentre applichi le modifiche. "
        "Modifica solo le frasi che contengono le parole chiave attivate, "
        "oppure le frasi in cui co-occorrono le parole di una coppia attivata, "
        "oppure le occorrenze da sostituire; tutto il resto deve rimanere IDENTICO. "
        "Non tradurre termini/slogan/hashtag in inglese inseriti in frasi italiane (es. 'topicA', brand, acronimi): devono restare identici.\n\n"
        f"{protected_prompt}"
        f"{keywords_prompt}"
        f"{pairs_prompt}"
        f"{replacements_prompt}"
        f"Contesto aggiuntivo: {extra_context or ''}\n\n"
        f"Testo originale:\n\"\"\"{text_for_ai}\"\"\""
    )

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "Sei un copywriter professionista."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code != 200:
        print("[ERRORE AI] Risposta non OK:", response.status_code, response.text)
        raise RuntimeError("Errore chiamata AI")

    data = response.json()
    try:
        ai_text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        print("[ERRORE AI] Formato risposta inatteso:", data)
        raise RuntimeError("Formato risposta AI inatteso")

    ai_text = unwrap_triple_quotes(ai_text)
    ai_text = restore_protected_phrases(ai_text, protected_map) if protected_map else ai_text
    mismatches = find_protected_phrase_mismatches(original_text, ai_text, protected_list)
    if mismatches:
        debug["protected_mismatches"] = mismatches
        debug["reverted_due_to_protected_phrases"] = True
        return original_text, debug

    debug["protected_mismatches"] = []
    debug["reverted_due_to_protected_phrases"] = False
    return ai_text, debug


def passthrough_post(
    post: Dict[str, Any],
    post_number: int,
    post_id: Optional[str],
    created_time: str,
    original_message: str,
    base_text: str,
) -> Dict[str, Any]:
    """
    Restituisce un post marcato come invariato per la fase AI.
    """
    entry = {
        **post,
        "post_number": post_number,
        "post_id": post_id,
        "created_time": created_time,
        "original_message": original_message,
        "status_meccanico": post.get("status_meccanico", "invariato"),
        "status_ai": "invariato",
        "used_ai": False,
        "ai_error": False,
        "ai_error_message": "",
        "parte_modificata": "",
    }

    # Mantieni il testo solo se la fase meccanica aveva modificato il contenuto.
    if post.get("status_meccanico") == "modificato":
        entry["message_meccanico"] = base_text
        entry["modified_message"] = base_text
    else:
        entry.pop("message_meccanico", None)
        entry.pop("modified_message", None)

    return entry


def process_posts(
    posts: List[Dict[str, Any]],
    use_ai: bool = True,
    start_from: int = 1,
    input_path: str = "",
    checkpoint_interval: int = 5,
    existing_results: Optional[List[Dict[str, Any]]] = None,
    audit_logger: Optional[AuditLogger] = None,
) -> List[Dict[str, Any]]:
    """
    Processa i post applicando solo la logica AI (le sostituzioni meccaniche sono già state applicate).
    """
    results: List[Dict[str, Any]] = list(existing_results) if existing_results else []
    already_processed = len(results)

    def estrai_parte_modificata(original: str, modified: str) -> str:
        """
        Estrae le parti modificate in modo leggibile.
        Strategia a due livelli:
        1) Diff per frasi/righe: identifica le frasi cambiate (gestisce \n).
        2) Per ogni frase cambiata, diff a livello di parole per mostrare solo le parole/cluster inseriti o sostituiti.
        """
        if original == modified:
            return ""

        import re

        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)

        line_sm = difflib.SequenceMatcher(a=orig_lines, b=mod_lines)
        changed_fragments: List[str] = []

        def word_tokens(s: str) -> List[str]:
            return re.split(r"(\s+|[\.,;:!\?\-—])", s)

        for tag, i1, i2, j1, j2 in line_sm.get_opcodes():
            if tag in ("replace", "insert"):
                for line in mod_lines[j1:j2]:
                    ref_line = orig_lines[i1] if i1 < len(orig_lines) else ""
                    o_tokens = word_tokens(ref_line)
                    m_tokens = word_tokens(line)
                    w_sm = difflib.SequenceMatcher(a=o_tokens, b=m_tokens)
                    parts: List[str] = []
                    for wtag, wi1, wi2, wj1, wj2 in w_sm.get_opcodes():
                        if wtag in ("replace", "insert"):
                            left = wj1
                            right = wj2
                            if left > 0 and m_tokens[left - 1] and m_tokens[left - 1].isspace():
                                left -= 1
                            if right < len(m_tokens) and m_tokens[right] and m_tokens[right].isspace():
                                right += 1
                            parts.append("".join(m_tokens[left:right]))
                    fragment = "".join(parts).strip()
                    if fragment:
                        changed_fragments.append(fragment)

        return "\n".join(changed_fragments)

    for index, post in enumerate(posts, start=1):
        if index <= already_processed:
            continue

        post_number = post.get("post_number", index)
        post_id = post.get("post_id") or post.get("id")
        created_time = post.get("created_time", "N/A")
        original_message = post.get("original_message") or post.get("message") or ""
        base_text = post.get("modified_message") or post.get("message_meccanico") or original_message

        if post_number < start_from:
            if audit_logger:
                audit_logger.log(
                    {
                        "post_number": post_number,
                        "post_id": post_id,
                        "created_time": created_time,
                        "action": "skipped_before_start_from",
                        "start_from": start_from,
                    }
                )
            results.append(passthrough_post(post, post_number, post_id, created_time, original_message, base_text))
            continue

        if not base_text:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) senza testo dopo la fase meccanica.")
            if audit_logger:
                audit_logger.log(
                    {
                        "post_number": post_number,
                        "post_id": post_id,
                        "created_time": created_time,
                        "action": "skipped_no_text",
                        "status_meccanico": post.get("status_meccanico", "invariato"),
                    }
                )
            results.append(passthrough_post(post, post_number, post_id, created_time, original_message, base_text))
            continue

        print(f"\n[POST #{post_number}] {post_id} - {created_time}")
        print("Testo dopo fase meccanica:")
        print(base_text)
        print("-" * 60)

        final_text = base_text
        used_ai_for_this_post = False
        ai_error = False
        ai_error_message = ""
        ai_debug: Dict[str, Any] = {}

        keyword_filter_active = bool(AI_KEYWORDS)
        triggered_keywords = check_keywords_in_text(base_text, AI_KEYWORDS) if keyword_filter_active else []
        keywords_triggered = bool(triggered_keywords)
        replacements_present = bool(
            REPLACEMENTS and any(old.lower() in base_text.lower() for old in REPLACEMENTS.keys())
        )
        triggered_pairs = find_triggered_pairs(base_text, AI_WORD_PAIRS)
        pair_triggered = bool(triggered_pairs)
        triggers_present = keywords_triggered or replacements_present or pair_triggered
        should_use_ai = use_ai and triggers_present

        if should_use_ai:
            if keywords_triggered:
                print("[INFO] Il post soddisfa i criteri per la riscrittura AI.")
            if pair_triggered:
                pairs_str = ", ".join([f"{a}+{b}" for a, b in triggered_pairs])
                print(f"[INFO] AI attiva per coppia parole configurata: {pairs_str}")
            if replacements_present:
                print("[INFO] AI attiva per applicare le sostituzioni richieste.")
            try:
                final_text, ai_debug = rewrite_with_ai(
                    base_text,
                    AI_EXTRA_CONTEXT,
                    REPLACEMENTS if replacements_present else None,
                    triggered_keywords if triggered_keywords else None,
                    triggered_pairs if triggered_pairs else None,
                    PROTECTED_PHRASES,
                )
                used_ai_for_this_post = True
            except RuntimeError as e:
                print("[WARN] Errore AI, mantengo il testo della fase meccanica:", e)
                ai_error = True
                ai_error_message = str(e)
                final_text = base_text
                ai_debug = {"error": ai_error_message}
        else:
            reasons = []
            if not use_ai:
                reasons.append("AI disabilitata")
            if not triggers_present:
                reasons.append("nessun trigger (keyword/coppia/sostituzioni)")
            reason_msg = " e ".join(reasons) if reasons else "condizioni non soddisfatte"
            print(f"[INFO] Il post non richiede AI ({reason_msg}).")

        if (
            normalize_text_whitespace(final_text) != normalize_text_whitespace(base_text)
            and not triggers_present
        ):
            print("[INFO] Modifica AI ignorata: nessun trigger (keyword/coppia/sostituzione) presente.")
            final_text = base_text

        if normalize_text_whitespace(final_text) != normalize_text_whitespace(base_text):
            status_ai = "modificato"
            parte_modificata = estrai_parte_modificata(base_text, final_text)
        else:
            status_ai = "invariato"
            parte_modificata = ""

        print("Nuovo testo proposto:")
        print(final_text)
        print("-" * 60)

        result_entry = {
            **post,
            "post_number": post_number,
            "post_id": post_id,
            "created_time": created_time,
            "original_message": original_message,
            "message_meccanico": base_text,
            "modified_message": final_text,
            "status_meccanico": post.get("status_meccanico", "invariato"),
            "status_ai": status_ai,
            "used_ai": used_ai_for_this_post and not ai_error,
            "ai_error": ai_error,
            "ai_error_message": ai_error_message,
            "parte_modificata": parte_modificata,
        }

        # Se nessuna fase ha modificato il testo, evita campi duplicati.
        keep_meccanico_text = result_entry.get("status_meccanico") == "modificato"
        keep_ai_text = result_entry.get("status_ai") == "modificato"
        if not keep_meccanico_text and not keep_ai_text:
            result_entry.pop("message_meccanico", None)
            result_entry.pop("modified_message", None)

        results.append(result_entry)

        if audit_logger:
            audit_record = {
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "status_meccanico": result_entry.get("status_meccanico", "invariato"),
                "status_ai": status_ai,
                "triggers": {
                    "keywords": triggered_keywords,
                    "pairs": triggered_pairs,
                    "replacements_present": replacements_present,
                },
                "should_use_ai": should_use_ai,
                "used_ai": used_ai_for_this_post and not ai_error,
                "ai_error": ai_error,
                "ai_error_message": ai_error_message,
                "reverted_due_to_protected_phrases": ai_debug.get("reverted_due_to_protected_phrases", False),
                "protected_mismatches": ai_debug.get("protected_mismatches", []),
            }
            if audit_logger.include_text:
                audit_record["message_meccanico"] = base_text
                audit_record["modified_message"] = final_text
            audit_logger.log(audit_record)

        if input_path and post_number % checkpoint_interval == 0:
            save_checkpoint(input_path, post_number, results)
            print(f"[CHECKPOINT] Salvato progresso al post #{post_number}")

    total_ai_used = sum(1 for r in results if r.get("used_ai"))
    total_ai_modified = sum(1 for r in results if r.get("status_ai") == "modificato")
    total_ai_failed = sum(1 for r in results if r.get("ai_error"))

    print("\n========== RIEPILOGO AI ==========")
    print(f"Post totali processati: {len(results)}")
    print(f"Post modificati dall'AI: {total_ai_modified}")
    print(f"Post passati all'AI: {total_ai_used}")
    print(f"Post con errore AI: {total_ai_failed}")
    print("================================")

    return results


def save_results(all_posts: List[Dict[str, Any]], output_path: Optional[str] = None) -> Path:
    """
    Salva solo i post effettivamente modificati in un file JSON
    pronto per la pubblicazione (output/pronti_per_pubblicazione).
    """
    modified_posts = [
        p for p in all_posts
        if p.get("status_meccanico") == "modificato" or p.get("status_ai") == "modificato"
    ]

    total_posts = len(all_posts)
    total_meccanici = sum(1 for r in all_posts if r.get("status_meccanico") == "modificato")
    total_ai = sum(1 for r in all_posts if r.get("status_ai") == "modificato")

    base_dir = Path(__file__).parent
    output_dir = base_dir / OUTPUT_DIR / PRONTI_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = Path(output_path) if output_path else Path(DEFAULT_PRONTI_FILENAME)
    if not out_path.is_absolute() and str(out_path.parent) == ".":
        out_path = output_dir / out_path.name
    elif not out_path.is_absolute():
        out_path = (base_dir / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "summary": {
            "total_posts_processed": total_posts,
            "total_modificati_meccanici": total_meccanici,
            "total_modificati_ai": total_ai,
            "total_pronti_per_pubblicazione": len(modified_posts),
        },
        "posts": modified_posts,
    }

    write_json_atomic(out_path, payload)
    print(f"\n[INFO] Creato file per la pubblicazione con {len(modified_posts)} post modificati.")
    print(f"[INFO] Risultati salvati in: {out_path.resolve()}")
    return out_path


def _removed_fragments(original: str, modified: str) -> str:
    """
    Ritorna una stringa con le parti rimosse tra originale e modificato.
    Prima controlla in modo esplicito gli hashtag tolti, per evitare falsi positivi
    dovuti al diff su blocchi contigui; se non trova hashtag rimossi,
    ripiega sul diff testuale come fallback.
    """
    hashtag_re = re.compile(r"#\w[\w-]*", flags=re.UNICODE)
    orig_tags = hashtag_re.findall(original or "")
    mod_tags_lower = {t.lower() for t in hashtag_re.findall(modified or "")}

    removed_tags: List[str] = []
    for tag in orig_tags:
        if tag.lower() not in mod_tags_lower and tag not in removed_tags:
            removed_tags.append(tag)

    if removed_tags:
        return " ".join(removed_tags)

    sm = difflib.SequenceMatcher(a=original, b=modified)
    removed = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            frag = original[i1:i2].strip()
            if frag:
                removed.append(frag)
        elif tag == "replace":
            frag = original[i1:i2].strip()
            if frag and modified[j1:j2].strip() != frag:
                removed.append(frag)
    seen = set()
    unique = []
    for f in removed:
        if f in seen:
            continue
        seen.add(f)
        unique.append(f)
    return " | ".join(unique)


def _wrap_paragraphs(text: str, width: int = 100) -> str:
    if not text:
        return ""
    parts: List[str] = []
    for para in text.splitlines():
        para = para.strip()
        if not para:
            parts.append("")
            continue
        parts.append(textwrap.fill(para, width=width))
    return "\n".join(parts)


def generate_resoconto(results: List[Dict[str, Any]], report_tag: Optional[str] = None) -> None:
    """
    Crea un resoconto delle modifiche (JSON + Markdown) in 'Resoconto modifiche mec-ai'.
    """
    modified_posts = [
        p for p in results if p.get("status_meccanico") == "modificato" or p.get("status_ai") == "modificato"
    ]
    out_dir = Path("Resoconto modifiche mec-ai")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_items = []
    for p in modified_posts:
        orig = p.get("original_message") or ""
        mech = p.get("message_meccanico") or orig
        mod = p.get("modified_message") or mech
        removed = _removed_fragments(orig, mech) if p.get("status_meccanico") == "modificato" else ""
        json_items.append(
            {
                "post_number": p.get("post_number"),
                "post_id": p.get("post_id"),
                "status_meccanico": p.get("status_meccanico"),
                "status_ai": p.get("status_ai"),
                "meccanico_rimosso": removed,
                "messaggio_originale": _wrap_paragraphs(orig),
                "messaggio_modificato": _wrap_paragraphs(mod),
            }
        )

    json_payload = {
        "total_posts": len(json_items),
        "posts": json_items,
    }
    suffix = f"_{report_tag}" if report_tag else ""
    json_path = out_dir / f"pronti_modifiche{suffix}.json"
    write_json_atomic(json_path, json_payload)

    md_lines: List[str] = []
    md_lines.append("# Resoconto modifiche meccaniche/AI")
    for item in json_items:
        md_lines.append("")
        md_lines.append(f"## Post {item.get('post_number')} ({item.get('post_id')})")
        md_lines.append(f"- status_meccanico: {item.get('status_meccanico')}")
        md_lines.append(f"- status_ai: {item.get('status_ai')}")
        if item.get("meccanico_rimosso"):
            md_lines.append(f"- meccanico_rimosso: {item.get('meccanico_rimosso')}")
        md_lines.append("")
        md_lines.append("**Originale**")
        md_lines.append(item.get("messaggio_originale", ""))
        md_lines.append("")
        md_lines.append("**Modificato**")
        md_lines.append(item.get("messaggio_modificato", ""))

    md_path = out_dir / f"pronti_modifiche{suffix}.md"
    md_path.write_text("\n".join(md_lines))
    print(f"[INFO] Resoconto modifiche scritto in {json_path} e {md_path}")


def generate_prima_dopo(results: List[Dict[str, Any]], report_tag: Optional[str] = None) -> None:
    """
    Salva un file JSON con solo prima/dopo dei post modificati (fase meccanica o AI).
    """
    modified_posts = [
        p for p in results if p.get("status_meccanico") == "modificato" or p.get("status_ai") == "modificato"
    ]
    out_dir = Path("Resoconto modifiche mec-ai")
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for p in modified_posts:
        orig = p.get("original_message") or ""
        mech = p.get("message_meccanico") or orig
        mod = p.get("modified_message") or mech
        entries.append(
            {
                "post_number": p.get("post_number"),
                "post_id": p.get("post_id"),
                "status_meccanico": p.get("status_meccanico"),
                "status_ai": p.get("status_ai"),
                "prima": orig,
                "dopo": mod,
            }
        )

    payload = {
        "total_posts": len(entries),
        "posts": entries,
    }
    suffix = f"_{report_tag}" if report_tag else ""
    out_path = out_dir / f"prima_dopo{suffix}.json"
    write_json_atomic(out_path, payload)
    print(f"[INFO] Report prima/dopo scritto in {out_path}")


def save_ai_failures(results: List[Dict[str, Any]]) -> None:
    """
    Salva in un file dedicato i post che hanno avuto errori AI.
    """
    failed = [
        {
            "post_number": r.get("post_number"),
            "post_id": r.get("post_id"),
            "created_time": r.get("created_time"),
            "status_meccanico": r.get("status_meccanico"),
            "original_message": r.get("original_message"),
            "message_meccanico": r.get("message_meccanico"),
            "modified_message": r.get("modified_message"),
            "ai_error_message": r.get("ai_error_message"),
        }
        for r in results
        if r.get("ai_error")
    ]

    if not failed:
        return

    base_dir = Path(__file__).parent
    output_dir = base_dir / OUTPUT_DIR / MODIFICATI_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"posts_ai_falliti_{ts}.json"
    payload = {
        "failed": len(failed),
        "timestamp": dt.datetime.now().isoformat(),
        "posts": failed,
    }
    write_json_atomic(path, payload)
    print(f"[INFO] Post con errori AI salvati in {path.resolve()}")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Modifica il testo dei post con AI partendo dall'output della fase meccanica."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_FILE,
        help=f"File JSON intermedio con stato_meccanico (default: {DEFAULT_INPUT_FILE})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Percorso del file pronto per la pubblicazione (default: output/pronti_per_pubblicazione/pronti.json)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disabilita la riscrittura AI; vengono riportati i testi meccanici.",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=DEFAULT_START_FROM,
        help=f"Numero del post (post_number) da cui iniziare l'elaborazione (default da .env: {DEFAULT_START_FROM}). Utile per riprendere dopo un blocco.",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=DEFAULT_CHECKPOINT_INTERVAL,
        help=f"Ogni quanti post salvare un checkpoint (default da .env: {DEFAULT_CHECKPOINT_INTERVAL}).",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="Percorso di un file JSONL dove salvare un audit per ogni post (trigger AI, status, ecc.).",
    )
    parser.add_argument(
        "--audit-include-text",
        action="store_true",
        help="Se impostato, include anche i testi (meccanico/modificato) nel file di audit (file più grande).",
    )
    parser.add_argument(
        "--only-post-id",
        default=None,
        help="Elabora solo il post con questo id (utile per debug mirato).",
    )
    parser.add_argument(
        "--only-post-number",
        type=int,
        default=None,
        help="Elabora solo il post con questo post_number (utile per debug mirato).",
    )
    parser.add_argument(
        "--report-tag",
        default=None,
        help="Aggiunge un suffisso ai file di report (Resoconto modifiche mec-ai) per non sovrascrivere i report precedenti.",
    )

    args = parser.parse_args()
    use_ai = DEFAULT_ENABLE_AI if not args.no_ai else False
    input_file = args.input or DEFAULT_INPUT_FILE
    output_file = args.output
    output_display = output_file or f"{OUTPUT_DIR}/{PRONTI_SUBDIR}/{DEFAULT_PRONTI_FILENAME}"

    checkpoint = load_checkpoint(input_file)
    start_from = args.start_from
    initial_results: List[Dict[str, Any]] = []

    if checkpoint and start_from == 1:
        last_processed = checkpoint.get("last_processed", 0)
        checkpoint_time = checkpoint.get("timestamp", "sconosciuto")
        print(f"\n[CHECKPOINT] Trovato checkpoint al post #{last_processed} (salvato: {checkpoint_time})")
        response = input(f"Vuoi riprendere dal post #{last_processed + 1}? [S/n]: ").strip().lower()

        if response in ["", "s", "si", "y", "yes"]:
            start_from = last_processed + 1
            print(f"[INFO] Ripresa dal post #{start_from}")
            initial_results = checkpoint.get("results", [])
        else:
            print("[INFO] Ricomincio dall'inizio")
            delete_checkpoint(input_file)
    elif checkpoint and start_from != 1:
        print(f"[INFO] Checkpoint ignorato, start manuale dal post #{start_from}")
        delete_checkpoint(input_file)
    elif start_from != 1:
        print(f"[INFO] Nessun checkpoint, start manuale dal post #{start_from}")

    print("\n===== MODIFICA TESTO POST CON AI =====")
    print(f"Input file: {input_file}")
    print(f"Output file: {output_display}")
    print(f"AI abilitata: {'SI' if use_ai else 'NO'}")
    print(f"Start from: Post #{start_from}")
    print("======================================")

    posts = load_posts(input_file)
    if args.only_post_id or args.only_post_number is not None:
        only_id = args.only_post_id
        only_number = args.only_post_number
        filtered = []
        for p in posts:
            pid = p.get("post_id") or p.get("id")
            pnum = p.get("post_number")
            if only_id and pid != only_id:
                continue
            if only_number is not None and pnum != only_number:
                continue
            filtered.append(p)
        posts = filtered
        # In modalità debug mirata ha senso elaborare comunque il post richiesto.
        start_from = 1
        delete_checkpoint(input_file)
        if not posts:
            print("[ERRORE] Nessun post trovato con i filtri richiesti (--only-post-id/--only-post-number).")
            sys.exit(1)

    audit_logger: Optional[AuditLogger] = None
    if args.audit_log:
        audit_logger = AuditLogger(Path(args.audit_log), include_text=args.audit_include_text)

    results = process_posts(
        posts,
        use_ai,
        start_from,
        input_file,
        args.checkpoint_interval,
        existing_results=initial_results,
        audit_logger=audit_logger,
    )
    save_results(results, output_file)
    save_ai_failures(results)
    generate_resoconto(results, args.report_tag)
    generate_prima_dopo(results, args.report_tag)

    if audit_logger:
        audit_logger.close()
        print(f"[INFO] Audit log scritto in: {Path(args.audit_log).resolve()}")

    delete_checkpoint(input_file)

    print("\n[COMPLETATO] Modifica testi completata con successo!")


if __name__ == "__main__":
    main()
