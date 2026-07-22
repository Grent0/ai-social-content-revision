#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 1: Scarica i post da Facebook
Recupera tutti i post della pagina nell'intervallo di date specificato
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


def env_or_raise(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Manca la variabile d'ambiente {key}. Impostala in .env.")
    return value


load_env_from_file()

# Page ID e token obbligatori
PAGE_ID = env_or_raise("PAGE_ID")
ACCESS_TOKEN = env_or_raise("PAGE_ACCESS_TOKEN")

# Endpoint Graph API
FB_API_BASE = os.getenv("FB_API_BASE", "https://graph.facebook.com/v18.0")
# Campi recuperati per ogni post (ridotti al minimo per evitare l'errore sugli attachment)
POST_FIELDS = "id,message,created_time"

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


def get_posts_in_range(since: str, until: str, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Recupera tutti i post della pagina nell'intervallo [since, until].
    since/until sono stringhe YYYY-MM-DD.
    limit controlla quante entry vengono chieste per pagina (max 100).
    """
    print(f"[INFO] Scarico i post da {since} a {until}...")

    posts_url = f"{FB_API_BASE}/{PAGE_ID}/posts"
    feed_url = f"{FB_API_BASE}/{PAGE_ID}/feed"
    url = posts_url
    params = {
        "since": since,
        "until": until,
        "fields": POST_FIELDS,
        "access_token": ACCESS_TOKEN,
        "limit": max(1, min(limit, 100)),
    }

    posts: List[Dict[str, Any]] = []
    retried_with_feed = False
    while True:
        r = requests.get(url, params=params)
        if r.status_code != 200:
            body = r.text
            if (
                not retried_with_feed
                and "deprecate_post_aggregated_fields_for_attachement" in body
                and url == posts_url
            ):
                print("[WARN] Errore di deprecazione attachments su /posts, riprovo con /feed.")
                url = feed_url
                retried_with_feed = True
                # params restano invariati (since/until/fields/token)
                continue

            print("[ERRORE] Chiamata Facebook fallita:", r.status_code, body)
            print(
                "[INFO] Campo attachments escluso; se persiste controlla token/permessi e prova con un intervallo date più stretto."
            )
            sys.exit(1)

        data = r.json()
        chunk = data.get("data", [])
        posts.extend(chunk)

        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url:
            break

        # Le prossime chiamate usano direttamente l'URL di "next"
        url = next_url
        params = {}

    print(f"[INFO] Trovati {len(posts)} post nell'intervallo.")
    fetch_and_attach_media(posts)
    enrich_media_info(posts)
    return posts


def fetch_attachments(post_id: str) -> Dict[str, Any]:
    """
    Recupera gli attachments di un post tramite endpoint dedicato, evitando i campi deprecati.
    """
    url = f"{FB_API_BASE}/{post_id}/attachments"
    params = {
        "fields": "id,media_type,type,target{id},title,description,url",
        "limit": 25,
        "access_token": ACCESS_TOKEN,
    }
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def fetch_and_attach_media(posts: List[Dict[str, Any]]) -> None:
    """
    Per ogni post scarica gli attachments con una chiamata dedicata.
    """
    for post in posts:
        pid = post.get("id")
        if not pid:
            continue
        try:
            data = fetch_attachments(pid)
            attachments = data.get("attachments")
            data_list = data.get("data")
            if attachments:
                post["attachments"] = attachments
            elif data_list:
                post["attachments"] = {"data": data_list}
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] Impossibile leggere attachments per post {pid}: {exc}")


def is_video_media(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return "video" in mt


def is_image_media(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return any(token in mt for token in ("photo", "image"))


def fetch_video_length(video_id: str) -> Optional[float]:
    """
    Recupera la durata del video (secondi) tramite Graph API.
    """
    if not video_id:
        return None
    url = f"{FB_API_BASE}/{video_id}"
    params = {"fields": "length", "access_token": ACCESS_TOKEN}
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        # Non solleviamo per non interrompere il download dei post
        print(f"[WARN] Impossibile recuperare durata video {video_id}: {resp.status_code}")
        return None
    data = resp.json()
    return data.get("length")


def enrich_media_info(posts: List[Dict[str, Any]]) -> None:
    """
    Analizza gli attachment dei post e aggiunge flag has_image/has_video e durata video (se disponibile).
    """
    video_ids: List[str] = []
    video_map: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}

    for post in posts:
        attachments = post.get("attachments", {})
        data_list = attachments.get("data", []) if isinstance(attachments, dict) else []
        has_image = False
        has_video = False
        videos: List[Dict[str, Any]] = []

        for att in data_list:
            media_type = att.get("media_type") or att.get("type") or ""
            target = att.get("target") or {}
            target_id = target.get("id") or att.get("id")
            url = att.get("url") or ""
            title = att.get("title")
            description = att.get("description")

            if is_video_media(media_type):
                has_video = True
                video_entry = {
                    "id": target_id,
                    "url": url,
                    "title": title,
                    "description": description,
                    "duration_sec": None,
                }
                videos.append(video_entry)
                if target_id:
                    video_ids.append(target_id)
                    video_map.setdefault(target_id, []).append((post, video_entry))
            elif is_image_media(media_type):
                has_image = True

        post["has_image"] = has_image
        post["has_video"] = has_video
        if videos:
            post["videos"] = videos

    # Recupera le durate dei video (chiamate separate per non rompere il download post)
    unique_ids = list(dict.fromkeys(video_ids))
    for vid in unique_ids:
        length = fetch_video_length(vid)
        if length is None:
            continue
        for _, video_entry in video_map.get(vid, []):
            video_entry["duration_sec"] = length


def compute_media_stats(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calcola quanti attachment immagine/video ci sono e la durata totale dei video.
    """
    images = 0
    videos = 0
    videos_duration = 0.0

    for post in posts:
        attachments = post.get("attachments", {})
        data_list = attachments.get("data", []) if isinstance(attachments, dict) else []
        for att in data_list:
            media_type = att.get("media_type") or att.get("type") or ""
            if is_image_media(media_type):
                images += 1
            elif is_video_media(media_type):
                videos += 1

        for video_entry in post.get("videos", []) or []:
            dur = video_entry.get("duration_sec")
            if isinstance(dur, (int, float)):
                videos_duration += float(dur)

    return {
        "images": images,
        "videos": videos,
        "videos_total_duration_sec": round(videos_duration, 3),
    }


def save_posts(posts: List[Dict[str, Any]], output_path: str) -> None:
    """
    Salva i post in un file JSON nella cartella configurata.
    """
    media_summary = compute_media_stats(posts)

    # Crea percorso completo con sottocartella
    base_dir = Path(__file__).parent
    output_dir = base_dir / OUTPUT_DIR / SCARICATI_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Se output_path è solo il nome del file, usalo nella cartella configurata
    out_path = Path(output_path)
    if not out_path.is_absolute() and str(out_path.parent) == ".":
        out_path = output_dir / out_path.name
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    
    numbered_posts: List[Dict[str, Any]] = []
    for idx, post in enumerate(posts, start=1):
        post_copy = dict(post)
        post_copy["post_number"] = idx
        numbered_posts.append(post_copy)

    payload = {
        "total_posts": len(posts),
        "downloaded_at": dt.datetime.now().isoformat(),
        "media_summary": media_summary,
        "posts": numbered_posts,
    }
    
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[INFO] Post salvati in {out_path.resolve()}")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scarica i post di una pagina Facebook in un intervallo di date."
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

    # Se non specificate, usa valori da .env o inizio/fine anno corrente
    since = parse_date(args.since) if args.since else DEFAULT_SINCE
    until = parse_date(args.until) if args.until else DEFAULT_UNTIL

    # Controllo che since <= until
    if since > until:
        print("Errore: --since deve essere antecedente o uguale a --until.")
        sys.exit(1)

    print("===== SCARICAMENTO POST DA FACEBOOK =====")
    print(f"Pagina FB: {PAGE_ID}")
    print(f"Intervallo date: {since} -> {until}")
    print(f"Output file: {args.output}")
    print("==========================================")

    posts = get_posts_in_range(since, until)
    save_posts(posts, args.output)

    print("\n[COMPLETATO] Post scaricati con successo!")


if __name__ == "__main__":
    main()
