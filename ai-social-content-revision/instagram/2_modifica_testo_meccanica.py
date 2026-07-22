#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 2: Modifica meccanica del testo (IG)
Legge i post da un file JSON, applica sostituzioni meccaniche
e salva i risultati in un nuovo file JSON con uno stato di modifica.

Uso:
    python 2_modifica_testo_meccanica.py
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


load_env_from_file()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON su file in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


# Mappa sostituzioni meccaniche
REPLACEMENTS: Dict[str, str] = parse_json_env(
    "REPLACEMENTS",
    {
        "catalogo": "brochure",
        "link in bio": "link nel primo commento",
    },
)

# Hashtag da rimuovere se contengono queste sottostringhe (case-insensitive)
HASHTAG_REMOVE_SUBSTRINGS = [
    s.lower() for s in parse_json_env("HASHTAG_REMOVE_SUBSTRINGS", ["concettoA", "topicA"])
]
# Hashtag da rimuovere esplicitamente (case-insensitive, confronto sul tag completo)
HASHTAG_REMOVE_LIST = [
    s.lower()
    for s in parse_json_env(
        "HASHTAG_REMOVE",
        ["#esempiohashtag", "#altrohashtag", "#esempio_hashtag"],
    )
]

# Cartelle di gestione
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "meccanici"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
MODIFICATI_SUBDIR = os.getenv("MODIFICATI_SUBDIR", "modificati")

# Parametri di gestione elaborazione
DEFAULT_START_FROM = int(os.getenv("START_FROM_POST", "1"))
DEFAULT_CHECKPOINT_INTERVAL = int(os.getenv("CHECKPOINT_INTERVAL", "5"))

# Output intermedio di default
DEFAULT_OUTPUT_FILE = "posts_con_stato_meccanico.json"

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

    checkpoint_name = f"checkpoint_meccanico_{input_file.stem}.json"
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
    Carica i post da un file JSON.
    """
    path = Path(input_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    if not path.exists():
        print(f"[ERRORE] File non trovato: {path}")
        print("[INFO] Esegui prima lo script 1_scarica_post.py per scaricare i post.")
        sys.exit(1)

    data = json.loads(path.read_text())
    posts = data.get("posts", [])
    print(f"[INFO] Caricati {len(posts)} post da {path.name}")
    return posts


def replace_words(text: str, replacements: Dict[str, str]) -> str:
    """
    Sostituzione meccanica basata su dict {vecchia: nuova}.
    """
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def remove_blocked_hashtags(text: str) -> str:
    """
    Rimuove gli hashtag che contengono sottostringhe bloccate (es. 'concettoA', 'topicA').
    Mantiene il resto del testo intatto.
    """
    if not text:
        return text

    hashtag_re = re.compile(r"#\w[\w-]*", flags=re.UNICODE)

    def _replace(match: re.Match) -> str:
        tag = match.group(0)
        lower = tag.lower()
        if lower in HASHTAG_REMOVE_LIST or any(sub in lower for sub in HASHTAG_REMOVE_SUBSTRINGS):
            return ""
        return tag

    cleaned = hashtag_re.sub(_replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def passthrough_post(post: Dict[str, Any], post_number: int) -> Dict[str, Any]:
    """
    Restituisce un post marcato come invariato (utile per start manuali).
    """
    post_copy = post.copy()
    original_message = (
        post_copy.get("message")
        or post_copy.get("original_message")
        or post_copy.get("caption")
        or ""
    )
    post_copy["post_number"] = post_number
    post_copy["post_id"] = post_copy.get("post_id") or post_copy.get("id")
    post_copy["original_message"] = original_message
    post_copy["original_caption"] = original_message
    post_copy["message_meccanico"] = original_message
    post_copy["modified_message"] = original_message
    post_copy["status_meccanico"] = "invariato"
    return post_copy


def process_posts(
    posts: List[Dict[str, Any]],
    start_from: int,
    input_path: str,
    checkpoint_interval: int,
    existing_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Processa i post applicando sostituzioni meccaniche.
    """
    results: List[Dict[str, Any]] = list(existing_results) if existing_results else []
    modified_count = sum(1 for r in results if r.get("status_meccanico") == "modificato")
    already_processed = len(results)

    for index, post in enumerate(posts, start=1):
        if index <= already_processed:
            continue

        post_number = post.get("post_number", index)

        # Salta i post prima di start_from ma li preserva nell'output finale
        if post_number < start_from:
            results.append(passthrough_post(post, post_number))
            continue

        original_message = (
            post.get("message")
            or post.get("original_message")
            or post.get("caption")
            or ""
        )

        if not original_message:
            post_copy = passthrough_post(post, post_number)
            results.append(post_copy)
            continue

        # Prima pulisco gli hashtag indesiderati, poi applico le sostituzioni meccaniche
        cleaned_message = remove_blocked_hashtags(original_message)
        modified_message = replace_words(cleaned_message, REPLACEMENTS)

        post_copy = post.copy()
        post_copy["post_number"] = post_number
        post_copy["post_id"] = post.get("post_id") or post.get("id")
        post_copy["original_message"] = original_message
        post_copy["original_caption"] = original_message
        post_copy["message_meccanico"] = modified_message
        post_copy["modified_message"] = modified_message

        if modified_message != original_message:
            modified_count += 1
            post_copy["status_meccanico"] = "modificato"
            print(f"[MODIFICATO] Post #{post_number}")
        else:
            post_copy["status_meccanico"] = "invariato"

        results.append(post_copy)

        # Salva checkpoint ogni N post
        if input_path and post_number % checkpoint_interval == 0:
            save_checkpoint(input_path, post_number, results)
            print(f"[CHECKPOINT] Salvato progresso al post #{post_number}")

    print("\n========== RIEPILOGO MECCANICO ==========")
    print(f"Post totali processati: {len(posts)}")
    print(f"Post modificati meccanicamente: {modified_count}")
    print("==========================================")

    return results


def save_results(results: List[Dict[str, Any]], output_path: str) -> None:
    """
    Salva i risultati in un file JSON.
    """
    base_dir = Path(__file__).parent
    output_dir = base_dir / OUTPUT_DIR / MODIFICATI_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    final_path = output_dir / output_path

    payload = {
        "total_posts": len(results),
        "modificati_meccanici": sum(1 for r in results if r.get("status_meccanico") == "modificato"),
        "posts": results,  # Mantiene la chiave 'posts' per coerenza
    }
    write_json_atomic(final_path, payload)
    print(f"[INFO] Risultati salvati in {final_path.resolve()}")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Applica sostituzioni meccaniche al testo dei post."
    )
    parser.add_argument(
        "--input",
        default="output/scaricati/posts_scaricati.json",
        help="File JSON con i post scaricati (default: output/scaricati/posts_scaricati.json)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"File JSON dove salvare i post con modifiche meccaniche (default: {DEFAULT_OUTPUT_FILE}).",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=DEFAULT_START_FROM,
        help=f"Numero del post da cui iniziare (default: {DEFAULT_START_FROM}).",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=DEFAULT_CHECKPOINT_INTERVAL,
        help=f"Intervallo di salvataggio checkpoint (default: {DEFAULT_CHECKPOINT_INTERVAL}).",
    )

    args = parser.parse_args()
    input_file = args.input
    output_file = args.output

    # Gestione Checkpoint
    checkpoint = load_checkpoint(input_file)
    start_from = args.start_from
    initial_results: List[Dict[str, Any]] = []

    if checkpoint and start_from == 1:
        last_processed = checkpoint.get("last_processed", 0)
        response = input(f"Trovato checkpoint al post #{last_processed}. Riprendere? [S/n]: ").strip().lower()
        if response in ["", "s", "si", "y", "yes"]:
            start_from = last_processed + 1
            initial_results = checkpoint.get("results", [])
            print(f"[INFO] Ripresa dal post #{start_from}")
        else:
            print("[INFO] Ricomincio dall'inizio.")
            delete_checkpoint(input_file)
    elif checkpoint and start_from != 1:
        # L'utente forza uno start manuale, quindi il checkpoint non serve
        print(f"[INFO] Checkpoint ignorato, start manuale dal post #{start_from}")
        delete_checkpoint(input_file)

    print("\n===== MODIFICA MECCANICA TESTO POST IG =====")
    print(f"Input file: {input_file}")
    print(f"Output file: {output_file}")
    print(f"Start from: Post #{start_from}")
    print("==========================================")

    all_posts = load_posts(input_file)

    processed_results = process_posts(
        all_posts,
        start_from,
        input_file,
        args.checkpoint_interval,
        existing_results=initial_results,
    )

    save_results(processed_results, output_file)

    delete_checkpoint(input_file)

    print("\n[COMPLETATO] Modifica meccanica completata.")


if __name__ == "__main__":
    main()
