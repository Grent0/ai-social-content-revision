#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Passo 5: combina i report OCR di immagini e video in un riepilogo unico.

Flusso consigliato:
1) python elab-imgevid/1_download_images.py ...  # scarica immagini
2) python elab-imgevid/2_download_videos.py ...  # scarica video
3) python elab-imgevid/3_analyze_images.py ... --output output/elab_imgevid/vision_images.json
4) python elab-imgevid/4_analyze_videos.py ... --output output/elab_imgevid/vision_videos.json
5) python elab-imgevid/5_combine_vision_reports.py  # genera vision_summary.json
"""

import argparse
import json
import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict, Optional

from utils import load_env_from_file, load_posts, resolve_path, write_json_atomic

IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")


def parse_args() -> argparse.Namespace:
    default_images = os.path.join(IMGEVID_REPORT_DIR, "vision_images.json")
    default_videos = os.path.join(IMGEVID_REPORT_DIR, "vision_videos.json")
    default_summary = os.path.join(IMGEVID_REPORT_DIR, "vision_summary.json")

    parser = argparse.ArgumentParser(
        description="Combina i report Vision (immagini e video) in un riepilogo unico."
    )
    parser.add_argument(
        "--posts-file",
        default="output/scaricati/posts_scaricati.json",
        help="File JSON con i post scaricati.",
    )
    parser.add_argument(
        "--images-report",
        default=default_images,
        help="Report OCR immagini.",
    )
    parser.add_argument(
        "--videos-report",
        default=default_videos,
        help="Report OCR video.",
    )
    parser.add_argument(
        "--output",
        default=default_summary,
        help="Percorso del riepilogo combinato.",
    )
    return parser.parse_args()


def load_optional_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def main() -> None:
    args = parse_args()
    load_env_from_file()

    posts = load_posts(args.posts_file)
    posts_info = {
        "total": len(posts),
        "with_images": sum(1 for p in posts if p.get("has_image")),
        "with_videos": sum(1 for p in posts if p.get("has_video")),
    }

    images_path = resolve_path(args.images_report)
    videos_path = resolve_path(args.videos_report)

    images_data = load_optional_json(images_path)
    videos_data = load_optional_json(videos_path)

    summary = {
        "timestamp": dt.datetime.now().isoformat(),
        "posts_file": str(resolve_path(args.posts_file)),
        "posts": posts_info,
        "images_report": {
            "path": str(images_path),
            "available": images_data is not None,
            "summary": images_data.get("summary") if images_data else None,
            "error": None if images_data else f"Report non trovato in {images_path}",
        },
        "videos_report": {
            "path": str(videos_path),
            "available": videos_data is not None,
            "summary": videos_data.get("summary") if videos_data else None,
            "error": None if videos_data else f"Report non trovato in {videos_path}",
        },
        "phrases": [],
        "phrase": None,
        "case_sensitive": None,
        "language_hints": [],
    }

    # Propaga info frase/hints se presenti in uno dei report
    for data in (images_data, videos_data):
        if not data:
            continue
        phrases = data.get("phrases") or []
        if phrases and not summary["phrases"]:
            summary["phrases"] = phrases
        summary["phrase"] = summary["phrase"] or data.get("phrase")
        summary["case_sensitive"] = (
            summary["case_sensitive"] if summary["case_sensitive"] is not None else data.get("case_sensitive")
        )
        hints = data.get("language_hints") or []
        if hints and not summary["language_hints"]:
            summary["language_hints"] = hints

    output_path = resolve_path(args.output)
    write_json_atomic(output_path, summary)
    print(f"[INFO] Riepilogo scritto in {output_path}")
    if summary["images_report"]["error"] or summary["videos_report"]["error"]:
        print("[WARN] Alcuni report mancano o non sono leggibili. Vedi il riepilogo per i dettagli.")


if __name__ == "__main__":
    main()
