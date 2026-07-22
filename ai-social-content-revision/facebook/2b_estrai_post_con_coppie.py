#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 2b: Estrai post che contengono coppie di parole "trigger"

Obiettivo:
  - Legge l'output della fase meccanica (es. output/modificati/posts_con_stato_meccanico.json)
  - Identifica i post che contengono almeno una coppia configurata (AI_WORD_PAIRS)
  - Salva un JSON separato con solo quei post, da passare successivamente a 3_modifica_testo_ai.py

Uso:
  python 2b_estrai_post_con_coppie.py \
    --input output/modificati/posts_con_stato_meccanico.json \
    --output output/modificati/posts_con_coppie.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
                os.environ.setdefault(current_key, value)
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
        os.environ.setdefault(key, value)

    if current_key and current_lines:
        value = "\n".join(current_lines)
        if quote_char and value.startswith(quote_char):
            value = value[1:]
        os.environ.setdefault(current_key, value)


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


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON su file in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


load_env_from_file()


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


# ==========================
# FUNZIONI
# ==========================


def load_posts(input_path: str) -> List[Dict[str, Any]]:
    path = Path(input_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    if not path.exists():
        print(f"[ERRORE] File non trovato: {path}")
        sys.exit(1)

    data = json.loads(path.read_text())
    posts = data.get("posts") or data.get("results", [])
    print(f"[INFO] Caricati {len(posts)} post da {path.name}")
    return posts


def find_triggered_pairs(text: str, pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    if not text:
        return []
    triggered: List[Tuple[str, str]] = []
    for first, second in pairs:
        p1 = r"\b" + re.escape(first) + r"\b"
        p2 = r"\b" + re.escape(second) + r"\b"
        if re.search(p1, text, flags=re.IGNORECASE) and re.search(p2, text, flags=re.IGNORECASE):
            triggered.append((first, second))
    return triggered


def build_output_path(output_arg: Optional[str]) -> Path:
    base_dir = Path(__file__).parent
    default = base_dir / "output" / "modificati" / "posts_con_coppie.json"

    if not output_arg:
        return default

    out_path = Path(output_arg)
    if not out_path.is_absolute():
        out_path = (base_dir / out_path).resolve()
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estrae i post che contengono coppie di parole trigger (AI_WORD_PAIRS) e li salva in un JSON separato."
    )
    parser.add_argument(
        "--input",
        default="output/modificati/posts_con_stato_meccanico.json",
        help="File JSON di input (di solito l'output dello step 2).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="File JSON di output (default: output/modificati/posts_con_coppie.json).",
    )

    args = parser.parse_args()
    posts = load_posts(args.input)
    out_path = build_output_path(args.output)

    extracted: List[Dict[str, Any]] = []
    breakdown: Dict[str, int] = {}

    for idx, post in enumerate(posts, start=1):
        post_number = post.get("post_number", idx)
        post_id = post.get("post_id") or post.get("id")
        created_time = post.get("created_time", "N/A")
        original_message = post.get("original_message") or post.get("message") or ""
        base_text = post.get("modified_message") or post.get("message_meccanico") or original_message

        triggered_pairs = find_triggered_pairs(base_text, AI_WORD_PAIRS)
        if not triggered_pairs:
            continue

        for a, b in triggered_pairs:
            key = f"{a}+{b}"
            breakdown[key] = breakdown.get(key, 0) + 1

        item = {**post}
        item["post_number"] = post_number
        item["post_id"] = post_id
        item["created_time"] = created_time
        item["triggered_pairs"] = [[a, b] for a, b in triggered_pairs]
        extracted.append(item)

    payload = {
        "summary": {
            "generated_at": dt.datetime.now().isoformat(),
            "input_total_posts": len(posts),
            "output_total_posts": len(extracted),
            "pairs_breakdown": breakdown,
        },
        "posts": extracted,
    }

    write_json_atomic(out_path, payload)
    print(f"[INFO] Post con coppie trovati: {len(extracted)}")
    print(f"[INFO] Output scritto in: {out_path}")


if __name__ == "__main__":
    main()

