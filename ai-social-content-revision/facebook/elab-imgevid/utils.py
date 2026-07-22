#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Utility condivise per l'elaborazione di immagini e video con Google Cloud Vision.
"""

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def repo_root() -> Path:
    """
    Ritorna la root del repository (cartella padre di questo file).
    """
    return Path(__file__).resolve().parent.parent


def load_env_from_file(env_file: str = ".env") -> None:
    """
    Carica chiavi=valore da un file .env nella root del repo, se presente.
    """
    env_path = repo_root() / env_file
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
                if current_key == "GOOGLE_APPLICATION_CREDENTIALS":
                    value = _prepare_google_credentials_value(value)
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
        if key == "GOOGLE_APPLICATION_CREDENTIALS":
            value = _prepare_google_credentials_value(value)
        os.environ.setdefault(key, value)

    if current_key and current_lines:
        value = "\n".join(current_lines)
        if quote_char and value.startswith(quote_char):
            value = value[1:]
        if current_key == "GOOGLE_APPLICATION_CREDENTIALS":
            value = _prepare_google_credentials_value(value)
        os.environ.setdefault(current_key, value)


_GOOGLE_CREDS_TEMP_PATH: Optional[Path] = None


def _prepare_google_credentials_value(value: str) -> str:
    """
    Supporta credenziali inline (JSON o base64 del JSON) creando un file temporaneo
    e restituendo il percorso da usare in GOOGLE_APPLICATION_CREDENTIALS.
    Se è già un percorso valido, lo normalizza in assoluto.
    """
    inline = _decode_google_creds(value)
    if inline:
        return _ensure_google_creds_file(inline)

    path = Path(value)
    if not path.is_absolute():
        path = repo_root() / path
    if path.exists():
        return str(path)

    return value


def _decode_google_creds(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""

    if raw.lstrip().startswith("{"):
        try:
            json.loads(raw)
            return raw
        except Exception:
            return ""

    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        if decoded.lstrip().startswith("{"):
            json.loads(decoded)
            return decoded
    except Exception:
        pass

    return ""


def _ensure_google_creds_file(content: str) -> str:
    global _GOOGLE_CREDS_TEMP_PATH
    if _GOOGLE_CREDS_TEMP_PATH and _GOOGLE_CREDS_TEMP_PATH.exists():
        return str(_GOOGLE_CREDS_TEMP_PATH)

    tmp_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", prefix="gcp-creds-")
    tmp_file.write(content)
    tmp_file.flush()
    tmp_file.close()
    _GOOGLE_CREDS_TEMP_PATH = Path(tmp_file.name)
    return tmp_file.name


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON su file in modo atomico: tmp + rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


def resolve_path(path: str) -> Path:
    """
    Converte un percorso relativo alla root del repo in Path assoluto.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    return p


def load_posts(posts_file: str) -> List[Dict[str, Any]]:
    """
    Carica i post scaricati da un file JSON.
    """
    path = resolve_path(posts_file)
    if not path.exists():
        raise FileNotFoundError(f"File post non trovato: {path}")
    data = json.loads(path.read_text())
    posts = data.get("posts", [])
    return posts


def normalize_exts(exts: Iterable[str]) -> Tuple[str, ...]:
    """
    Normalizza un elenco di estensioni restituendo una tupla con il punto iniziale.
    """
    normalized = []
    for ext in exts:
        e = ext.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = f".{e}"
        normalized.append(e)
    # dict.fromkeys preserva l'ordine e rimuove i duplicati
    return tuple(dict.fromkeys(normalized))


def phrase_in_text(text: str, phrase: str, case_sensitive: bool = False) -> bool:
    """
    Controlla se la frase è presente nel testo (case-insensitive di default).
    """
    if not phrase:
        return False
    if case_sensitive:
        return phrase in text
    return phrase.lower() in text.lower()


def _dedupe_paths(paths: List[Path], max_items: int = 0) -> List[Path]:
    seen = set()
    result: List[Path] = []
    for p in paths:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
        if max_items and len(result) >= max_items:
            break
    return result


def find_media_files(
    media_dir: str,
    post: Dict[str, Any],
    allowed_exts: Tuple[str, ...],
    fallback_search: bool = True,
    max_files: int = 0,
) -> List[Path]:
    """
    Tenta di trovare file media per un post.
    - Cerca in sottocartelle media_dir/<post_id>/ e media_dir/<post_number>/.
    - Se non trova nulla e fallback_search=True, cerca ricorsivamente file che contengono
      post_id o post_number nel nome.
    - max_files=0 significa nessun limite.
    """
    base_dir = resolve_path(media_dir)
    if not base_dir.exists():
        return []

    post_id = str(post.get("id", "")).strip()
    post_number = post.get("post_number")
    post_number_str = str(post_number) if post_number is not None else ""
    collected: List[Path] = []

    def add_from_dir(directory: Path) -> None:
        if not directory.exists():
            return
        for item in directory.iterdir():
            if item.is_file() and item.suffix.lower() in allowed_exts:
                collected.append(item)

    for token in (post_id, post_number_str):
        if token:
            add_from_dir(base_dir / token)

    if fallback_search and not collected:
        patterns = []
        if post_id:
            patterns.append(f"*{post_id}*")
        if post_number_str:
            patterns.append(f"*{post_number_str}*")

        for pattern in patterns:
            for item in base_dir.rglob(pattern):
                if item.is_file() and item.suffix.lower() in allowed_exts:
                    collected.append(item)
                    if max_files and len(collected) >= max_files:
                        return _dedupe_paths(collected, max_items=max_files)

    return _dedupe_paths(collected, max_items=max_files)
