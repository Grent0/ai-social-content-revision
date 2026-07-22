#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 5: elimina i post indicati in DA-ELIM/posts_602_618_747.json.
Richiede sempre la conferma esplicita digitando 'elimina' sulla console
prima di procedere alla cancellazione reale. In dry-run mostra solo cosa
verrebbe cancellato.

Uso rapido:
    python 5_elimina_post.py --dry-run
    python 5_elimina_post.py --no-dry-run
    python 5_elimina_post.py --input DA-ELIM/posts_602_618_747.json --no-dry-run
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
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


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ["1", "true", "yes", "si", "on"]


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


load_env_from_file()

# Endpoint Graph API
FB_API_BASE = os.getenv("FB_API_BASE", "https://graph.facebook.com/v18.0")

# Config input/output
DEFAULT_INPUT = os.getenv("DELETE_INPUT_FILE", "Eliminare/matched_posts.json")
DEFAULT_DRY_RUN = env_bool("DEFAULT_DRY_RUN", True)
REPORT_DIR = Path(os.getenv("DELETE_REPORT_DIR", "output/eliminati"))


# ==========================
# FUNZIONI CORE
# ==========================


def load_posts(input_path: str) -> List[Dict[str, Any]]:
    """
    Carica i post da eliminare dal JSON di matched_posts.
    """
    path = Path(input_path)
    if not path.is_file():
        alt_path = Path(__file__).parent / path
        if alt_path.is_file():
            path = alt_path
        else:
            print(f"[ERRORE] File non trovato: {input_path}")
            print("[INFO] Percorsi verificati:", path.resolve(), "e", alt_path.resolve())
            sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERRORE] File JSON non valido: {path}")
        print(f"[ERRORE] Dettagli: {exc}")
        sys.exit(1)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERRORE] Impossibile leggere il file: {path}")
        print(f"[ERRORE] Dettagli: {exc}")
        sys.exit(1)

    posts = data.get("posts") or data.get("results") or []
    if not isinstance(posts, list):
        print(f"[ERRORE] Formato inatteso nel file: {path}")
        sys.exit(1)

    print(f"[INFO] Caricati {len(posts)} post da {path}")
    return posts


def confirm_elimination(total_posts: int) -> bool:
    """
    Richiede la digitura 'elimina' per procedere alla cancellazione reale.
    """
    candidate = input(
        f"Stai per eliminare {total_posts} post. Digita 'elimina' per procedere: "
    ).strip().lower()

    if candidate != "elimina":
        print("[STOP] Conferma non valida. Scrivi esattamente: elimina")
        return False
    return True


def delete_post(post_id: str, access_token: str) -> Tuple[bool, Optional[str]]:
    """
    Elimina un post tramite Graph API.
    Ritorna (successo, errore).
    """
    url = f"{FB_API_BASE}/{post_id}"
    params = {"access_token": access_token}
    resp = requests.delete(url, params=params)
    if resp.status_code != 200:
        return False, f"{resp.status_code}: {resp.text}"

    payload = resp.json()
    if payload.get("success") is True:
        return True, None
    if "error" in payload:
        return False, str(payload["error"])
    # Alcune risposte possono restituire l'id se la cancellazione va a buon fine
    return True, None


def process_deletions(
    posts: List[Dict[str, Any]], dry_run: bool, access_token: str
) -> Dict[str, Any]:
    """
    Esegue la cancellazione (o la simula se dry_run=True) e restituisce il report.
    """
    deleted = 0
    failed = 0
    details: List[Dict[str, Any]] = []
    seen_ids = set()

    for idx, post in enumerate(posts, start=1):
        post_id = str(post.get("id") or post.get("post_id") or "").strip()
        message = (
            post.get("message")
            or post.get("messaggio_originale")
            or post.get("messaggio_modificato")
            or ""
        ).strip()
        created_time = post.get("created_time", "N/A")
        post_number = post.get("post_number", "N/A")

        if not post_id:
            failed += 1
            details.append(
                {
                    "index": idx,
                    "post_number": post_number,
                    "post_id": None,
                    "created_time": created_time,
                    "action": "skipped",
                    "reason": "missing_post_id",
                    "success": False,
                }
            )
            continue

        if post_id in seen_ids:
            print(f"[SKIP] Post duplicato {post_id}, salto.")
            details.append(
                {
                    "index": idx,
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "skipped",
                    "reason": "duplicate",
                    "success": True,
                }
            )
            continue
        seen_ids.add(post_id)

        preview = message.replace("\n", " ")[:120]
        print(f"\n[{idx}/{len(posts)}] Post {post_id} (#{post_number}) - {created_time}")
        if preview:
            print(f"Anteprima: {preview}")

        if dry_run:
            details.append(
                {
                    "index": idx,
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "dry_run",
                    "success": True,
                }
            )
            continue

        ok, err = delete_post(post_id, access_token)
        if ok:
            deleted += 1
            print("[OK] Post eliminato.")
            details.append(
                {
                    "index": idx,
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "deleted",
                    "success": True,
                }
            )
        else:
            failed += 1
            print(f"[ERRORE] Cancellazione fallita: {err}")
            details.append(
                {
                    "index": idx,
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "delete_failed",
                    "error": err,
                    "success": False,
                }
            )
        time.sleep(1)

    summary = {
        "total_posts": len(posts),
        "deleted": deleted,
        "failed": failed,
        "dry_run": dry_run,
    }
    return {"summary": summary, "details": details}


def save_report(report: Dict[str, Any]) -> None:
    """
    Salva il report in output/eliminati/eliminazione_<timestamp>.json.
    """
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"eliminazione_{timestamp}.json"
    path = REPORT_DIR / filename
    report["timestamp"] = dt.datetime.now().isoformat()
    write_json_atomic(path, report)
    print(f"\n[INFO] Report salvato in {path.resolve()}")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Elimina i post elencati in DA-ELIM/posts_602_618_747.json tramite Graph API."
    )
    parser.add_argument(
        "--input",
        help="File JSON con i post da eliminare (default: DA-ELIM/posts_602_618_747.json).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Mostra cosa verrebbe cancellato senza chiamare l'API.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Esegue davvero la cancellazione (richiede conferma 'elimina').",
    )
    parser.set_defaults(dry_run=DEFAULT_DRY_RUN)

    args = parser.parse_args()

    input_file = args.input if args.input else DEFAULT_INPUT

    page_id_hint = os.getenv("PAGE_ID", "<non impostata>")
    print("===== ELIMINA POST FACEBOOK =====")
    print(f"Pagina FB (da .env se impostata): {page_id_hint}")
    print(f"Input file: {input_file}")
    print(f"Dry-run: {'SI' if args.dry_run else 'NO'}")
    print("=================================")

    posts = load_posts(input_file)
    if not posts:
        print("[INFO] Nessun post da eliminare nel file fornito.")
        return

    access_token = ""

    if not args.dry_run:
        if not confirm_elimination(len(posts)):
            print("[INFO] Nessuna cancellazione eseguita.")
            return
        # Dopo la conferma, verifica che le chiavi siano presenti
        page_id = env_or_raise("PAGE_ID")
        access_token = env_or_raise("PAGE_ACCESS_TOKEN")
        print(f"[INFO] Chiavi verificate per la pagina: {page_id}")
    else:
        # In dry-run non servono le chiavi, ma proviamo a leggere la pagina se presente
        access_token = os.getenv("PAGE_ACCESS_TOKEN", "")

    report = process_deletions(posts, args.dry_run, access_token)
    save_report(report)

    summary = report["summary"]
    print("\n========== RIEPILOGO ==========")
    print(f"Post totali: {summary['total_posts']}")
    print(f"Post eliminati: {summary['deleted']}")
    print(f"Post falliti: {summary['failed']}")
    print(f"Modalita dry-run: {'SI' if summary['dry_run'] else 'NO'}")
    print("================================")


if __name__ == "__main__":
    main()
