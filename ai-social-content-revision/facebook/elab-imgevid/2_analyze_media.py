#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Analizza immagini e video dei post in un unico passaggio con Google Cloud Vision.
Genera i report separati (vision_images.json / vision_videos.json) e i rispettivi
report di errori, oltre a copiare i media con match nella cartella matches.

Uso rapido:
  python elab-imgevid/2_analyze_media.py --posts-file output/scaricati/posts_scaricati.json

Puoi saltare una fase con --skip-images o --skip-videos.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import shutil
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

try:
    import cv2  # type: ignore
except ModuleNotFoundError:
    cv2 = None  # type: ignore

try:
    from google.cloud import vision  # type: ignore
except ModuleNotFoundError:
    vision = None  # type: ignore

from utils import (
    find_media_files,
    load_env_from_file,
    load_posts,
    normalize_exts,
    phrase_in_text,
    resolve_path,
    write_json_atomic,
)

if TYPE_CHECKING:
    from google.cloud import vision as vision_types
    VisionClient = vision_types.ImageAnnotatorClient
else:
    VisionClient = Any

# Defaults da .env
IMGEVID_IMAGES_DIR = os.getenv("IMGEVID_IMAGES_DIR", "elab-imgevid/contenuti/immagini")
IMGEVID_VIDEOS_DIR = os.getenv("IMGEVID_VIDEOS_DIR", "elab-imgevid/contenuti/video")
IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")
IMGEVID_MATCH_DIR = os.getenv("IMGEVID_MATCH_DIR", "output/elab_imgevid/matches")

DEFAULT_POSTS = "output/scaricati/posts_scaricati.json"
DEFAULT_IMAGES_REPORT = os.path.join(IMGEVID_REPORT_DIR, "vision_images.json")
DEFAULT_VIDEOS_REPORT = os.path.join(IMGEVID_REPORT_DIR, "vision_videos.json")
DEFAULT_IMAGES_FAILED = os.path.join(IMGEVID_REPORT_DIR, "vision_images_failed.json")
DEFAULT_VIDEOS_FAILED = os.path.join(IMGEVID_REPORT_DIR, "vision_videos_failed.json")


def env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "si")


def env_list(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    parts = raw.replace(",", " ").split()
    return [p for p in parts if p]


def env_phrases(img: bool) -> List[str]:
    phrases: List[str] = []

    def _add_from_raw(raw_val: str) -> None:
        raw_val = raw_val.strip()
        if raw_val.startswith("["):
            try:
                data = json.loads(raw_val)
                if isinstance(data, list):
                    phrases.extend([str(x).strip() for x in data if str(x).strip()])
            except Exception:
                pass
        else:
            for chunk in raw_val.split(","):
                val = chunk.strip()
                if val:
                    phrases.append(val)

    keys_multi = ("IMGEVID_IMAGE_PHRASES",) if img else ("IMGEVID_VIDEO_PHRASES",)
    keys_multi += ("IMGEVID_PHRASES",)
    keys_single = ("IMGEVID_IMAGE_PHRASE",) if img else ("IMGEVID_VIDEO_PHRASE",)
    keys_single += ("IMGEVID_PHRASE",)

    for key in keys_multi:
        raw = os.getenv(key)
        if raw:
            _add_from_raw(raw)

    for key in keys_single:
        single = os.getenv(key)
        if single and single.strip():
            phrases.append(single.strip())

    return list(dict.fromkeys([p for p in phrases if p]))


# ---------------------------------------------------------------------------
# Immagini
# ---------------------------------------------------------------------------


def load_existing_report(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def matches_any(text: str, phrases: Sequence[str], case_sensitive: bool) -> bool:
    for ph in phrases:
        if phrase_in_text(text, ph, case_sensitive=case_sensitive):
            return True
    return False


def ensure_vision_client() -> VisionClient:
    if vision is None:
        print("[ERRORE] google-cloud-vision non è installato. Esegui: pip install google-cloud-vision")
        sys.exit(1)
    try:
        return vision.ImageAnnotatorClient()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERRORE] Impossibile creare il client Google Cloud Vision: {exc}")
        print("Verifica le credenziali (GOOGLE_APPLICATION_CREDENTIALS) o il progetto GCP.")
        sys.exit(1)


def detect_text(client: VisionClient, image_path: Path, language_hints: Sequence[str]) -> str:
    with image_path.open("rb") as f:
        content = f.read()

    image = vision.Image(content=content)
    context = vision.ImageContext(language_hints=list(language_hints) if language_hints else None)

    response = client.text_detection(image=image, image_context=context)
    if response.error.message:
        raise RuntimeError(response.error.message)

    full_text = ""
    if getattr(response, "full_text_annotation", None) and response.full_text_annotation.text:
        full_text = response.full_text_annotation.text
    elif response.text_annotations:
        full_text = response.text_annotations[0].description or ""
    return full_text


def trim_text(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit]
    return text


def process_images(args: argparse.Namespace) -> Dict[str, Any]:
    run_started_at = dt.datetime.now()
    run_started_monotonic = time.monotonic()

    posts = load_posts(args.posts_file)
    allowed_exts = normalize_exts(args.img_allowed_exts.split(","))
    client = ensure_vision_client()
    skip_posts_without_image = args.img_skip_without_image
    output_path = resolve_path(args.images_report)
    resume = bool(args.img_resume)
    matches_dir = resolve_path(args.matches_dir) if args.matches_dir else None
    copied_images: Set[str] = set()

    def make_post_key(post: Dict[str, Any]) -> str:
        pid = str(post.get("id") or post.get("post_id") or "")
        pnum = str(post.get("post_number") or "")
        return pid or f"post_number:{pnum}"

    def make_image_key(post: Dict[str, Any], image: Path) -> str:
        pid = str(post.get("id") or post.get("post_id") or "")
        pnum = str(post.get("post_number") or "")
        try:
            ip = image if isinstance(image, Path) else Path(str(image))
            if not ip.is_absolute():
                ip = resolve_path(str(ip))
            image_norm = str(ip.resolve())
        except Exception:
            image_norm = str(image)
        return f"{pid}::{pnum}::{image_norm}"

    existing_report = load_existing_report(output_path) if resume else None
    existing_results = existing_report.get("results") if existing_report else []

    results: List[Dict[str, Any]] = []
    processed_keys: Set[str] = set()
    posts_with_media_set: Set[str] = set()
    posts_with_match_set: Set[str] = set()
    total_images = 0
    images_with_match = 0

    if existing_results:
        results.extend(existing_results)
        for item in existing_results:
            image_key = make_image_key(item, item.get("image"))
            processed_keys.add(image_key)
            post_key = make_post_key(item)
            if post_key:
                posts_with_media_set.add(post_key)
            if item.get("match") and post_key:
                posts_with_match_set.add(post_key)
                images_with_match += 1
            total_images += 1

    for post in posts:
        if skip_posts_without_image and not post.get("has_image"):
            continue

        post_key = make_post_key(post)

        images = find_media_files(
            args.images_dir,
            post,
            allowed_exts=allowed_exts,
            fallback_search=bool(args.img_fallback_search),
            max_files=max(0, args.img_max_images_per_post),
        )
        if not images:
            continue

        post_has_match = False

        for image_path in images:
            image_key = make_image_key(post, image_path)
            if resume and image_key in processed_keys:
                continue

            image_started = time.monotonic()
            total_images += 1
            try:
                full_text = detect_text(client, image_path, args.img_language_hints or [])
                match = matches_any(full_text, args.img_phrases, case_sensitive=args.img_case_sensitive)
                if match:
                    images_with_match += 1
                    post_has_match = True
                    if matches_dir:
                        dest_dir = matches_dir / "images" / str(post.get("id") or post.get("post_number"))
                        try:
                            dest = dest_dir / Path(image_path).name
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            counter = 1
                            while dest.exists():
                                dest = dest_dir / f"{dest.stem}_{counter}{dest.suffix}"
                                counter += 1
                            shutil.copy2(image_path, dest)
                            copied_images.add(str(dest))
                        except Exception:
                            pass

                entry: Dict[str, Any] = {
                    "post_id": post.get("id"),
                    "post_number": post.get("post_number"),
                    "image": str(image_path),
                    "match": match,
                    "full_text_chars": len(full_text),
                    "process_seconds": round(time.monotonic() - image_started, 3),
                }

                should_keep_text = match or args.img_keep_text_non_match
                if should_keep_text:
                    trimmed = trim_text(full_text, args.img_max_text_chars)
                    entry["text"] = trimmed
                    entry["text_truncated"] = bool(
                        args.img_max_text_chars and len(full_text) > args.img_max_text_chars
                    )

                results.append(entry)
            except Exception as exc:  # pylint: disable=broad-except
                results.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "image": str(image_path),
                        "match": False,
                        "process_seconds": round(time.monotonic() - image_started, 3),
                        "error": str(exc),
                    }
                )
            processed_keys.add(image_key)
            if post_key:
                posts_with_media_set.add(post_key)
                if post_has_match:
                    posts_with_match_set.add(post_key)
                    break

    summary = {
        "posts_total": len(posts),
        "posts_with_media": len(posts_with_media_set),
        "posts_with_match": len(posts_with_match_set),
        "images_scanned": total_images,
        "images_with_match": images_with_match,
        "run_started_at": run_started_at.isoformat(),
        "run_ended_at": dt.datetime.now().isoformat(),
        "run_elapsed_seconds": round(time.monotonic() - run_started_monotonic, 3),
    }

    return {
        "phrases": list(args.img_phrases),
        "case_sensitive": args.img_case_sensitive,
        "language_hints": args.img_language_hints or [],
        "posts_file": str(resolve_path(args.posts_file)),
        "images_dir": str(resolve_path(args.images_dir)),
        "timestamp": dt.datetime.now().isoformat(),
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------


def ensure_cv2():
    if cv2 is None:
        print("[ERRORE] opencv-python non è installato. Esegui: pip install opencv-python")
        sys.exit(1)


def detect_text_from_bytes(client: VisionClient, image_bytes: bytes, language_hints: Sequence[str]) -> str:
    image = vision.Image(content=image_bytes)
    context = vision.ImageContext(language_hints=list(language_hints) if language_hints else None)

    response = client.text_detection(image=image, image_context=context)
    if response.error.message:
        raise RuntimeError(response.error.message)

    full_text = ""
    if getattr(response, "full_text_annotation", None) and response.full_text_annotation.text:
        full_text = response.full_text_annotation.text
    elif response.text_annotations:
        full_text = response.text_annotations[0].description or ""
    return full_text


def detect_text_with_retry(
    client: VisionClient,
    image_bytes: bytes,
    language_hints: Sequence[str],
    retries: int = 2,
    base_delay: float = 0.5,
) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return detect_text_from_bytes(client, image_bytes, language_hints)
        except Exception as exc:  # pylint: disable=broad-except
            last_exc = exc
            if attempt < retries:
                time.sleep(base_delay * (attempt + 1))
            else:
                raise
    raise last_exc  # pragma: no cover


def iter_frames(
    video_path: Path, every_n_seconds: float, max_frames: int, resize_width: int
) -> Iterator[Tuple[int, float, bytes]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Impossibile aprire il video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if fps <= 0:
        fps = 25.0

    frame_interval = max(1, int(round(fps * every_n_seconds)))
    frame_idx = 0
    yielded = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            if resize_width and frame.shape[1] > resize_width:
                scale = resize_width / frame.shape[1]
                frame = cv2.resize(frame, (int(frame.shape[1] * scale), int(frame.shape[0] * scale)))
            success, buffer = cv2.imencode(".jpg", frame)
            if not success:
                raise RuntimeError("Impossibile codificare il frame in JPEG")
            yield frame_idx, frame_idx / fps if fps else 0.0, buffer.tobytes()
            yielded += 1
            if max_frames and yielded >= max_frames:
                break

        frame_idx += 1

    cap.release()


def build_run_config(args: argparse.Namespace, allowed_exts: Tuple[str, ...]) -> Dict[str, Any]:
    return {
        "allowed_exts": allowed_exts,
        "frame_every": args.vid_frame_every,
        "max_frames_per_video": args.vid_max_frames_per_video,
        "max_seconds_per_video": args.vid_max_seconds_per_video,
        "max_videos_per_post": args.vid_max_videos_per_post,
        "resize_width": args.vid_resize_width,
        "language_hints": args.vid_language_hints,
        "phrases": list(args.vid_phrases),
        "case_sensitive": args.vid_case_sensitive,
    }


def load_existing_video_report(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def should_resume(existing_report: Optional[Dict[str, Any]], existing_results: List[Dict[str, Any]], run_config: Dict[str, Any]) -> Tuple[bool, str]:
    if not existing_report:
        return True, "Nessun report precedente"
    prev_config = existing_report.get("config") or {}
    for key, val in run_config.items():
        if prev_config.get(key) != val:
            return False, f"Config differente per {key}"
    if not existing_results:
        return True, "Report presente ma senza risultati"
    return True, "Config coerente"


def process_videos(args: argparse.Namespace) -> Dict[str, Any]:
    ensure_cv2()
    client = ensure_vision_client()

    run_started_at = dt.datetime.now()
    run_started_monotonic = time.monotonic()

    posts = load_posts(args.posts_file)
    allowed_exts = normalize_exts(args.vid_allowed_exts.split(","))
    output_path = resolve_path(args.videos_report)
    resume = bool(args.vid_resume)
    run_config = build_run_config(args, allowed_exts)
    matches_dir = resolve_path(args.matches_dir) if args.matches_dir else None
    copied_videos: Set[str] = set()

    existing_report = load_existing_video_report(output_path) if resume else None
    existing_results = existing_report.get("results") if existing_report else []

    if resume:
        resume_ok, reason = should_resume(existing_report, existing_results, run_config)
        if not resume_ok:
            print(f"[INFO] Resume disabilitato: {reason}")
            existing_report = None
            existing_results = []
            resume = False

    results: List[Dict[str, Any]] = []
    processed_keys: Set[str] = set()
    posts_with_media_set: Set[str] = set()
    posts_with_match_set: Set[str] = set()
    total_videos = 0
    videos_with_match = 0
    videos_timed_out = 0
    total_frames = 0
    matched_frames = 0

    def make_post_key(post: Dict[str, Any]) -> str:
        pid = str(post.get("id") or post.get("post_id") or "")
        pnum = str(post.get("post_number") or "")
        return pid or f"post_number:{pnum}"

    def make_video_key(post: Dict[str, Any], video: Path) -> str:
        pid = str(post.get("id") or post.get("post_id") or "")
        pnum = str(post.get("post_number") or "")
        try:
            vp = video if isinstance(video, Path) else Path(str(video))
            if not vp.is_absolute():
                vp = resolve_path(str(vp))
            video_norm = str(vp.resolve())
        except Exception:
            video_norm = str(video)
        return f"{pid}::{pnum}::{video_norm}"

    def copy_video_match(video_path: Path, post_id: Any, post_number: Any) -> None:
        if not matches_dir:
            return
        try:
            dest_dir = matches_dir / "videos" / str(post_id or post_number)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / Path(video_path).name
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{dest.stem}_{counter}{dest.suffix}"
                counter += 1
            shutil.copy2(video_path, dest)
            copied_videos.add(str(video_path))
        except Exception:
            pass

    if existing_results:
        results.extend(existing_results)
        for item in existing_results:
            video_key = make_video_key(item, item.get("video"))
            processed_keys.add(video_key)
            post_key = make_post_key(item)
            if post_key:
                posts_with_media_set.add(post_key)
            frames_with_match = int(item.get("frames_with_match", 0))
            if frames_with_match and post_key:
                posts_with_match_set.add(post_key)
                videos_with_match += 1
                if matches_dir and item.get("video") and str(item.get("video")) not in copied_videos:
                    copy_video_match(Path(item.get("video")), item.get("post_id"), item.get("post_number"))
            total_videos += 1
            total_frames += int(item.get("frames_analyzed", 0))
            matched_frames += frames_with_match

    for post in posts:
        if args.vid_skip_without_video and not post.get("has_video"):
            continue

        post_key = make_post_key(post)

        videos = find_media_files(
            args.videos_dir,
            post,
            allowed_exts=allowed_exts,
            fallback_search=bool(args.vid_fallback_search),
            max_files=max(0, args.vid_max_videos_per_post),
        )
        if not videos:
            continue

        post_has_match = False

        for video_path in videos:
            video_key = make_video_key(post, video_path)
            if resume and video_key in processed_keys:
                continue

            print(
                f"[INFO] Analisi video {video_path} (frame_every={args.vid_frame_every}s, max_frames={args.vid_max_frames_per_video or 'illimitato'})"
            )

            video_started = time.monotonic()
            total_videos += 1
            video_frames = 0
            video_matches = 0
            frame_results: List[Dict[str, Any]] = []
            timed_out = False

            try:
                for frame_idx, timestamp, image_bytes in iter_frames(
                    video_path,
                    every_n_seconds=args.vid_frame_every,
                    max_frames=max(0, args.vid_max_frames_per_video),
                    resize_width=max(0, args.vid_resize_width),
                ):
                    video_frames += 1
                    total_frames += 1

                    try:
                        full_text = detect_text_with_retry(
                            client, image_bytes, args.vid_language_hints or []
                        )
                        match = matches_any(full_text, args.vid_phrases, case_sensitive=args.vid_case_sensitive)
                        if match:
                            video_matches += 1
                            matched_frames += 1
                            post_has_match = True

                        entry: Dict[str, Any] = {
                            "post_id": post.get("id"),
                            "post_number": post.get("post_number"),
                            "video": str(video_path),
                            "frame_index": frame_idx,
                            "timestamp_sec": round(timestamp, 3),
                            "match": match,
                            "full_text_chars": len(full_text),
                        }

                        should_keep_text = match or args.vid_keep_text_non_match
                        if should_keep_text:
                            trimmed = trim_text(full_text, args.vid_max_text_chars)
                            entry["text"] = trimmed
                            entry["text_truncated"] = bool(
                                args.vid_max_text_chars and len(full_text) > args.vid_max_text_chars
                            )

                        frame_results.append(entry)
                        if post_has_match:
                            break
                    except Exception as frame_exc:  # pylint: disable=broad-except
                        frame_results.append(
                            {
                                "post_id": post.get("id"),
                                "post_number": post.get("post_number"),
                                "video": str(video_path),
                                "frame_index": frame_idx,
                                "timestamp_sec": round(timestamp, 3),
                                "match": False,
                                "error": str(frame_exc),
                            }
                        )
                    if post_has_match:
                        break
                    if args.vid_max_seconds_per_video and (time.monotonic() - video_started) >= args.vid_max_seconds_per_video:
                        timed_out = True
                        frame_results.append(
                            {
                                "post_id": post.get("id"),
                                "post_number": post.get("post_number"),
                                "video": str(video_path),
                                "frame_index": frame_idx,
                                "timestamp_sec": round(timestamp, 3),
                                "match": False,
                                "error": f"Timeout video dopo {args.vid_max_seconds_per_video} secondi",
                            }
                        )
                        break
                if video_matches:
                    post_has_match = True
                    videos_with_match += 1
            except Exception as video_exc:  # pylint: disable=broad-except
                frame_results.append(
                    {
                        "post_id": post.get("id"),
                        "post_number": post.get("post_number"),
                        "video": str(video_path),
                        "match": False,
                        "error": str(video_exc),
                    }
                )

            video_elapsed = time.monotonic() - video_started
            print(
                f"[INFO] Completato {video_path}: frame={video_frames}, match={video_matches}, durata={round(video_elapsed, 3)}s{' (timeout)' if timed_out else ''}"
            )
            if timed_out:
                videos_timed_out += 1
            results.append(
                {
                    "post_id": post.get("id"),
                    "post_number": post.get("post_number"),
                    "video": str(video_path),
                    "frames_analyzed": video_frames,
                    "frames_with_match": video_matches,
                    "frame_every_seconds": args.vid_frame_every,
                    "resize_width": args.vid_resize_width,
                    "max_frames_per_video": args.vid_max_frames_per_video,
                    "process_seconds": round(video_elapsed, 3),
                    "timed_out": timed_out,
                    "frames": frame_results,
                }
            )
            processed_keys.add(video_key)
            if post_key:
                posts_with_media_set.add(post_key)
                if video_matches:
                    posts_with_match_set.add(post_key)
                    if matches_dir and str(video_path) not in copied_videos:
                        copy_video_match(video_path, post.get("id"), post.get("post_number"))

        if post_has_match:
            continue

    summary = {
        "posts_total": len(posts),
        "posts_with_media": len(posts_with_media_set),
        "posts_with_match": len(posts_with_match_set),
        "videos_analyzed": total_videos,
        "videos_with_match": videos_with_match,
        "videos_timed_out": videos_timed_out,
        "frames_analyzed": total_frames,
        "frames_with_match": matched_frames,
        "run_started_at": run_started_at.isoformat(),
        "run_ended_at": dt.datetime.now().isoformat(),
        "run_elapsed_seconds": round(time.monotonic() - run_started_monotonic, 3),
    }

    return {
        "phrases": list(args.vid_phrases),
        "case_sensitive": args.vid_case_sensitive,
        "language_hints": args.vid_language_hints or [],
        "posts_file": str(resolve_path(args.posts_file)),
        "videos_dir": str(resolve_path(args.videos_dir)),
        "timestamp": dt.datetime.now().isoformat(),
        "config": run_config,
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    load_env_from_file()
    # Immagini defaults
    img_langs = env_list("IMGEVID_LANGUAGE_HINTS", ["it"])
    img_allowed = os.getenv("IMGEVID_ALLOWED_IMAGE_EXTS", "jpg,jpeg,png,webp,bmp")
    img_max_images = int(os.getenv("IMGEVID_MAX_IMAGES_PER_POST", "0"))
    img_max_chars = int(os.getenv("IMGEVID_MAX_TEXT_CHARS_IMAGE", "1200"))
    img_keep_text = env_bool("IMGEVID_KEEP_TEXT_NON_MATCH", False)
    img_case_sensitive = env_bool("IMGEVID_CASE_SENSITIVE", False)
    img_phrases = env_phrases(img=True)
    img_skip_without = env_bool("IMGEVID_SKIP_POSTS_WITHOUT_IMAGE", True)
    img_resume = env_bool("IMGEVID_RESUME_IMAGES", True)

    # Video defaults
    vid_langs = env_list("IMGEVID_LANGUAGE_HINTS", ["it"])
    vid_allowed = os.getenv("IMGEVID_ALLOWED_VIDEO_EXTS", "mp4,mov,m4v,avi,mkv")
    vid_frame_every = float(os.getenv("IMGEVID_FRAME_EVERY", "2.0"))
    vid_max_frames = int(os.getenv("IMGEVID_MAX_FRAMES_PER_VIDEO", "40"))
    vid_max_seconds = float(os.getenv("IMGEVID_MAX_SECONDS_PER_VIDEO", "0"))
    vid_max_videos = int(os.getenv("IMGEVID_MAX_VIDEOS_PER_POST", "0"))
    vid_resize = int(os.getenv("IMGEVID_RESIZE_WIDTH", "1280"))
    vid_max_chars = int(os.getenv("IMGEVID_MAX_TEXT_CHARS_VIDEO", "800"))
    vid_keep_text = env_bool("IMGEVID_KEEP_TEXT_NON_MATCH", False)
    vid_case_sensitive = env_bool("IMGEVID_CASE_SENSITIVE", False)
    vid_phrases = env_phrases(img=False)
    vid_skip_without = env_bool("IMGEVID_SKIP_POSTS_WITHOUT_VIDEO", True)
    vid_resume = env_bool("IMGEVID_RESUME_VIDEOS", True)

    parser = argparse.ArgumentParser(
        description="Analizza immagini e video con Vision in un unico passaggio."
    )
    parser.add_argument(
        "--posts-file",
        default=DEFAULT_POSTS,
        help="File JSON con i post scaricati.",
    )
    parser.add_argument(
        "--images-dir",
        default=IMGEVID_IMAGES_DIR,
        help="Cartella immagini.",
    )
    parser.add_argument(
        "--videos-dir",
        default=IMGEVID_VIDEOS_DIR,
        help="Cartella video.",
    )
    parser.add_argument(
        "--images-report",
        default=DEFAULT_IMAGES_REPORT,
        help="Report OCR immagini.",
    )
    parser.add_argument(
        "--videos-report",
        default=DEFAULT_VIDEOS_REPORT,
        help="Report OCR video.",
    )
    parser.add_argument(
        "--images-failed-report",
        default=DEFAULT_IMAGES_FAILED,
        help="Report analisi immagini fallite.",
    )
    parser.add_argument(
        "--videos-failed-report",
        default=DEFAULT_VIDEOS_FAILED,
        help="Report analisi video fallite.",
    )
    parser.add_argument(
        "--matches-dir",
        default=IMGEVID_MATCH_DIR,
        help="Cartella dove copiare media con match (images/videos). Vuota per disabilitare.",
    )
    parser.add_argument("--skip-images", action="store_true", help="Salta analisi immagini.")
    parser.add_argument("--skip-videos", action="store_true", help="Salta analisi video.")

    # Opzioni immagini
    parser.add_argument("--img-allowed-exts", default=img_allowed, help="Estensioni immagini ammesse.")
    parser.add_argument("--img-language-hints", nargs="*", default=img_langs, help="Language hints immagini.")
    parser.add_argument("--img-max-images-per-post", type=int, default=img_max_images, help="Limite immagini per post (0 = no).")
    parser.add_argument("--img-max-text-chars", type=int, default=img_max_chars, help="Max caratteri testo OCR (0 = no).")
    parser.add_argument("--img-keep-text-non-match", action="store_true", default=img_keep_text, help="Salva testo OCR anche senza match.")
    parser.add_argument("--img-case-sensitive", action="store_true", default=img_case_sensitive, help="Ricerca case-sensitive.")
    parser.add_argument("--img-fallback-search", action="store_true", default=True, help="Ricerca ricorsiva se manca la cartella.")
    parser.add_argument("--img-no-fallback-search", dest="img_fallback_search", action="store_false", help="Disabilita fallback search.")
    parser.add_argument("--img-resume", action="store_true", default=img_resume, help="Resume immagini.")
    parser.add_argument("--img-no-resume", dest="img_resume", action="store_false", help="Disabilita resume immagini.")
    parser.add_argument("--img-skip-without-image", action="store_true", dest="img_skip_without_image", default=img_skip_without, help="Salta post senza flag has_image.")
    parser.add_argument("--img-include-without-image", action="store_false", dest="img_skip_without_image", help="Analizza anche se has_image è False.")
    parser.add_argument("--img-phrase", action="append", help="Frase da cercare (ripetibile).")
    parser.add_argument("--img-phrases", nargs="+", help="Frasi da cercare (lista).")

    # Opzioni video
    parser.add_argument("--vid-allowed-exts", default=vid_allowed, help="Estensioni video ammesse.")
    parser.add_argument("--vid-language-hints", nargs="*", default=vid_langs, help="Language hints video.")
    parser.add_argument("--vid-frame-every", type=float, default=vid_frame_every, help="Intervallo tra frame (s).")
    parser.add_argument("--vid-max-frames-per-video", type=int, default=vid_max_frames, help="Limite frame per video (0 = no).")
    parser.add_argument("--vid-max-seconds-per-video", type=float, default=vid_max_seconds, help="Timeout per video (0 = no).")
    parser.add_argument("--vid-max-videos-per-post", type=int, default=vid_max_videos, help="Limite video per post (0 = no).")
    parser.add_argument("--vid-resize-width", type=int, default=vid_resize, help="Ridimensiona frame se più larghi (0 = no).")
    parser.add_argument("--vid-max-text-chars", type=int, default=vid_max_chars, help="Max caratteri testo OCR (0 = no).")
    parser.add_argument("--vid-keep-text-non-match", action="store_true", default=vid_keep_text, help="Salva testo OCR anche senza match.")
    parser.add_argument("--vid-case-sensitive", action="store_true", default=vid_case_sensitive, help="Ricerca case-sensitive.")
    parser.add_argument("--vid-fallback-search", action="store_true", default=True, help="Ricerca ricorsiva se manca la cartella.")
    parser.add_argument("--vid-no-fallback-search", dest="vid_fallback_search", action="store_false", help="Disabilita fallback search.")
    parser.add_argument("--vid-resume", action="store_true", default=vid_resume, help="Resume video.")
    parser.add_argument("--vid-no-resume", dest="vid_resume", action="store_false", help="Disabilita resume video.")
    parser.add_argument("--vid-skip-without-video", action="store_true", dest="vid_skip_without_video", default=vid_skip_without, help="Salta post senza flag has_video.")
    parser.add_argument("--vid-include-without-video", action="store_false", dest="vid_skip_without_video", help="Analizza anche se has_video è False.")
    parser.add_argument("--vid-phrase", action="append", help="Frase da cercare (ripetibile).")
    parser.add_argument("--vid-phrases", nargs="+", help="Frasi da cercare (lista).")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    img_phrases: List[str] = []
    img_phrases.extend(env_phrases(img=True))
    if args.img_phrase:
        img_phrases.extend([p for p in args.img_phrase if p])
    if args.img_phrases:
        img_phrases.extend([p for p in args.img_phrases if p])
    args.img_phrases = [p.strip() for p in img_phrases if p and p.strip()]

    vid_phrases: List[str] = []
    vid_phrases.extend(env_phrases(img=False))
    if args.vid_phrase:
        vid_phrases.extend([p for p in args.vid_phrase if p])
    if args.vid_phrases:
        vid_phrases.extend([p for p in args.vid_phrases if p])
    args.vid_phrases = [p.strip() for p in vid_phrases if p and p.strip()]

    if not args.img_phrases and not args.skip_images:
        print("[ERRORE] Nessuna frase per le immagini. Usa --img-phrase/--img-phrases o IMGEVID_IMAGE_PHRASE/PHRASES.")
        sys.exit(1)
    if not args.vid_phrases and not args.skip_videos:
        print("[ERRORE] Nessuna frase per i video. Usa --vid-phrase/--vid-phrases o IMGEVID_VIDEO_PHRASE/PHRASES.")
        sys.exit(1)

    if not args.skip_images:
        img_report = process_images(args)
        out_img = resolve_path(args.images_report)
        write_json_atomic(out_img, img_report)
        print(f"[INFO] Report immagini scritto in {out_img}")
        print(f"[INFO] Immagini elaborate: {img_report['summary']['images_scanned']}")
        print(f"[INFO] Immagini con match: {img_report['summary']['images_with_match']}")

        failed_items = [item for item in img_report["results"] if item.get("error")]
        failed_payload = {
            "timestamp": img_report["timestamp"],
            "posts_file": img_report["posts_file"],
            "images_dir": img_report["images_dir"],
            "errors": len(failed_items),
            "items": failed_items,
        }
        failed_path = resolve_path(args.images_failed_report)
        write_json_atomic(failed_path, failed_payload)
        print(f"[INFO] Report analisi immagini fallite scritto in {failed_path}")
    else:
        print("[INFO] Skip analisi immagini")

    if not args.skip_videos:
        vid_report = process_videos(args)
        out_vid = resolve_path(args.videos_report)
        write_json_atomic(out_vid, vid_report)
        print(f"[INFO] Report video scritto in {out_vid}")
        print(f"[INFO] Frame analizzati: {vid_report['summary']['frames_analyzed']}")
        print(f"[INFO] Frame con match: {vid_report['summary']['frames_with_match']}")

        failed_items: List[Dict[str, Any]] = []
        for item in vid_report["results"]:
            if item.get("error"):
                failed_items.append(
                    {
                        "post_id": item.get("post_id"),
                        "post_number": item.get("post_number"),
                        "video": item.get("video"),
                        "error": item.get("error"),
                        "level": "video",
                    }
                )
            if item.get("timed_out"):
                failed_items.append(
                    {
                        "post_id": item.get("post_id"),
                        "post_number": item.get("post_number"),
                        "video": item.get("video"),
                        "error": f"Timeout video dopo {item.get('process_seconds')}s",
                        "level": "video",
                    }
                )
            for frame in item.get("frames", []):
                if frame.get("error"):
                    failed_items.append(
                        {
                            "post_id": frame.get("post_id"),
                            "post_number": frame.get("post_number"),
                            "video": frame.get("video"),
                            "frame_index": frame.get("frame_index"),
                            "timestamp_sec": frame.get("timestamp_sec"),
                            "error": frame.get("error"),
                            "level": "frame",
                        }
                    )

        failed_payload = {
            "timestamp": vid_report["timestamp"],
            "posts_file": vid_report["posts_file"],
            "videos_dir": vid_report["videos_dir"],
            "errors": len(failed_items),
            "items": failed_items,
        }
        failed_path = resolve_path(args.videos_failed_report)
        write_json_atomic(failed_path, failed_payload)
        print(f"[INFO] Report analisi video fallite scritto in {failed_path}")
    else:
        print("[INFO] Skip analisi video")


if __name__ == "__main__":
    main()
