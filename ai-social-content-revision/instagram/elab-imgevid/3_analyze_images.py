#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Passo 3: analizza immagini dei post con Google Cloud Vision per trovare una frase specifica.

Uso rapido:
    python elab-imgevid/3_analyze_images.py \
        --phrase "testo da cercare" \
        --images-dir elab-imgevid/contenuti/immagini \
        --posts-file output/scaricati/posts_scaricati.json
"""

import argparse
import datetime as dt
import os
import sys
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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

IMGEVID_IMAGES_DIR = os.getenv("IMGEVID_IMAGES_DIR", "elab-imgevid/contenuti/immagini")
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
    Legge le frasi per immagini (IMAGE_PHRASES/IMAGE_PHRASE) con fallback alle comuni
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

    for key in ("IMAGE_PHRASES", "PHRASES", "IMGEVID_IMAGE_PHRASES", "IMGEVID_PHRASES"):
        raw = os.getenv(key)
        if raw:
            _add_from_raw(raw)

    for key in ("IMAGE_PHRASE", "PHRASE", "IMGEVID_IMAGE_PHRASE", "IMGEVID_PHRASE"):
        single = os.getenv(key)
        if single and single.strip():
            phrases.append(single.strip())

    # dedupe preservando l'ordine
    return list(dict.fromkeys(phrases))


def _normalize_seq(value: Any) -> tuple:
    """
    Normalizza sequenze (stringa -> lista di token, lista/tupla -> tupla).
    """
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
        "images_dir": str(config.get("images_dir") or ""),
        "max_images_per_post": int(config.get("max_images_per_post") or 0),
        "language_hints": _normalize_seq(config.get("language_hints")),
        "allowed_exts": normalized_exts,
        "phrases": _normalize_seq(config.get("phrases")),
        "case_sensitive": bool(config.get("case_sensitive")),
        "keep_text_non_match": bool(config.get("keep_text_non_match")),
        "skip_posts_without_image": bool(config.get("skip_posts_without_image")),
        "fallback_search": bool(config.get("fallback_search")),
        "max_text_chars": int(config.get("max_text_chars") or 0),
    }


def build_run_config(args: argparse.Namespace, allowed_exts: Sequence[str]) -> Dict[str, Any]:
    """
    Costruisce uno snapshot della configurazione corrente da salvare nel report.
    """
    return {
        "posts_file": str(resolve_path(args.posts_file)),
        "images_dir": str(resolve_path(args.images_dir)),
        "max_images_per_post": int(args.max_images_per_post),
        "language_hints": list(args.language_hints or []),
        "allowed_exts": list(allowed_exts),
        "phrases": list(args.phrases or []),
        "case_sensitive": bool(args.case_sensitive),
        "keep_text_non_match": bool(args.keep_text_non_match),
        "skip_posts_without_image": bool(args.skip_posts_without_image),
        "fallback_search": bool(args.fallback_search),
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


def matches_any(text: str, phrases: Sequence[str], case_sensitive: bool) -> bool:
    for ph in phrases:
        if phrase_in_text(text, ph, case_sensitive=case_sensitive):
            return True
    return False


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


def detect_text(
    client: "vision.ImageAnnotatorClient", image_path: Path, language_hints: Sequence[str]
) -> str:
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


def process_posts(args: argparse.Namespace) -> Dict[str, Any]:
    load_env_from_file()

    run_started_at = dt.datetime.now()
    run_started_monotonic = time.monotonic()

    posts = load_posts(args.posts_file)
    allowed_exts = normalize_exts(args.allowed_exts.split(","))
    run_config = build_run_config(args, allowed_exts)
    client = ensure_vision_client()
    skip_posts_without_image = args.skip_posts_without_image
    output_path = resolve_path(args.output)
    resume = bool(args.resume)

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
        # Evita falsi positivi dovuti al fallback su file con numeri simili:
        # se il post non ha flag image, salta (configurabile via CLI/env).
        if skip_posts_without_image and not post.get("has_image"):
            continue

        post_key = make_post_key(post)

        images = find_media_files(
            args.images_dir,
            post,
            allowed_exts=allowed_exts,
            fallback_search=bool(args.fallback_search),
            max_files=max(0, args.max_images_per_post),
        )
        if not images:
            continue

        post_matches = 0
        post_entries: List[Dict[str, Any]] = []
        post_has_match = False

        for image_path in images:
            image_key = make_image_key(post, image_path)
            if resume and image_key in processed_keys:
                continue

            image_started = time.monotonic()
            total_images += 1
            try:
                full_text = detect_text(client, image_path, args.language_hints or [])
                match = matches_any(full_text, args.phrases, case_sensitive=args.case_sensitive)
                if match:
                    post_matches += 1
                    images_with_match += 1
                    post_has_match = True

                entry: Dict[str, Any] = {
                    "post_id": post.get("id"),
                    "post_number": post.get("post_number"),
                    "image": str(image_path),
                    "match": match,
                    "full_text_chars": len(full_text),
                    "process_seconds": round(time.monotonic() - image_started, 3),
                }

                should_keep_text = match or args.keep_text_non_match
                if should_keep_text:
                    trimmed = trim_text(full_text, args.max_text_chars)
                    entry["text"] = trimmed
                    entry["text_truncated"] = bool(
                        args.max_text_chars and len(full_text) > args.max_text_chars
                    )

                post_entries.append(entry)
            except Exception as exc:  # pylint: disable=broad-except
                post_entries.append(
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
                if match:
                    posts_with_match_set.add(post_key)
            if post_has_match:
                # Stop elaborazione per questo post: passa al successivo
                break

        results.extend(post_entries)

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
        "phrases": list(args.phrases),
        "case_sensitive": args.case_sensitive,
        "language_hints": args.language_hints or [],
        "posts_file": str(resolve_path(args.posts_file)),
        "images_dir": str(resolve_path(args.images_dir)),
        "timestamp": dt.datetime.now().isoformat(),
        "config": run_config,
        "summary": summary,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    load_env_from_file()
    default_langs = env_list("IMGEVID_LANGUAGE_HINTS", ["it"])
    default_allowed = os.getenv("IMGEVID_ALLOWED_IMAGE_EXTS", "jpg,jpeg,png,webp,bmp")
    default_output = os.path.join(IMGEVID_REPORT_DIR, "vision_images.json")
    default_max_images = int(os.getenv("IMGEVID_MAX_IMAGES_PER_POST", "0"))
    default_max_chars = int(os.getenv("IMGEVID_MAX_TEXT_CHARS_IMAGE", "1200"))
    default_keep_text = env_bool("IMGEVID_KEEP_TEXT_NON_MATCH", False)
    default_case_sensitive = env_bool("IMGEVID_CASE_SENSITIVE", False)
    default_phrases = env_phrases()
    default_skip_without_image = env_bool("IMGEVID_SKIP_POSTS_WITHOUT_IMAGE", True)
    default_resume = env_bool("IMGEVID_RESUME_IMAGES", True)

    parser = argparse.ArgumentParser(
        description="Cerca una frase dentro di immagini usando Google Cloud Vision."
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
        "--images-dir",
        default=IMGEVID_IMAGES_DIR,
        help="Cartella dove si trovano le immagini (sottocartelle per post_id o post_number).",
    )
    parser.add_argument(
        "--output",
        default=default_output,
        help="Percorso del report JSON.",
    )
    parser.add_argument(
        "--allowed-exts",
        default=default_allowed,
        help="Estensioni immagini ammesse, separate da virgola.",
    )
    parser.add_argument(
        "--language-hints",
        nargs="*",
        default=default_langs,
        help="Language hints per Vision (es. it en).",
    )
    parser.add_argument(
        "--max-images-per-post",
        type=int,
        default=default_max_images,
        help="Limite immagini per post (0 = nessun limite).",
    )
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=default_max_chars,
        help="Max caratteri di testo OCR salvati per immagine (0 = nessun limite).",
    )
    parser.add_argument(
        "--keep-text-non-match",
        action="store_true",
        default=default_keep_text,
        help="Salva il testo OCR anche per le immagini senza match.",
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
        help="Se esiste il report di output, salta le immagini già presenti e aggiunge solo le nuove.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disabilita il resume e rielabora tutto.",
    )
    parser.add_argument(
        "--skip-posts-without-image",
        action="store_true",
        dest="skip_posts_without_image",
        default=default_skip_without_image,
        help="Salta i post senza flag has_image per evitare match accidentali.",
    )
    parser.add_argument(
        "--include-posts-without-image",
        action="store_false",
        dest="skip_posts_without_image",
        help="Analizza anche i post senza flag has_image (comportamento precedente).",
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
        print("[ERRORE] Specifica --phrase/--phrases o imposta IMAGE_PHRASE/IMAGE_PHRASES "
              "(fallback PHRASE/PHRASES; compatibilità IMGEVID_*).")
        sys.exit(1)

    args.phrases = phrases
    report = process_posts(args)

    output_path = resolve_path(args.output)
    write_json_atomic(output_path, report)
    print(f"[INFO] Report scritto in {output_path}")
    print(f"[INFO] Immagini elaborate: {report['summary']['images_scanned']}")
    print(f"[INFO] Immagini con match: {report['summary']['images_with_match']}")


if __name__ == "__main__":
    main()
