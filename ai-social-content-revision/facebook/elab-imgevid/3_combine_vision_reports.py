#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Passo 5: combina i report OCR di immagini e video in un riepilogo unico e può
generare automaticamente:
- report combinato dei download falliti (immagini + video)
- JSON con i post che hanno match (Eliminare/)
- JSON con i post senza match (Modifica/)

Flusso consigliato:
1) python elab-imgevid/1_download_images.py ...  # scarica immagini
2) python elab-imgevid/2_download_videos.py ...  # scarica video
3) python elab-imgevid/3_analyze_images.py ... --output output/elab_imgevid/vision_images.json
4) python elab-imgevid/4_analyze_videos.py ... --output output/elab_imgevid/vision_videos.json
5) python elab-imgevid/5_combine_vision_reports.py  # genera vision_summary.json e gli extra
"""

import argparse
import json
import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set

from utils import load_env_from_file, load_posts, resolve_path, write_json_atomic

IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")
DEFAULT_DOWNLOAD_IMAGES_FAILED = os.path.join(IMGEVID_REPORT_DIR, "download_images_failed.json")
DEFAULT_DOWNLOAD_VIDEOS_FAILED = os.path.join(IMGEVID_REPORT_DIR, "download_videos_failed.json")
DEFAULT_DOWNLOAD_FAILED_OUTPUT = os.path.join(IMGEVID_REPORT_DIR, "download_falliti.json")
DEFAULT_MATCHED_OUTPUT = "Eliminare/matched_posts.json"
DEFAULT_UNMATCHED_OUTPUT = "Modifica/posts_senza_match.json"


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
    parser.add_argument(
        "--download-images-failed",
        default=DEFAULT_DOWNLOAD_IMAGES_FAILED,
        help="Report download immagini falliti (opzionale).",
    )
    parser.add_argument(
        "--download-videos-failed",
        default=DEFAULT_DOWNLOAD_VIDEOS_FAILED,
        help="Report download video falliti (opzionale).",
    )
    parser.add_argument(
        "--download-failed-output",
        default=DEFAULT_DOWNLOAD_FAILED_OUTPUT,
        help="Percorso del report combinato download falliti.",
    )
    parser.add_argument(
        "--matched-output",
        default=DEFAULT_MATCHED_OUTPUT,
        help='Percorso del JSON con i post che hanno match (default: "Eliminare/matched_posts.json").',
    )
    parser.add_argument(
        "--unmatched-output",
        default=DEFAULT_UNMATCHED_OUTPUT,
        help='Percorso del JSON con i post senza match (default: "Modifica/posts_senza_match.json").',
    )
    return parser.parse_args()


def load_optional_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def post_key(post_id: Any, post_number: Any) -> Optional[str]:
    if post_id:
        return str(post_id)
    if post_number is not None:
        return f"post_number:{post_number}"
    return None


def collect_matches(data: Optional[Dict[str, Any]], media_type: str) -> Set[str]:
    matches: Set[str] = set()
    if not data:
        return matches
    if media_type == "image":
        for item in data.get("results", []):
            if not item.get("match"):
                continue
            key = post_key(item.get("post_id"), item.get("post_number"))
            if key:
                matches.add(key)
    elif media_type == "video":
        for item in data.get("results", []):
            has_match = bool(item.get("frames_with_match")) or any(frame.get("match") for frame in item.get("frames", []))
            if not has_match:
                continue
            key = post_key(item.get("post_id"), item.get("post_number"))
            if key:
                matches.add(key)
    return matches


def main() -> None:
    args = parse_args()
    load_env_from_file()

    posts = load_posts(args.posts_file)

    images_path = resolve_path(args.images_report)
    videos_path = resolve_path(args.videos_report)

    images_data = load_optional_json(images_path)
    videos_data = load_optional_json(videos_path)

    def make_post_key(post_id: Optional[str], post_number: Optional[int]) -> Optional[str]:
        if post_id:
            return str(post_id)
        if post_number is None:
            return None
        return f"post_number:{post_number}"

    def ensure_post_entry(
        mapping: Dict[str, Dict[str, Any]],
        post: Dict[str, Any],
        *,
        default_has_image: bool = False,
        default_has_video: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Garantisce la presenza di un record per il post, copiando tutti i campi originali
        e aggiungendo i flag di match Vision.
        """
        key = make_post_key(post.get("id") or post.get("post_id"), post.get("post_number"))
        if key is None:
            return None

        if key not in mapping:
            base = dict(post)
            # Normalizza alias id/post_id per coerenza
            if "post_id" not in base and "id" in base:
                base["post_id"] = base.get("id")
            if "id" not in base and "post_id" in base:
                base["id"] = base.get("post_id")
            base.setdefault("has_image", default_has_image or bool(base.get("has_image")))
            base.setdefault("has_video", default_has_video or bool(base.get("has_video")))

            base.update(
                {
                    "found_in_images": False,
                    "found_in_videos": False,
                    "found_any": False,
                    "images_scanned": 0,
                    "image_matches": 0,
                    "first_image_match": None,
                    "videos_analyzed": 0,
                    "video_matches": 0,
                    "first_video_match": None,
                }
            )
            mapping[key] = base

        return mapping.get(key)

    per_post: Dict[str, Dict[str, Any]] = {}
    for post in posts:
        ensure_post_entry(per_post, post)

    images_available = images_data is not None
    videos_available = videos_data is not None

    if images_available:
        for item in images_data.get("results", []):
            entry = ensure_post_entry(
                per_post,
                {
                    "id": item.get("post_id"),
                    "post_id": item.get("post_id"),
                    "post_number": item.get("post_number"),
                    "has_image": True,
                },
                default_has_image=True,
            )
            if entry is None:
                continue
            entry["has_image"] = entry.get("has_image") or True
            entry["images_scanned"] += 1
            if item.get("match"):
                entry["image_matches"] += 1
                if not entry.get("first_image_match"):
                    entry["first_image_match"] = item.get("image")
            entry["found_in_images"] = entry["found_in_images"] or bool(item.get("match"))

    if videos_available:
        for item in videos_data.get("results", []):
            entry = ensure_post_entry(
                per_post,
                {
                    "id": item.get("post_id"),
                    "post_id": item.get("post_id"),
                    "post_number": item.get("post_number"),
                    "has_video": True,
                },
                default_has_video=True,
            )
            if entry is None:
                continue
            entry["has_video"] = entry.get("has_video") or True
            entry["videos_analyzed"] += 1
            has_video_match = bool(item.get("frames_with_match")) or any(
                frame.get("match") for frame in item.get("frames", [])
            )
            if has_video_match:
                entry["video_matches"] += 1
                if not entry.get("first_video_match"):
                    match_frame = next((f for f in item.get("frames", []) if f.get("match")), None)
                    entry["first_video_match"] = {
                        "video": item.get("video"),
                        "timestamp_sec": match_frame.get("timestamp_sec") if match_frame else None,
                        "frame_index": match_frame.get("frame_index") if match_frame else None,
                    }
            entry["found_in_videos"] = entry["found_in_videos"] or has_video_match

    for entry in per_post.values():
        entry["found_any"] = bool(entry.get("found_in_images") or entry.get("found_in_videos"))

    posts_info = {
        "total": len(per_post),
        "with_images": sum(1 for p in per_post.values() if p.get("has_image")),
        "with_videos": sum(1 for p in per_post.values() if p.get("has_video")),
        "found_in_images": sum(1 for p in per_post.values() if p.get("found_in_images")),
        "found_in_videos": sum(1 for p in per_post.values() if p.get("found_in_videos")),
        "found_any": sum(1 for p in per_post.values() if p.get("found_any")),
    }

    summary = {
        "timestamp": dt.datetime.now().isoformat(),
        "posts_file": str(resolve_path(args.posts_file)),
        "posts": posts_info,
        "images_report": {
            "path": str(images_path),
            "available": images_available,
            "summary": images_data.get("summary") if images_data else None,
            "error": None if images_available else f"Report non trovato in {images_path}",
        },
        "videos_report": {
            "path": str(videos_path),
            "available": videos_available,
            "summary": videos_data.get("summary") if videos_data else None,
            "error": None if videos_available else f"Report non trovato in {videos_path}",
        },
        "phrases": [],
        "phrase": None,
        "case_sensitive": None,
        "language_hints": [],
        "per_post": list(per_post.values()),
    }

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
    print(f"[INFO] Riepilogo con dettaglio post scritto in {output_path}")
    if summary["images_report"]["error"] or summary["videos_report"]["error"]:
        print("[WARN] Alcuni report mancano o non sono leggibili. Vedi il riepilogo per i dettagli.")

    # Report download falliti combinato
    images_failed = load_optional_json(resolve_path(args.download_images_failed))
    videos_failed = load_optional_json(resolve_path(args.download_videos_failed))
    combined_failed_items = []
    if images_failed:
        for item in images_failed.get("items", []):
            combined_failed_items.append({**item, "media_type": "image"})
    if videos_failed:
        for item in videos_failed.get("items", []):
            combined_failed_items.append({**item, "media_type": "video"})
    failed_payload = {
        "images_failed_path": str(resolve_path(args.download_images_failed)),
        "videos_failed_path": str(resolve_path(args.download_videos_failed)),
        "images_errors": len(images_failed.get("items", [])) if images_failed else 0,
        "videos_errors": len(videos_failed.get("items", [])) if videos_failed else 0,
        "total_errors": len(combined_failed_items),
        "items": combined_failed_items,
    }
    failed_output_path = resolve_path(args.download_failed_output)
    write_json_atomic(failed_output_path, failed_payload)
    print(f"[INFO] Report download falliti combinato scritto in {failed_output_path}")

    # Export matched / unmatched post
    matched_keys: Set[str] = set()
    matched_keys |= collect_matches(images_data, "image")
    matched_keys |= collect_matches(videos_data, "video")

    def has_match(post: Dict[str, Any]) -> bool:
        key = post_key(post.get("id"), post.get("post_number"))
        return key in matched_keys if key else False

    matched_posts = [p for p in posts if has_match(p)]
    unmatched_posts = [p for p in posts if not has_match(p)]

    matched_payload = {
        "timestamp": dt.datetime.now().isoformat(),
        "posts_file": str(resolve_path(args.posts_file)),
        "images_report": str(images_path),
        "videos_report": str(videos_path),
        "total_posts_input": len(posts),
        "matched_posts": len(matched_posts),
        "posts": matched_posts,
    }
    matched_output_path = resolve_path(args.matched_output)
    write_json_atomic(matched_output_path, matched_payload)
    print(f"[INFO] Post con match salvati in {matched_output_path}")

    unmatched_payload = {
        "timestamp": dt.datetime.now().isoformat(),
        "posts_file": str(resolve_path(args.posts_file)),
        "images_report": str(images_path),
        "videos_report": str(videos_path),
        "total_posts_input": len(posts),
        "unmatched_posts": len(unmatched_posts),
        "posts": unmatched_posts,
    }
    unmatched_output_path = resolve_path(args.unmatched_output)
    write_json_atomic(unmatched_output_path, unmatched_payload)
    print(f"[INFO] Post senza match salvati in {unmatched_output_path}")


if __name__ == "__main__":
    main()
