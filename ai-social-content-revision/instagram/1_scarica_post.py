#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 1: Scarica i post Instagram (media) in un intervallo di date
e li salva in un file JSON.

Uso:
    python 1_scarica_post.py --since 2025-09-01 --until 2025-09-30
    python 1_scarica_post.py --since 2025-09-01 --until 2025-09-30 --output posts.json
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

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

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_or_raise_any(candidates, display_name: str) -> str:
    """
    Restituisce il primo valore disponibile tra le chiavi candidate,
    altrimenti solleva errore citando display_name.
    """
    if isinstance(candidates, str):
        candidates = [candidates]
    for key in candidates:
        val = os.getenv(key)
        if val:
            return val
    names = ", ".join(candidates)
    raise RuntimeError(f"Manca la variabile d'ambiente {display_name} (prova con: {names}). Impostala in .env.")


load_env_from_file()

# ID account IG Business/Creator e token obbligatori (alias PAGE_ID/PAGE_ACCESS_TOKEN per allineamento .env FB)
IG_USER_ID = env_or_raise_any(["PAGE_ID", "IG_USER_ID"], "IG_USER_ID/PAGE_ID")
ACCESS_TOKEN = env_or_raise_any(["PAGE_ACCESS_TOKEN", "ACCESS_TOKEN"], "ACCESS_TOKEN/PAGE_ACCESS_TOKEN")

# Endpoint Graph API (usa nomi FB per compatibilità .env)
IG_API_BASE = os.getenv("FB_API_BASE") or os.getenv("IG_API_BASE") or "https://graph.facebook.com/v18.0"

# Anno di riferimento da .env (opzionale)
current_year = dt.datetime.now().year
year_start = os.getenv("DEFAULT_YEAR_START", str(current_year))
year_end = os.getenv("DEFAULT_YEAR_END", str(current_year))
DEFAULT_SINCE = f"{year_start}-01-01"
DEFAULT_UNTIL = f"{year_end}-12-31"

# Cartelle di gestione
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
SCARICATI_SUBDIR = os.getenv("SCARICATI_SUBDIR", "scaricati")


# ==========================
# FUNZIONI
# ==========================


def parse_date(date_str: str) -> str:
    """
    Controlla e normalizza la data in formato YYYY-MM-DD.
    """
    try:
        dt.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Data non valida: {date_str}. Usa formato YYYY-MM-DD (es. 2025-09-01).")
        sys.exit(1)
    return date_str


def parse_timestamp(ts: str) -> dt.datetime:
    """
    Converte una timestamp IG (2025-09-01T10:20:30+0000) in datetime.
    """
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def get_posts_in_range(since: str, until: str, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Recupera tutti i post dell'account IG nell'intervallo [since, until] (date).
    since/until sono stringhe YYYY-MM-DD.
    limit controlla quante entry vengono chieste per pagina (max 100).
    """
    since_date = parse_timestamp(f"{since}T00:00:00+0000").date()
    until_date = parse_timestamp(f"{until}T23:59:59+0000").date()

    print(f"[INFO] Scarico i post IG da {since} a {until}...")

    url = f"{IG_API_BASE}/{IG_USER_ID}/media"
    params = {
        "fields": "id,caption,media_type,media_url,timestamp,permalink",
        "access_token": ACCESS_TOKEN,
        "limit": max(1, min(limit, 100)),
    }

    posts: List[Dict[str, Any]] = []
    while True:
        r = requests.get(url, params=params)
        if r.status_code != 200:
            print("[ERRORE] Chiamata Instagram Graph fallita:", r.status_code, r.text)
            sys.exit(1)

        data = r.json()
        for item in data.get("data", []):
            ts = item.get("timestamp")
            if not ts:
                continue
            try:
                post_date = parse_timestamp(ts).date()
            except ValueError:
                continue
            if since_date <= post_date <= until_date:
                posts.append(item)

        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url:
            break

        url = next_url
        params = {}

    print(f"[INFO] Trovati {len(posts)} post IG nell'intervallo.")
    return posts


def save_posts(posts: List[Dict[str, Any]], output_path: str) -> None:
    """
    Salva i post in un file JSON nella cartella configurata.
    """
    base_dir = Path(__file__).parent
    output_dir = base_dir / OUTPUT_DIR / SCARICATI_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = Path(output_path)
    if not out_path.is_absolute() and str(out_path.parent) == ".":
        out_path = output_dir / out_path.name
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    numbered_posts: List[Dict[str, Any]] = []
    for idx, post in enumerate(posts, start=1):
        post_copy = dict(post)
        post_copy["post_number"] = idx
        post_copy["post_id"] = post_copy.get("id")
        post_copy["created_time"] = post_copy.get("timestamp")
        # Alias compatibile con la pipeline FB
        post_copy["message"] = post_copy.get("caption", "")
        numbered_posts.append(post_copy)

    payload = {
        "total_posts": len(posts),
        "downloaded_at": dt.datetime.now().isoformat(),
        "posts": numbered_posts,
    }

    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[INFO] Post IG salvati in {out_path.resolve()}")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scarica i post Instagram (media) in un intervallo di date."
    )
    parser.add_argument(
        "--since",
        help="Data iniziale (inclusa) nel formato YYYY-MM-DD (es. 2025-09-01). Se non specificata, usa inizio anno corrente.",
    )
    parser.add_argument(
        "--until",
        help="Data finale (inclusa) nel formato YYYY-MM-DD (es. 2025-09-30). Se non specificata, usa fine anno corrente.",
    )
    parser.add_argument(
        "--output",
        default="posts_scaricati.json",
        help="Percorso file JSON dove salvare i post scaricati (default: posts_scaricati.json)",
    )

    args = parser.parse_args()

    since = parse_date(args.since) if args.since else DEFAULT_SINCE
    until = parse_date(args.until) if args.until else DEFAULT_UNTIL

    if since > until:
        print("Errore: --since deve essere antecedente o uguale a --until.")
        sys.exit(1)

    print("===== SCARICAMENTO POST INSTAGRAM =====")
    print(f"Account IG: {IG_USER_ID}")
    print(f"Intervallo date: {since} -> {until}")
    print(f"Output file: {args.output}")
    print("=======================================")

    posts = get_posts_in_range(since, until)
    save_posts(posts, args.output)

    print("\n[COMPLETATO] Post IG scaricati con successo!")


if __name__ == "__main__":
    main()
