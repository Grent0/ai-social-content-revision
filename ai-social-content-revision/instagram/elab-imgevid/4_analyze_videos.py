#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Passo 4: estrae frame dai video dei post, esegue OCR con Google Cloud Vision e cerca una frase.

Uso rapido:
    python elab-imgevid/4_analyze_videos.py \
        --phrase "testo da cercare" \
        --videos-dir elab-imgevid/contenuti/video \
        --posts-file output/scaricati/posts_scaricati.json
"""

import argparse
import datetime as dt
import os
import sys
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

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

IMGEVID_VIDEOS_DIR = os.getenv("IMGEVID_VIDEOS_DIR", "elab-imgevid/contenuti/video")
IMGEVID_REPORT_DIR = os.getenv("IMGEVID_REPORT_DIR", "output/elab_imgevid")


def env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_list(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    parts = raw.replace(",", " ").split()
    return [p for p in parts if p]


def env_phrases() -> List[str]:
    """
    Legge le frasi per video (VIDEO_PHRASES/VIDEO_PHRASE) con fallback alle comuni
    (PHRASES/PHRASE) e mantiene la compatibilità con i vecchi IMGEVID_*.
    Supporta JSON list o comma-separated. Conserva le frasi con spazi.
    """
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

    for key in ("VIDEO_PHRASES", "PHRASES", "IMGEVID_VIDEO_PHRASES", "IMGEVID_PHRASES"):
        raw = os.getenv(key)
        if raw:
            _add_from_raw(raw)

    for key in ("VIDEO_PHRASE", "PHRASE", "IMGEVID_VIDEO_PHRASE", "IMGEVID_PHRASE"):
        single = os.getenv(key)
        if single and single.strip():
            phrases.append(single.strip())

    # dedupe preservando l'ordine
    return list(dict.fromkeys(phrases))


def _normalize_seq(value: Any) -> Tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return tuple(str(x).strip() for x in value if str(x).strip())


def normalize_config_for_compare(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizza la config per confrontare run diversi (usato per resume).
    """
    allowed_exts = config.get("allowed_exts") or config.get("allowed_exts_normalized") or ()
    if allowed_exts and isinstance(allowed_exts, str):
        allowed_exts = allowed_exts.split(",")
    normalized_exts = tuple(normalize_exts(allowed_exts))

    return {
        "posts_file": str(config.get("posts_file") or ""),
        "videos_dir": str(config.get("videos_dir") or ""),
        "frame_every": float(config.get("frame_every") or 0),
        "max_frames_per_video": int(config.get("max_frames_per_video") or 0),
        "max_videos_per_post": int(config.get("max_videos_per_post") or 0),
        "resize_width": int(config.get("resize_width") or 0),
        "language_hints": _normalize_seq(config.get("language_hints")),
        "allowed_exts": normalized_exts,
        "phrases": _normalize_seq(config.get("phrases")),
        "case_sensitive": bool(config.get("case_sensitive")),
        "keep_text_non_match": bool(config.get("keep_text_non_match")),
        "skip_posts_without_video": bool(config.get("skip_posts_without_video")),
        "fallback_search": bool(config.get("fallback_search")),
        "max_seconds_per_video": float(config.get("max_seconds_per_video") or 0),
        "max_text_chars": int(config.get("max_text_chars") or 0),
    }


def build_run_config(args: argparse.Namespace, allowed_exts: Tuple[str, ...]) -> Dict[str, Any]:
    """
    Costruisce uno snapshot della configurazione corrente da salvare nel report.
    """
    return {
        "posts_file": str(resolve_path(args.posts_file)),
        "videos_dir": str(resolve_path(args.videos_dir)),
        "frame_every": float(args.frame_every),
        "max_frames_per_video": int(args.max_frames_per_video),
        "max_videos_per_post": int(args.max_videos_per_post),
        "resize_width": int(args.resize_width),
        "language_hints": list(args.language_hints or []),
        "allowed_exts": list(allowed_exts),
        "phrases": list(args.phrases or []),
        "case_sensitive": bool(args.case_sensitive),
        "keep_text_non_match": bool(args.keep_text_non_match),
        "skip_posts_without_video": bool(args.skip_posts_without_video),
        "fallback_search": bool(args.fallback_search),
        "max_seconds_per_video": float(args.max_seconds_per_video),
        "max_text_chars": int(args.max_text_chars),
    }


def should_resume(
    existing_report: Optional[Dict[str, Any]],
    existing_results: List[Dict[str, Any]],
    current_config: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """
    Decide se è sicuro fare resume confrontando la config salvata col run attuale.
    """
    if not existing_report:
        return True, None

    prev_config = existing_report.get("config") if isinstance(existing_report, dict) else None
    if not prev_config:
        return False, "config precedente assente"

    prev_norm = normalize_config_for_compare(prev_config)
    current_norm = normalize_config_for_compare(current_config)

    for key in current_norm.keys():
        if prev_norm.get(key) != current_norm.get(key):
            prev_val = prev_norm.get(key)
            curr_val = current_norm.get(key)
            reason = f"{key} differente (precedente={prev_val}, nuovo={curr_val})"
            return False, reason

    return True, None


def load_existing_report(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def validate_inputs(args: argparse.Namespace, allowed_exts: Tuple[str, ...]) -> None:
    if args.frame_every <= 0:
        print("[ERRORE] --frame-every deve essere > 0")
        sys.exit(1)
    if args.max_frames_per_video < 0:
        print("[ERRORE] --max-frames-per-video deve essere >= 0")
        sys.exit(1)
    if args.max_videos_per_post < 0:
        print("[ERRORE] --max-videos-per-post deve essere >= 0")
        sys.exit(1)
    if args.max_seconds_per_video < 0:
        print("[ERRORE] --max-seconds-per-video deve essere >= 0")
        sys.exit(1)
    if not allowed_exts:
        print("[ERRORE] --allowed-exts non può essere vuoto")
        sys.exit(1)

    posts_path = resolve_path(args.posts_file)
    videos_path = resolve_path(args.videos_dir)
    if not posts_path.exists():
        print(f"[ERRORE] File post non trovato: {posts_path}")
        sys.exit(1)
    if not videos_path.exists():
        print(f"[ERRORE] Cartella video non trovata: {videos_path}")
        sys.exit(1)


def matches_any(text: str, phrases: Sequence[str], case_sensitive: bool) -> bool:
    for ph in phrases:
        if phrase_in_text(text, ph, case_sensitive=case_sensitive):
            return True
    return False


def ensure_cv2():
    if cv2 is None:
        print("[ERRORE] opencv-python non è installato. Esegui:")
        print("  pip install opencv-python")
        sys.exit(1)


def ensure_vision_client():
    if vision is None:
        print("[ERRORE] google-cloud-vision non è installato. Esegui:")
        print("  pip install google-cloud-vision")
        sys.exit(1)

    try:
        return vision.ImageAnnotatorClient()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERRORE] Impossibile creare il client Google Cloud Vision: {exc}")
        print("Verifica le credenziali (GOOGLE_APPLICATION_CREDENTIALS) o il progetto GCP.")
        sys.exit(1)


def detect_text_from_bytes(
    client: "vision.ImageAnnotatorClient", image_bytes: bytes, language_hints: Sequence[str]
) -> str:
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
    client: "vision.ImageAnnotatorClient",
    image_bytes: bytes,
    language_hints: Sequence[str],
    retries: int = 2,
    base_delay: float = 0.5,
) -> str:
    """
    Tenta l'OCR con piccoli retry su errori transienti.
    """
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
                new_size = (int(frame.shape[1] * scale), int(frame.shape[0] * scale))
                frame = cv2.resize(frame, new_size)

            success, buffer = cv2.imencode(".jpg", frame)
            if not success:
                raise RuntimeError(f"Impossibile codificare il frame {frame_idx}")

            timestamp_sec = frame_idx / fps if fps else 0.0
            yield frame_idx, timestamp_sec, bytes(buffer)
            yielded += 1

            if max_frames and yielded >= max_frames:
                break

        frame_idx += 1

    cap.release()


def trim_text(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit]
    return text


def process_posts(args: argparse.Namespace) -> Dict[str, Any]:
    load_env_from_file()
    allowed_exts = normalize_exts(args.allowed_exts.split(","))
    skip_posts_without_video = args.skip_posts_without_video
    output_path = resolve_path(args.output)
    resume = bool(args.resume)
    run_config = build_run_config(args, allowed_exts)
    validate_inputs(args, allowed_exts)

    ensure_cv2()
    client = ensure_vision_client()

    run_started_at = dt.datetime.now()
    run_started_monotonic = time.monotonic()

    posts = load_posts(args.posts_file)

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

    existing_report = load_existing_report(output_path) if resume else None
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
            total_videos += 1
            total_frames += int(item.get("frames_analyzed", 0))
            matched_frames += frames_with_match

    for post in posts:
        # Evita falsi positivi dovuti al fallback su file con numeri simili:
        # se il post non ha flag video, salta (configurabile via CLI/env).
        if skip_posts_without_video and not post.get("has_video"):
            continue

        post_key = make_post_key(post)

        videos = find_media_files(
            args.videos_dir,
            post,
            allowed_exts=allowed_exts,
            fallback_search=bool(args.fallback_search),
            max_files=max(0, args.max_videos_per_post),
        )
        if not videos:
            continue

        post_matches = 0
        post_has_match = False

        for video_path in videos:
            video_key = make_video_key(post, video_path)
            if resume and video_key in processed_keys:
                continue

            print(
                f"[INFO] Analisi video {video_path} "
                f"(frame_every={args.frame_every}s, max_frames={args.max_frames_per_video or 'illimitato'})"
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
                    every_n_seconds=args.frame_every,
                    max_frames=max(0, args.max_frames_per_video),
                    resize_width=max(0, args.resize_width),
                ):
                    video_frames += 1
                    total_frames += 1

                    try:
                        full_text = detect_text_with_retry(
                            client, image_bytes, args.language_hints or []
                        )
                        match = matches_any(full_text, args.phrases, case_sensitive=args.case_sensitive)
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

                        should_keep_text = match or args.keep_text_non_match
                        if should_keep_text:
                            trimmed = trim_text(full_text, args.max_text_chars)
                            entry["text"] = trimmed
                            entry["text_truncated"] = bool(
                                args.max_text_chars and len(full_text) > args.max_text_chars
                            )

                        frame_results.append(entry)
                        if post_has_match:
                            # Stop analisi frame e passa al video successivo/post successivo
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
                    if args.max_seconds_per_video and (
                        time.monotonic() - video_started
                    ) >= args.max_seconds_per_video:
                        timed_out = True
                        frame_results.append(
                            {
                                "post_id": post.get("id"),
                                "post_number": post.get("post_number"),
                                "video": str(video_path),
                                "frame_index": frame_idx,
                                "timestamp_sec": round(timestamp, 3),
                                "match": False,
                                "error": f"Timeout video dopo {args.max_seconds_per_video} secondi",
                            }
                        )
                        break
                if video_matches:
                    post_matches += 1
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
                f"[INFO] Completato {video_path}: frame={video_frames}, match={video_matches}, "
                f"durata={round(video_elapsed, 3)}s{' (timeout)' if timed_out else ''}"
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
                    "frame_every_seconds": args.frame_every,
                    "resize_width": args.resize_width,
                    "max_frames_per_video": args.max_frames_per_video,
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

        if post_has_match:
            # Stop elaborazione su altri video di questo post
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
        "phrases": list(args.phrases),
        "case_sensitive": args.case_sensitive,
        "language_hints": args.language_hints or [],
        "posts_file": str(resolve_path(args.posts_file)),
        "videos_dir": str(resolve_path(args.videos_dir)),
        "timestamp": dt.datetime.now().isoformat(),
        "config": run_config,
        "summary": summary,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    load_env_from_file()
    default_langs = env_list("IMGEVID_LANGUAGE_HINTS", ["it"])
    default_allowed = os.getenv("IMGEVID_ALLOWED_VIDEO_EXTS", "mp4,mov,m4v,avi,mkv")
    default_output = os.path.join(IMGEVID_REPORT_DIR, "vision_videos.json")
    default_frame_every = float(os.getenv("IMGEVID_FRAME_EVERY", "2.0"))
    default_max_frames = int(os.getenv("IMGEVID_MAX_FRAMES_PER_VIDEO", "40"))
    default_max_seconds_per_video = float(os.getenv("IMGEVID_MAX_SECONDS_PER_VIDEO", "0"))
    default_max_videos = int(os.getenv("IMGEVID_MAX_VIDEOS_PER_POST", "0"))
    default_resize = int(os.getenv("IMGEVID_RESIZE_WIDTH", "1280"))
    default_max_chars = int(os.getenv("IMGEVID_MAX_TEXT_CHARS_VIDEO", "800"))
    default_keep_text = env_bool("IMGEVID_KEEP_TEXT_NON_MATCH", False)
    default_case_sensitive = env_bool("IMGEVID_CASE_SENSITIVE", False)
    default_phrases = env_phrases()
    default_skip_without_video = env_bool("IMGEVID_SKIP_POSTS_WITHOUT_VIDEO", True)
    default_resume = env_bool("IMGEVID_RESUME_VIDEOS", True)

    parser = argparse.ArgumentParser(
        description="Estrae frame dai video, esegue OCR e cerca una frase con Google Cloud Vision."
    )
    parser.add_argument(
        "--phrase",
        action="append",
        help="Frase da cercare (puoi ripetere il flag). Se omessa usa IMGEVID_PHRASES/IMGEVID_PHRASE.",
    )
    parser.add_argument(
        "--phrases",
        nargs="+",
        help="Lista di frasi da cercare (separale con spazio).",
    )
    parser.add_argument(
        "--posts-file",
        default="output/scaricati/posts_scaricati.json",
        help="File JSON con i post scaricati.",
    )
    parser.add_argument(
        "--videos-dir",
        default=IMGEVID_VIDEOS_DIR,
        help="Cartella dove si trovano i video (sottocartelle per post_id o post_number).",
    )
    parser.add_argument(
        "--output",
        default=default_output,
        help="Percorso del report JSON.",
    )
    parser.add_argument(
        "--allowed-exts",
        default=default_allowed,
        help="Estensioni video ammesse, separate da virgola.",
    )
    parser.add_argument(
        "--frame-every",
        dest="frame_every",
        type=float,
        default=default_frame_every,
        help="Intervallo (secondi) tra un frame e il successivo da analizzare.",
    )
    parser.add_argument(
        "--max-frames-per-video",
        type=int,
        default=default_max_frames,
        help="Limite frame per video (0 = nessun limite).",
    )
    parser.add_argument(
        "--max-seconds-per-video",
        type=float,
        default=default_max_seconds_per_video,
        help="Timeout per singolo video in secondi (0 = nessun timeout).",
    )
    parser.add_argument(
        "--max-videos-per-post",
        type=int,
        default=default_max_videos,
        help="Limite video per post (0 = nessun limite).",
    )
    parser.add_argument(
        "--resize-width",
        type=int,
        default=default_resize,
        help="Ridimensiona i frame se la larghezza supera questo valore (0 = nessun resize).",
    )
    parser.add_argument(
        "--language-hints",
        nargs="*",
        default=default_langs,
        help="Language hints per Vision (es. it en).",
    )
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=default_max_chars,
        help="Max caratteri di testo OCR salvati per frame (0 = nessun limite).",
    )
    parser.add_argument(
        "--keep-text-non-match",
        action="store_true",
        default=default_keep_text,
        help="Salva il testo OCR anche per i frame senza match.",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        default=default_case_sensitive,
        help="Rende la ricerca sensibile alle maiuscole/minuscole.",
    )
    parser.add_argument(
        "--fallback-search",
        action="store_true",
        default=True,
        help="Se non trova sottocartelle per il post, esegue una ricerca ricorsiva per nome file.",
    )
    parser.add_argument(
        "--no-fallback-search",
        dest="fallback_search",
        action="store_false",
        help="Disabilita la ricerca ricorsiva per nome file.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        dest="resume",
        default=default_resume,
        help="Se esiste il report di output, salta i video già presenti e aggiunge solo i nuovi.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disabilita il resume e rielabora tutto.",
    )
    parser.add_argument(
        "--skip-posts-without-video",
        action="store_true",
        dest="skip_posts_without_video",
        default=default_skip_without_video,
        help="Salta i post senza flag has_video per evitare match accidentali.",
    )
    parser.add_argument(
        "--include-posts-without-video",
        action="store_false",
        dest="skip_posts_without_video",
        help="Analizza anche i post senza flag has_video (comportamento precedente).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    phrases: List[str] = []
    phrases.extend(env_phrases())
    if args.phrase:
        phrases.extend([p for p in args.phrase if p])
    if args.phrases:
        phrases.extend([p for p in args.phrases if p])
    phrases = [p.strip() for p in phrases if p and p.strip()]

    if not phrases:
        print(
            "[ERRORE] Specifica --phrase/--phrases o imposta VIDEO_PHRASE/VIDEO_PHRASES "
            "(fallback PHRASE/PHRASES; compatibilità IMGEVID_*)."
        )
        sys.exit(1)

    args.phrases = phrases
    if args.frame_every <= 0:
        print("[ERRORE] --frame-every deve essere > 0")
        sys.exit(1)

    report = process_posts(args)
    output_path = resolve_path(args.output)
    write_json_atomic(output_path, report)
    print(f"[INFO] Report scritto in {output_path}")
    print(f"[INFO] Frame analizzati: {report['summary']['frames_analyzed']}")
    print(f"[INFO] Frame con match: {report['summary']['frames_with_match']}")


if __name__ == "__main__":
    main()
