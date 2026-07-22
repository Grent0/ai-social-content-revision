#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Passo 1: scarica le immagini dei post e le salva in elab-imgevid/contenuti/immagini/<post_id>/.

Compatibile sia con i post IG scaricati da 1_scarica_post.py (media_url/media_type/carousel) sia con i JSON FB
che contengono gli attachment.

Uso rapido:
    python elab-imgevid/1_download_images.py \
        --posts-file output/scaricati/posts_scaricati.json \
        --dest-dir elab-imgevid/contenuti/immagini
"""

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from utils import load_env_from_file, load_posts, resolve_path, write_json_atomic

GRAPH_API_BASE = os.getenv("IG_API_BASE") or os.getenv("FB_API_BASE") or "https://graph.facebook.com/v18.0"
IMGEVID_IMAGES_DIR = os.getenv("IMGEVID_IMAGES_DIR", "elab-imgevid/contenuti/immagini")
IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")
DEFAULT_DEST = IMGEVID_IMAGES_DIR
DEFAULT_REPORT = os.path.join(IMGEVID_REPORT_DIR, "download_images_report.json")
CHUNK = 1024 * 128


def env_or_raise_any(candidates, display_name: str) -> str:
    """
    Restituisce il primo valore disponibile tra le chiavi candidate.
    """
    if isinstance(candidates, str):
        candidates = [candidates]
    for key in candidates:
        val = os.getenv(key)
        if val:
            return val
    names = ", ".join(candidates)
    raise RuntimeError(f"Manca la variabile d'ambiente {display_name} (prova con: {names}). Impostala in .env.")


def is_image_media(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return any(token in mt for token in ("photo", "image"))


def guess_ext(url: str, content_type: Optional[str]) -> str:
    path_ext = Path(url.split("?")[0]).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    if path_ext in allowed:
        return path_ext
    if content_type and content_type.startswith("image/"):
        return f".{content_type.split('/', 1)[1]}"
    return ".jpg"


def fetch_media_url(object_id: str, token: str, allow_thumbnail: bool = True) -> Optional[str]:
    """
    Tenta di recuperare un URL diretto (IG: media_url/thumbnail_url, FB: source/images).
    """
    if not object_id:
        return None

    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{object_id}",
            params={"fields": "media_url,thumbnail_url,source,images", "access_token": token},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("media_url"):
                return data["media_url"]
            if allow_thumbnail and data.get("thumbnail_url"):
                return data["thumbnail_url"]
            if data.get("source"):
                return data["source"]
            images = data.get("images") or []
            if images and images[0].get("source"):
                return images[0]["source"]
    except Exception:
        pass

    return None


def fetch_image_url(object_id: str, token: str) -> Optional[str]:
    """
    Ottiene un URL diretto per l'immagine usando l'API Graph.
    """
    url = fetch_media_url(object_id, token, allow_thumbnail=True)
    if url:
        return url

    # Tentativo extra solo per FB: /picture?redirect=false
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{object_id}/picture",
            params={"type": "large", "redirect": "false", "access_token": token},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            pic_url = (data.get("data") or {}).get("url")
            if pic_url:
                return pic_url
    except Exception:
        pass

    return None


def fetch_ig_children(media_id: str, token: str) -> List[Dict[str, Any]]:
    """
    Se il post è un carosello IG recupera i children (id/media_type/media_url/...).
    """
    if not media_id:
        return []

    children: List[Dict[str, Any]] = []
    url = f"{GRAPH_API_BASE}/{media_id}/children"
    params = {
        "fields": "id,media_type,media_url,thumbnail_url,permalink",
        "limit": 50,
        "access_token": token,
    }

    while url:
        try:
            resp = requests.get(url, params=params if params else None, timeout=20)
            if resp.status_code != 200:
                print(f"[WARN] Impossibile leggere i children IG per {media_id}: HTTP {resp.status_code}")
                break
            data = resp.json()
            children.extend(data.get("data", []))
            paging = data.get("paging", {})
            url = paging.get("next")
            params = {}
        except Exception:
            break

    return children


def collect_image_entries(post: Dict[str, Any], token: str) -> List[Dict[str, Any]]:
    """
    Estrae le entry immagine dal post:
    - Facebook: attachments.data[*].(media_type|type) + target.id
    - Instagram: media_type IMAGE o CAROUSEL_ALBUM (children)
    """
    entries: List[Dict[str, Any]] = []

    attachments = post.get("attachments", {})
    data_list = attachments.get("data", []) if isinstance(attachments, dict) else []
    for att in data_list:
        media_type = att.get("media_type") or att.get("type") or ""
        if not is_image_media(media_type):
            continue
        obj_id = att.get("target", {}).get("id") or att.get("id")
        url = None
        media = att.get("media")
        if isinstance(media, dict):
            url = (media.get("image") or {}).get("src")
        url = url or att.get("url")
        entries.append({"object_id": obj_id, "url": url})

    if entries:
        return entries

    media_type = (post.get("media_type") or "").lower()
    post_id = post.get("id") or f"post_{post.get('post_number')}"

    if media_type == "carousel_album":
        children = (post.get("children") or {}).get("data") or fetch_ig_children(post_id, token)
        for child in children:
            ctype = child.get("media_type") or ""
            if not is_image_media(ctype):
                continue
            entries.append(
                {
                    "object_id": child.get("id"),
                    "url": child.get("media_url") or child.get("thumbnail_url"),
                }
            )
    elif is_image_media(media_type):
        entries.append(
            {
                "object_id": post_id,
                "url": post.get("media_url") or post.get("thumbnail_url"),
            }
        )

    return entries


def download_file(url: str, dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"Content-Type non valido: {content_type or 'n/d'}")
        ext = guess_ext(url, content_type)
        if dest_path.suffix == "":
            dest_path = dest_path.with_suffix(ext)
        with dest_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK):
                if chunk:
                    f.write(chunk)
    return dest_path


def process_posts(args: argparse.Namespace) -> Dict[str, Any]:
    load_env_from_file()
    token = env_or_raise_any(["PAGE_ACCESS_TOKEN", "ACCESS_TOKEN"], "ACCESS_TOKEN/PAGE_ACCESS_TOKEN")
    posts = load_posts(args.posts_file)

    summary = {
        "posts_total": len(posts),
        "posts_with_images": 0,
        "images_attempted": 0,
        "images_downloaded": 0,
        "errors": 0,
    }
    details: List[Dict[str, Any]] = []

    for post in posts:
        image_entries = collect_image_entries(post, token)

        if not image_entries:
            continue

        summary["posts_with_images"] += 1
        post_id = post.get("id") or f"post_{post.get('post_number')}"
        dest_dir = resolve_path(args.dest_dir) / str(post_id)

        for idx, entry in enumerate(image_entries, start=1):
            summary["images_attempted"] += 1
            obj_id = entry.get("object_id") or post.get("id")
            dest = dest_dir / f"image_{idx}"
            url = entry.get("url") or fetch_image_url(obj_id, token)
            if not url:
                summary["errors"] += 1
                details.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "object_id": obj_id,
                        "status": "error",
                        "error": "Nessun URL immagine disponibile",
                    }
                )
                continue
            try:
                final_path = download_file(url, dest)
                summary["images_downloaded"] += 1
                details.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "object_id": obj_id,
                        "status": "downloaded",
                        "dest": str(final_path),
                    }
                )
            except Exception as exc:  # pylint: disable=broad-except
                summary["errors"] += 1
                details.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "object_id": obj_id,
                        "status": "error",
                        "error": str(exc),
                    }
                )

    return {
        "posts_file": str(resolve_path(args.posts_file)),
        "dest_dir": str(resolve_path(args.dest_dir)),
        "timestamp": args.timestamp,
        "summary": summary,
        "details": details,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scarica le immagini dei post (compatibile IG media_url/carousel e FB attachments)."
    )
    parser.add_argument(
        "--posts-file",
        default="output/scaricati/posts_scaricati.json",
        help="File JSON con i post scaricati (contiene gli attachment).",
    )
    parser.add_argument(
        "--dest-dir",
        default=DEFAULT_DEST,
        help="Cartella di destinazione per le immagini scaricate.",
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT,
        help="Percorso del report JSON riassuntivo.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.timestamp = dt.datetime.now().isoformat()
    try:
        report = process_posts(args)
    except RuntimeError as exc:
        print(f"[ERRORE] {exc}")
        sys.exit(1)

    report_path = resolve_path(args.report)
    write_json_atomic(report_path, report)
    s = report["summary"]
    print(f"[INFO] Report scritto in {report_path}")
    print(
        f"[INFO] Post con immagini: {s['posts_with_images']} | scaricate: {s['images_downloaded']}/{s['images_attempted']} | errori: {s['errors']}"
    )


if __name__ == "__main__":
    main()
