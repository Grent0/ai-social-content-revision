#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scarica immagini e video dei post in un unico passaggio.
Produce i report (completi e falliti) come i vecchi script separati.

Uso rapido:
  python elab-imgevid/1_download_media.py \
    --posts-file output/scaricati/posts_scaricati.json

Opzioni:
  --skip-images / --skip-videos per saltare una delle due fasi.
  Percorsi di destinazione e report sono configurabili e hanno default da .env.
"""

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from utils import load_env_from_file, load_posts, resolve_path, write_json_atomic

FB_API_BASE = os.getenv("FB_API_BASE", "https://graph.facebook.com/v18.0")
IMGEVID_IMAGES_DIR = os.getenv("IMGEVID_IMAGES_DIR", "elab-imgevid/contenuti/immagini")
IMGEVID_VIDEOS_DIR = os.getenv("IMGEVID_VIDEOS_DIR", "elab-imgevid/contenuti/video")
IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")

DEFAULT_POSTS_FILE = "output/scaricati/posts_scaricati.json"

# Report di default
DEFAULT_IMAGES_REPORT = os.path.join(IMGEVID_REPORT_DIR, "download_images_report.json")
DEFAULT_IMAGES_FAILED = os.path.join(IMGEVID_REPORT_DIR, "download_images_failed.json")
DEFAULT_VIDEOS_REPORT = os.path.join(IMGEVID_REPORT_DIR, "download_videos_report.json")
DEFAULT_VIDEOS_FAILED = os.path.join(IMGEVID_REPORT_DIR, "download_videos_failed.json")

IMG_CHUNK = 1024 * 128
VID_CHUNK = 1024 * 256


def env_or_raise(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Manca la variabile d'ambiente {key}. Impostala in .env.")
    return value


# -------------------------
# IMMAGINI
# -------------------------

def is_image_media(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return any(token in mt for token in ("photo", "image"))


def guess_image_ext(url: str, content_type: Optional[str]) -> str:
    path_ext = Path(url.split("?")[0]).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    if path_ext in allowed:
        return path_ext
    if content_type and content_type.startswith("image/"):
        return f".{content_type.split('/', 1)[1]}"
    return ".jpg"


def fetch_image_url(object_id: str, token: str) -> Optional[str]:
    base_url = f"{FB_API_BASE}/{object_id}"

    try:
        resp = requests.get(
            base_url, params={"fields": "source,images", "access_token": token}, timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("source"):
                return data["source"]
            images = data.get("images") or []
            if images and images[0].get("source"):
                return images[0]["source"]
    except Exception:
        pass

    try:
        resp = requests.get(
            f"{base_url}/picture",
            params={"type": "large", "redirect": "false", "access_token": token},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            url = (data.get("data") or {}).get("url")
            if url:
                return url
    except Exception:
        pass

    return None


def download_image(url: str, dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"Content-Type non valido: {content_type or 'n/d'}")
        ext = guess_image_ext(url, content_type)
        if dest_path.suffix == "":
            dest_path = dest_path.with_suffix(ext)
        with dest_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=IMG_CHUNK):
                if chunk:
                    f.write(chunk)
    return dest_path


def download_images(posts: List[Dict[str, Any]], dest_dir: Path, report_path: Path, failed_path: Path, token: str) -> None:
    summary = {
        "posts_total": len(posts),
        "posts_with_images": 0,
        "images_attempted": 0,
        "images_downloaded": 0,
        "errors": 0,
    }
    details: List[Dict[str, Any]] = []

    for post in posts:
        attachments = post.get("attachments", {})
        data_list = attachments.get("data", []) if isinstance(attachments, dict) else []
        image_entries: List[Tuple[str, Dict[str, Any]]] = []

        for att in data_list:
            media_type = att.get("media_type") or att.get("type") or ""
            if not is_image_media(media_type):
                continue
            obj_id = att.get("target", {}).get("id") or att.get("id")
            if not obj_id:
                continue
            image_entries.append((obj_id, att))

        if not image_entries:
            continue

        summary["posts_with_images"] += 1
        post_id = post.get("id") or f"post_{post.get('post_number')}"
        post_dir = dest_dir / str(post_id)

        for idx, (obj_id, _) in enumerate(image_entries, start=1):
            summary["images_attempted"] += 1
            dest = post_dir / f"image_{idx}"
            url = fetch_image_url(obj_id, token)
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
                final_path = download_image(url, dest)
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

    report = {
        "posts_file": str(resolve_path(DEFAULT_POSTS_FILE)),
        "dest_dir": str(dest_dir.resolve()),
        "timestamp": dt.datetime.now().isoformat(),
        "summary": summary,
        "details": details,
    }
    write_json_atomic(report_path, report)
    failed_items = [d for d in details if d.get("status") == "error"]
    failed_payload = {
        "timestamp": report["timestamp"],
        "posts_file": report["posts_file"],
        "dest_dir": report["dest_dir"],
        "errors": len(failed_items),
        "items": failed_items,
    }
    write_json_atomic(failed_path, failed_payload)
    s = summary
    print(
        f"[IMG] Post con immagini: {s['posts_with_images']} | scaricate: {s['images_downloaded']}/{s['images_attempted']} | errori: {s['errors']}"
    )
    print(f"[IMG] Report: {report_path}")
    print(f"[IMG] Report errori: {failed_path}")


# -------------------------
# VIDEO
# -------------------------

def is_video_media(media_type: str) -> bool:
    mt = (media_type or "").lower()
    return "video" in mt


def guess_video_ext(url: str, content_type: Optional[str]) -> str:
    path_ext = Path(url.split("?")[0]).suffix.lower()
    allowed = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
    if path_ext in allowed:
        return path_ext
    if content_type and content_type.startswith("video/"):
        return f".{content_type.split('/', 1)[1]}"
    return ".mp4"


def fetch_video_url(object_id: str, token: str) -> Optional[str]:
    url = f"{FB_API_BASE}/{object_id}"
    params = {"fields": "source", "access_token": token}
    try:
        resp = requests.get(url, params=params, timeout=25)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("source")
    except Exception:
        return None


def download_video(url: str, dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=40) as resp:
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if not content_type.startswith("video/"):
            raise ValueError(f"Content-Type non valido: {content_type or 'n/d'}")
        ext = guess_video_ext(url, content_type)
        if dest_path.suffix == "":
            dest_path = dest_path.with_suffix(ext)
        with dest_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=VID_CHUNK):
                if chunk:
                    f.write(chunk)
    return dest_path


def download_videos(posts: List[Dict[str, Any]], dest_dir: Path, report_path: Path, failed_path: Path, token: str) -> None:
    summary = {
        "posts_total": len(posts),
        "posts_with_videos": 0,
        "videos_attempted": 0,
        "videos_downloaded": 0,
        "errors": 0,
    }
    details: List[Dict[str, Any]] = []

    for post in posts:
        attachments = post.get("attachments", {})
        data_list = attachments.get("data", []) if isinstance(attachments, dict) else []
        video_entries: List[Tuple[str, Dict[str, Any]]] = []

        for att in data_list:
            media_type = att.get("media_type") or att.get("type") or ""
            if not is_video_media(media_type):
                continue
            obj_id = att.get("target", {}).get("id") or att.get("id")
            if not obj_id:
                continue
            video_entries.append((obj_id, att))

        if not video_entries:
            continue

        summary["posts_with_videos"] += 1
        post_id = post.get("id") or f"post_{post.get('post_number')}"
        post_dir = dest_dir / str(post_id)

        for idx, (obj_id, att) in enumerate(video_entries, start=1):
            summary["videos_attempted"] += 1
            dest = post_dir / f"video_{idx}"
            url = fetch_video_url(obj_id, token) or att.get("url")
            if not url:
                summary["errors"] += 1
                details.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "object_id": obj_id,
                        "status": "error",
                        "error": "Nessun URL disponibile",
                    }
                )
                continue
            try:
                final_path = download_video(url, dest)
                summary["videos_downloaded"] += 1
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

    report = {
        "posts_file": str(resolve_path(DEFAULT_POSTS_FILE)),
        "dest_dir": str(dest_dir.resolve()),
        "timestamp": dt.datetime.now().isoformat(),
        "summary": summary,
        "details": details,
    }
    write_json_atomic(report_path, report)
    failed_items = [d for d in details if d.get("status") == "error"]
    failed_payload = {
        "timestamp": report["timestamp"],
        "posts_file": report["posts_file"],
        "dest_dir": report["dest_dir"],
        "errors": len(failed_items),
        "items": failed_items,
    }
    write_json_atomic(failed_path, failed_payload)
    s = summary
    print(
        f"[VID] Post con video: {s['posts_with_videos']} | scaricati: {s['videos_downloaded']}/{s['videos_attempted']} | errori: {s['errors']}"
    )
    print(f"[VID] Report: {report_path}")
    print(f"[VID] Report errori: {failed_path}")


# -------------------------
# CLI
# -------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scarica immagini e video dei post.")
    parser.add_argument(
        "--posts-file",
        default=DEFAULT_POSTS_FILE,
        help="File JSON con i post scaricati.",
    )
    parser.add_argument(
        "--images-dest",
        default=IMGEVID_IMAGES_DIR,
        help="Cartella destinazione immagini.",
    )
    parser.add_argument(
        "--videos-dest",
        default=IMGEVID_VIDEOS_DIR,
        help="Cartella destinazione video.",
    )
    parser.add_argument(
        "--images-report",
        default=DEFAULT_IMAGES_REPORT,
        help="Report JSON download immagini.",
    )
    parser.add_argument(
        "--images-failed-report",
        default=DEFAULT_IMAGES_FAILED,
        help="Report JSON download immagini fallite.",
    )
    parser.add_argument(
        "--videos-report",
        default=DEFAULT_VIDEOS_REPORT,
        help="Report JSON download video.",
    )
    parser.add_argument(
        "--videos-failed-report",
        default=DEFAULT_VIDEOS_FAILED,
        help="Report JSON download video falliti.",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Salta il download immagini.",
    )
    parser.add_argument(
        "--skip-videos",
        action="store_true",
        help="Salta il download video.",
    )
    return parser.parse_args()


def main() -> None:
    load_env_from_file()
    args = parse_args()
    token = env_or_raise("PAGE_ACCESS_TOKEN")
    posts = load_posts(args.posts_file)

    if not args.skip_images:
        download_images(
            posts,
            resolve_path(args.images_dest),
            resolve_path(args.images_report),
            resolve_path(args.images_failed_report),
            token,
        )
    else:
        print("[INFO] Skip download immagini")

    if not args.skip_videos:
        download_videos(
            posts,
            resolve_path(args.videos_dest),
            resolve_path(args.videos_report),
            resolve_path(args.videos_failed_report),
            token,
        )
    else:
        print("[INFO] Skip download video")


if __name__ == "__main__":
    main()
