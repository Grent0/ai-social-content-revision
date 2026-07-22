#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 3: Modifica complessa del testo (AI) per IG
Legge i post dal file intermedio generato dal passo meccanico,
applica (se necessario) una riscrittura AI e aggiunge lo stato
`status_ai`. Il risultato finale viene salvato come posts_modificati.json.

Uso:
    python 3_modifica_testo_ai.py
    python 3_modifica_testo_ai.py --input output/modificati/posts_con_stato_meccanico.json --output posts_modificati.json
    python 3_modifica_testo_ai.py --no-ai
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import difflib


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

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


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


def _split_entries(raw: str) -> List[str]:
    """
    Divide stringhe con formati legacy: a;b oppure ("a","b").
    """
    import re

    if not raw:
        return []
    s = raw.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    parts = re.split(r"[;,\n]", s)
    entries: List[str] = []
    for p in parts:
        v = p.strip()
        if not v:
            continue
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if v:
            entries.append(v)
    return entries


def parse_legacy_pairs(raw: str) -> Dict[str, str]:
    """
    Parsa sostituzioni dal formato legacy "find:replace;find2:replace2".
    """
    pairs: Dict[str, str] = {}
    for entry in _split_entries(raw):
        if ":" not in entry:
            continue
        find, replace = entry.split(":", 1)
        find = find.strip()
        replace = replace.strip()
        if find:
            pairs[find] = replace
    return pairs


def parse_legacy_list(raw: str) -> List[str]:
    """
    Parsa una lista di termini separati da virgola/punto e virgola/newline.
    """
    terms: List[str] = []
    for entry in _split_entries(raw):
        entry = entry.strip()
        if ":" in entry:
            entry = entry.split(":", 1)[0]
        if entry:
            terms.append(entry)
    return terms


load_env_from_file()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON su file in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


# Chiave AI (opzionale)
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")

# Modello AI configurabile
AI_MODEL = os.getenv("AI_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Mappa sostituzioni meccaniche
REPLACEMENTS: Dict[str, str] = parse_json_env(
    "REPLACEMENTS",
    {
        "catalogo": "brochure",
        "link in bio": "link nel primo commento",
    },
)
LEGACY_REPL = parse_legacy_pairs(os.getenv("MEC_REPLACE", ""))
if LEGACY_REPL and not os.getenv("REPLACEMENTS"):
    REPLACEMENTS = LEGACY_REPL

# Parole chiave per decidere se un post va passato all'AI
AI_KEYWORDS: List[str] = parse_json_env(
    "AI_KEYWORDS",
    [
        "promo",
        "offerta",
        "sconto",
        "nuova collezione",
    ],
)
LEGACY_FILTER = parse_legacy_list(os.getenv("AI_FILTER_TEXT", ""))
if LEGACY_FILTER and not os.getenv("AI_KEYWORDS"):
    AI_KEYWORDS = LEGACY_FILTER

# Contesto generico passato all'AI
AI_EXTRA_CONTEXT = os.getenv(
    "AI_EXTRA_CONTEXT",
    (
        "Sei un esperto di energia elettrica e fonti rinnovabili, oltre che un copywriter tecnico. "
        "Il tuo compito è: "
        "1) rielaborare il testo in modo chiaro, professionale e coerente con il settore elettrico e delle rinnovabili; "
        "2) sostituire in maniera contestuale le parole/frasi indicate nel contesto aggiuntivo. "
        "Regole: non fare sostituzioni meccaniche; usa i nuovi termini in modo naturale e grammaticalmente corretto "
        "(genere, numero, tempi, preposizioni); puoi adattare la struttura delle frasi per renderle fluide e comprensibili; "
        "mantieni il significato tecnico e i dati (numeri, link, hashtag, riferimenti normativi); "
        "se una sostituzione letterale non ha senso, riformula per rispettare il concetto."
    ),
)

# Cartelle di gestione
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "ai"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
MODIFICATI_SUBDIR = os.getenv("MODIFICATI_SUBDIR", "modificati")

# Parametri di gestione elaborazione
DEFAULT_START_FROM = int(os.getenv("START_FROM_POST", "1"))
DEFAULT_CHECKPOINT_INTERVAL = int(os.getenv("CHECKPOINT_INTERVAL", "5"))
DEFAULT_ENABLE_AI = os.getenv("ENABLE_AI", "true").lower() in ["true", "1", "yes", "si"]
# File di input di default (prodotto dallo step meccanico)
DEFAULT_INPUT_FILE = "output/modificati/posts_con_stato_meccanico.json"


# ==========================
# FUNZIONI
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


def load_posts(input_path: str) -> List[Dict[str, Any]]:
    """
    Carica i post da un file JSON intermedio (fase meccanica).
    Risolve percorsi relativi rispetto alla cartella dello script se necessario.
    """
    path = Path(input_path)
    if not path.exists():
        # Prova a risolvere rispetto alla cartella dello script
        base_dir = Path(__file__).parent
        alt_path = base_dir / path
        if alt_path.exists():
            path = alt_path
        else:
            print(f"[ERRORE] File non trovato: {input_path}")
            print("[INFO] Esegui prima lo script 2_modifica_testo_meccanica.py per generare il file intermedio.")
            sys.exit(1)

    data = json.loads(path.read_text())
    posts = data.get("posts") or data.get("results", [])
    print(f"[INFO] Caricati {len(posts)} post da {path}")
    return posts


def needs_ai_rewrite(text: str, keywords: List[str]) -> bool:
    """
    Ritorna True se il testo contiene almeno una delle keywords (case-insensitive).
    """
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def check_keywords_in_text(text: str, keywords: List[str]) -> List[str]:
    """
    Ritorna la lista di keywords ancora presenti nel testo (case-insensitive).
    """
    lowered = text.lower()
    found = [k for k in keywords if k.lower() in lowered]
    return found


def rewrite_with_ai(
    original_text: str,
    extra_context: Optional[str] = None,
    replacements: Optional[Dict[str, str]] = None,
    keywords_to_remove: Optional[List[str]] = None,
) -> str:
    """
    Riscrive il testo usando un modello di AI.
    keywords_to_remove: lista di parole da rimuovere/sostituire nel testo.
    """
    if not AI_API_KEY:
        raise RuntimeError(
            "AI_API_KEY/OPENAI_API_KEY non impostata, ma è stata richiesta la riscrittura AI."
        )

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
            f"IMPORTANTE: Le seguenti parole/espressioni NON devono apparire nel testo finale: {keywords_list}. "
            "Reinterpreta il concetto espresso da queste parole e sostituiscile con sinonimi, perifrasi o riformulazioni "
            "che mantengano lo stesso significato ma usando parole diverse. Il testo deve essere naturale e fluido.\n\n"
        )

    prompt = (
        "Riscrivi il seguente testo per un post Instagram, mantenendo il significato "
        "ma rendendolo più chiaro, coinvolgente e naturale. "
        "Mantieni eventuali emoji, link e hashtag se presenti.\n\n"
        f"{keywords_prompt}"
        f"{replacements_prompt}"
        f"Contesto aggiuntivo: {extra_context or ''}\n\n"
        f"Testo originale:\n\"\"\"{original_text}\"\"\""
    )

    # Chiamata API OpenAI
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
        "temperature": 0.7,
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

    return ai_text


def process_posts(
    posts: List[Dict[str, Any]],
    use_ai: bool = True,
    start_from: int = 1,
    input_path: str = "",
    checkpoint_interval: int = 5,
    existing_results: Optional[List[Dict[str, Any]]] = None,
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
        post_id = post.get("post_id") or post.get("id") or post.get("permalink")
        created_time = post.get("created_time") or post.get("timestamp") or "N/A"
        permalink = post.get("permalink") or post.get("permalink_url") or ""
        original_message = post.get("original_message") or post.get("message") or post.get("caption") or ""
        base_text = post.get("modified_message") or post.get("message_meccanico") or original_message

        # Salta i post prima di start_from ma li preserva nell'output finale
        if post_number < start_from:
            results.append({
                **post,
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
                "original_message": original_message,
                "message_meccanico": base_text,
                "modified_message": base_text,
                "status_ai": "invariato",
                "status_meccanico": post.get("status_meccanico", "invariato"),
                "used_ai": False,
                "ai_error": False,
                "ai_error_message": "",
                "parte_modificata": "",
            })
            continue

        if not base_text:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) senza testo dopo la fase meccanica.")
            results.append({
                **post,
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
                "original_message": original_message,
                "message_meccanico": base_text,
                "modified_message": base_text,
                "status_ai": "invariato",
                "status_meccanico": post.get("status_meccanico", "invariato"),
                "used_ai": False,
                "ai_error": False,
                "ai_error_message": "",
                "parte_modificata": "",
            })
            continue

        print(f"\n[POST #{post_number}] {post_id} - {created_time}")
        print("Testo dopo fase meccanica:")
        print(base_text)
        print("-" * 60)

        final_text = base_text
        used_ai_for_this_post = False
        ai_error = False
        ai_error_message = ""

        if use_ai and needs_ai_rewrite(base_text, AI_KEYWORDS):
            print("[INFO] Il post soddisfa i criteri per la riscrittura AI.")
            try:
                final_text = rewrite_with_ai(
                    base_text,
                    AI_EXTRA_CONTEXT,
                    REPLACEMENTS if REPLACEMENTS else None,
                    AI_KEYWORDS,
                )
                used_ai_for_this_post = True

                remaining_keywords = check_keywords_in_text(final_text, AI_KEYWORDS)
                if remaining_keywords:
                    print(f"[ATTENZIONE] Il testo elaborato dall'AI contiene ancora: {', '.join(remaining_keywords)}")
                    print("[INFO] Forzo una seconda elaborazione per reinterpretare le keywords rimaste...")
                    try:
                        final_text = rewrite_with_ai(
                            final_text,
                            f"{AI_EXTRA_CONTEXT}\n\nATTENZIONE CRITICA: Il testo contiene ancora le seguenti parole: {', '.join(remaining_keywords)}. "
                            f"Queste parole DEVONO essere sostituite con sinonimi, perifrasi o riformulazioni diverse che esprimano lo stesso concetto. "
                            f"NON usare letteralmente queste parole nel testo finale. Reinterpreta il significato e usa termini alternativi.",
                            REPLACEMENTS if REPLACEMENTS else None,
                            remaining_keywords,
                        )
                        final_check = check_keywords_in_text(final_text, AI_KEYWORDS)
                        if final_check:
                            print(f"[WARNING] Dopo la seconda elaborazione, alcune keywords sono ancora presenti: {', '.join(final_check)}")
                            print("[INFO] Potrebbe essere necessaria una revisione manuale.")
                        else:
                            print("[OK] Tutte le keywords sono state reinterpretate con successo!")
                    except RuntimeError as e2:
                        print(f"[WARN] Errore nella seconda elaborazione AI: {e2}")
                        print("[INFO] Uso il testo della prima elaborazione.")
            except RuntimeError as e:
                print("[WARN] Errore AI, mantengo il testo della fase meccanica:", e)
                ai_error = True
                ai_error_message = str(e)
                final_text = base_text
        else:
            print("[INFO] Il post non richiede AI (parole chiave assenti oppure AI disabilitata).")

        status_ai = "modificato" if final_text != base_text else "invariato"
        parte_modificata = estrai_parte_modificata(base_text, final_text)

        print("Nuovo testo proposto:")
        print(final_text)
        print("-" * 60)

        results.append({
            **post,
            "post_number": post_number,
            "post_id": post_id,
            "created_time": created_time,
            "permalink": permalink,
            "original_message": original_message,
            "message_meccanico": base_text,
            "modified_message": final_text,
            "status_meccanico": post.get("status_meccanico", "invariato"),
            "status_ai": status_ai,
            "used_ai": used_ai_for_this_post and not ai_error,
            "ai_error": ai_error,
            "ai_error_message": ai_error_message,
            "parte_modificata": parte_modificata,
        })

        if input_path and post_number % checkpoint_interval == 0:
            save_checkpoint(input_path, post_number, results)
            print(f"[CHECKPOINT] Salvato progresso al post #{post_number}")

    total_ai_used = sum(1 for r in results if r.get("used_ai"))
    total_ai_modified = sum(1 for r in results if r.get("status_ai") == "modificato")
    total_ai_failed = sum(1 for r in results if r.get("ai_error"))

    print("\n========== RIEPILOGO AI ==========")
    print(f"Post totali processati: {len(posts)}")
    print(f"Post modificati dall'AI: {total_ai_modified}")
    print(f"Post passati all'AI: {total_ai_used}")
    print(f"Post con errore AI: {total_ai_failed}")
    print("================================")

    return results


def save_results(all_posts: List[Dict[str, Any]]) -> None:
    """
    Filtra i risultati per tenere solo i post effettivamente modificati
    e li salva in un file JSON dedicato, pronto per la pubblicazione.
    """
    
    # 1. Filtra solo i post modificati
    modified_posts = [
        p for p in all_posts
        if p.get("status_meccanico") == "modificato" or p.get("status_ai") == "modificato"
    ]
    
    # 2. Calcola le statistiche dai risultati completi
    total_posts = len(all_posts)
    total_meccanici = sum(1 for r in all_posts if r.get("status_meccanico") == "modificato")
    total_ai = sum(1 for r in all_posts if r.get("status_ai") == "modificato")
    
    # 3. Definisci il nuovo percorso di output
    base_dir = Path(__file__).parent
    output_path = base_dir / "output" / "pronti_per_pubblicazione" / "pronti.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 4. Crea il payload finale
    payload = {
        "summary": {
            "total_posts_processed": total_posts,
            "total_modificati_meccanici": total_meccanici,
            "total_modificati_ai": total_ai,
            "total_pronti_per_pubblicazione": len(modified_posts),
        },
        "posts": modified_posts,
    }

    # 5. Salva il file
    write_json_atomic(output_path, payload)
    print(f"\n[INFO] Creato file per la pubblicazione con {len(modified_posts)} post modificati.")
    print(f"[INFO] Risultati salvati in: {output_path.resolve()}")


def save_ai_failures(results: List[Dict[str, Any]]) -> None:
    """
    Salva in un file dedicato i post che hanno avuto errori AI.
    """
    failed = [
        {
            "post_number": r.get("post_number"),
            "post_id": r.get("post_id"),
            "created_time": r.get("created_time"),
            "permalink": r.get("permalink"),
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
        "--no-ai",
        action="store_true",
        help="Disabilita l'uso dell'AI anche se abilitata nel .env.",
    )

    args = parser.parse_args()
    # Usa default da .env se --no-ai non è specificato
    use_ai = DEFAULT_ENABLE_AI if not args.no_ai else False
    input_file = args.input or DEFAULT_INPUT_FILE
    
    # Controlla se esiste un checkpoint
    checkpoint = load_checkpoint(input_file)
    start_from = args.start_from
    initial_results: List[Dict[str, Any]] = []

    if checkpoint and start_from == 1:
        # Checkpoint trovato e nessun override manuale
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
        # Override manuale specificato
        print(f"[INFO] Checkpoint ignorato, start manuale dal post #{start_from}")
        delete_checkpoint(input_file)
    elif start_from != 1:
        print(f"[INFO] Nessun checkpoint, start manuale dal post #{start_from}")

    print("\n===== MODIFICA TESTO POST IG CON AI =====")
    print(f"Input file: {input_file}")
    print(f"AI abilitata: {'SI' if use_ai else 'NO'}")
    print(f"Start from: Post #{start_from}")
    print("======================================")

    posts = load_posts(input_file)
    results = process_posts(
        posts,
        use_ai,
        start_from,
        input_file,
        args.checkpoint_interval,
        existing_results=initial_results,
    )
    save_results(results)
    save_ai_failures(results)

    # Elimina checkpoint al completamento
    delete_checkpoint(input_file)

    print("\n[COMPLETATO] Modifica testi completata con successo!")


if __name__ == "__main__":
    main()
