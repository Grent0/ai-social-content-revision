#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 4: Pubblica le modifiche su Facebook
Legge i post modificati da un file JSON e aggiorna i post su Facebook
tramite le API Graph. Pubblica solo se `status_meccanico` o `status_ai`
sono impostati a `modificato` (il file di default contiene già solo
i post pronti alla pubblicazione: `DA-PUB/output_openai.json` con fallback
ai file pronti generati dagli step precedenti).

Uso:
    python 4_pubblica_modifiche.py --dry-run
    python 4_pubblica_modifiche.py --input \"DA-PUB/output_openai.json\"
    python 4_pubblica_modifiche.py --input <file> --no-dry-run

    Puoi anche impostare DEFAULT_DRY_RUN=true nel .env per avere la modalità
    anteprima attiva di default.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set

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
    """
    Legge un booleano da env con fallback.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ["1", "true", "yes", "si", "on"]


load_env_from_file()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


def resolve_default_publish_input() -> str:
    """
    Determina il file di input di default per la pubblicazione.
    Priorità:
    1) Variabile d'ambiente PUBLISH_INPUT_FILE
    2) DA-PUB/output_openai.json (output AI senza status)
    3) DA-PUB/pronti_modifiche_senza_hashtag.json (pipeline precedente)
    4) output/pronti_per_pubblicazione/pronti.json (default storico)
    """
    env_value = os.getenv("PUBLISH_INPUT_FILE")
    if env_value:
        return env_value

    base_dir = Path(__file__).parent
    candidates = [
        "DA-PUB/output_openai.json",
        "DA-PUB/pronti_modifiche_senza_hashtag.json",
        "output/pronti_per_pubblicazione/pronti.json",
    ]

    for candidate in candidates:
        if (base_dir / candidate).is_file():
            return candidate
    return candidates[0]

# Page ID e token obbligatori
PAGE_ID = env_or_raise("PAGE_ID")
ACCESS_TOKEN = env_or_raise("PAGE_ACCESS_TOKEN")

# Endpoint Graph API
FB_API_BASE = os.getenv("FB_API_BASE", "https://graph.facebook.com/v18.0")

# Cartelle di gestione
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
PUBBLICATI_SUBDIR = os.getenv("PUBBLICATI_SUBDIR", "pubblicati")
FAILED_SUBDIR = os.getenv("FAILED_SUBDIR", "non_riusciti")
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "pubblicazione"
PRONTI_SUBDIR = os.getenv("PRONTI_SUBDIR", "pronti_per_pubblicazione")

# Modalità dry-run di default (sovrascrivibile da CLI)
DEFAULT_DRY_RUN = env_bool("DEFAULT_DRY_RUN", False)
# Percorso di default per il file da pubblicare (sovrascrivibile da env/CLI)
PUBLISH_INPUT_FILE = resolve_default_publish_input()
# Pausa fissa tra un aggiornamento e il successivo
FIXED_PUBLISH_SLEEP_SECONDS = 5.0
# Se True, interrompe la run al primo errore anti-spam/rate limit (FB code=368)
DEFAULT_STOP_ON_RATE_LIMIT = env_bool("STOP_ON_RATE_LIMIT", True)


# ==========================
# CHECKPOINTING
# ==========================


def get_checkpoint_path(input_path: str) -> Path:
    """
    Ritorna il percorso del file di checkpoint nella cartella dedicata.
    """
    input_file = Path(input_path)
    base_dir = Path(__file__).parent
    checkpoint_dir = base_dir / CHECKPOINT_DIR / CHECKPOINT_SUBDIR
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_name = f"checkpoint_pubblicazione_{input_file.stem}.json"
    return checkpoint_dir / checkpoint_name


def load_checkpoint(input_path: str) -> Set[str]:
    """
    Carica un checkpoint esistente e ritorna un set di post_id già pubblicati.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    if not checkpoint_path.exists():
        return set()
    
    try:
        data = json.loads(checkpoint_path.read_text())
        published_ids = data.get("published_post_ids", [])
        print(f"[INFO] Trovato checkpoint: {len(published_ids)} post già pubblicati.")
        return {str(pid) for pid in published_ids}
    except (json.JSONDecodeError, KeyError):
        print("[WARN] Checkpoint corrotto, verrà ignorato e sovrascritto.")
        return set()


def save_checkpoint(input_path: str, published_ids: Set[str]) -> None:
    """
    Salva il set di ID dei post pubblicati nel file di checkpoint.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    checkpoint_data = {
        "last_updated": dt.datetime.now().isoformat(),
        "published_post_ids": [str(pid) for pid in published_ids],
    }
    write_json_atomic(checkpoint_path, checkpoint_data)


def delete_checkpoint(input_path: str) -> None:
    """
    Elimina il file di checkpoint se esiste.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"[INFO] Checkpoint di pubblicazione eliminato: {checkpoint_path.name}")


# ==========================
# FUNZIONI CORE
# ==========================


def load_modified_posts(input_path: str) -> List[Dict[str, Any]]:
    """
    Carica i post modificati da un file JSON.
    Supporta diversi formati di file:
    - nuovi: key 'posts' (es. DA-PUB/output_openai.json o DA-PUB/pronti_modifiche_senza_hashtag.json)
    - vecchi: key 'results'
    - file dei falliti: key 'items'
    """
    path = Path(input_path)
    if not path.is_file():
        # Prova a risolvere rispetto alla cartella dello script se lanciato da fuori
        base_dir = Path(__file__).parent
        alt_path = base_dir / path
        if alt_path.is_file():
            path = alt_path
        else:
            print(f"[ERRORE] File non trovato: {input_path}")
            print("[INFO] Percorsi verificati:", path.resolve(), "e", alt_path.resolve())
            print("[INFO] Esegui prima lo script 2 e 3 per modificare i post.")
            sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[ERRORE] File JSON non valido: {path}")
        print(f"[ERRORE] Dettagli: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERRORE] Impossibile leggere il file: {path}")
        print(f"[ERRORE] Dettagli: {e}")
        sys.exit(1)

    if "posts" in data:
        results = data.get("posts", [])
    elif "items" in data:
        results = data.get("items", [])  # Per i file dei falliti
    elif "results" in data:
        results = data.get("results", [])  # Fallback per vecchi formati
    else:
        results = []

    if not results:
        print(f"[WARN] Nessun post trovato nel file: {path}")

    print(f"[INFO] Caricati {len(results)} post da {path}")
    return results


def normalize_identifier(post: Dict[str, Any]) -> str:
    """
    Identificatore robusto per checkpoint (post_id o permalink).
    """
    for key in ("post_id", "id", "permalink", "post_number"):
        val = post.get(key)
        if val not in (None, "", "None"):
            return str(val)
    return ""


def extract_messages(post: Dict[str, Any]) -> Tuple[str, str]:
    """
    Restituisce (originale, modificato) con fallback ai campi legacy.
    """
    original_message = (
        post.get("messaggio_originale")
        or post.get("original_message")
        or post.get("messaggio")
        or post.get("message")
        or ""
    )
    modified_message = (
        post.get("messaggio_modificato")
        or post.get("modified_message")
        or post.get("messaggio")
        or post.get("message")
        or ""
    )
    return original_message, modified_message


def is_post_marked_as_modified(post: Dict[str, Any]) -> bool:
    """
    Considera un post "da pubblicare" se:
    - ha status_meccanico/status_ai/legacy_status marcati come modificato, oppure
    - non ha status ma il messaggio modificato è diverso dall'originale (es. output_openai.json)
    """
    status_meccanico = post.get("status_meccanico")
    status_ai = post.get("status_ai")
    legacy_status = post.get("status")

    if any(val is not None for val in (status_meccanico, status_ai, legacy_status)):
        return (status_meccanico == "modificato") or (status_ai == "modificato") or (legacy_status == "modified")

    original_message, modified_message = extract_messages(post)
    return original_message != modified_message

def parse_fb_error_from_reason(reason: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """
    Prova a estrarre informazioni dall'errore serializzato come:
      "400: {\"error\": {...}}"
    Ritorna (http_status, fb_code, fb_subcode, fb_message) oppure (None, None, None, None).
    """
    if not reason:
        return None, None, None, None

    match = re.match(r"^(?P<status>\d{3})\s*:\s*(?P<body>\{.*\})\s*$", reason.strip())
    if not match:
        return None, None, None, None

    http_status = int(match.group("status"))
    body = match.group("body")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return http_status, None, None, None

    err = payload.get("error") or {}
    fb_code = err.get("code")
    fb_subcode = err.get("error_subcode")
    fb_message = err.get("message")
    return http_status, fb_code, fb_subcode, fb_message


def update_post(post_id: str, new_message: str) -> Tuple[bool, Optional[str]]:
    """
    Aggiorna il messaggio di un post su Facebook.
    Ritorna (successo, errore).
    """
    url = f"{FB_API_BASE}/{post_id}"
    params = {
        "message": new_message,
        "access_token": ACCESS_TOKEN,
    }
    r = requests.post(url, data=params)
    if r.status_code != 200:
        err_msg = f"{r.status_code}: {r.text}"
        print(f"[ERRORE] Aggiornamento post {post_id} fallito:", err_msg)
        return False, err_msg

    data = r.json()
    # Alcune versioni restituiscono {"success": true}, altre il post id.
    if "error" in data:
        err_msg = str(data["error"])
        print(f"[ERRORE] Aggiornamento post {post_id}:", err_msg)
        return False, err_msg

    print(f"[OK] Post {post_id} aggiornato con successo.")
    return True, None


def save_publication_report(report: Dict[str, Any], output_dir: str = "output/pubblicati") -> None:
    """
    Salva il report di pubblicazione nella cartella pubblicati.
    """
    import datetime as dt
    
    base_dir = Path(__file__).parent
    pub_dir = base_dir / output_dir
    pub_dir.mkdir(parents=True, exist_ok=True)
    
    # Nome file con timestamp
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pubblicazione_{timestamp}.json"
    output_path = pub_dir / filename
    
    # Aggiungi timestamp al report
    report["timestamp"] = dt.datetime.now().isoformat()
    
    write_json_atomic(output_path, report)
    print(f"\n[INFO] Report salvato in {output_path.resolve()}")


def save_failed_updates(failed_items: List[Dict[str, Any]], output_dir: str = "output/non_riusciti") -> None:
    """
    Salva i post falliti in un file JSON dedicato.
    """
    if not failed_items:
        return

    base_dir = Path(__file__).parent
    fail_dir = base_dir / output_dir
    fail_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"falliti_{timestamp}.json"
    output_path = fail_dir / filename

    payload = {
        "failed": len(failed_items),
        "timestamp": dt.datetime.now().isoformat(),
        "items": failed_items,
    }

    write_json_atomic(output_path, payload)
    print(f"[INFO] Post non riusciti salvati in {output_path.resolve()}")


def publish_posts(
    posts: List[Dict[str, Any]],
    input_path: str,
    dry_run: bool = False,
    stop_on_rate_limit: bool = True,
) -> Dict[str, Any]:
    """
    Pubblica le modifiche su Facebook.
    Se dry_run è True, mostra solo cosa verrebbe fatto senza aggiornare.
    Ritorna un dizionario con il report dettagliato.
    """
    updated_count = 0
    failed_count = 0
    skipped_count = 0
    already_published_count = 0
    not_processed_count = 0
    stopped_due_to_rate_limit = False
    report_details = []
    failed_items: List[Dict[str, Any]] = []

    # Carica ID già pubblicati dal checkpoint
    published_ids = load_checkpoint(input_path)

    for idx, post in enumerate(posts):
        post_id = post.get("post_id") or post.get("id")
        post_number = post.get("post_number", "N/A")
        status_meccanico = post.get("status_meccanico")
        status_ai = post.get("status_ai")
        legacy_status = post.get("status")
        original_message, modified_message = extract_messages(post)
        created_time = post.get("created_time", "N/A")
        checkpoint_key = normalize_identifier(post)

        # Determina se pubblicare secondo le nuove regole
        has_status_flags = any(val is not None for val in (status_meccanico, status_ai, legacy_status))
        if has_status_flags:
            should_publish = (
                (status_meccanico == "modificato")
                or (status_ai == "modificato")
                or (legacy_status == "modified")
            )
            status_label = f"status_meccanico={status_meccanico}, status_ai={status_ai}, status={legacy_status}"
        else:
            should_publish = original_message != modified_message
            status_label = "auto_detected_change" if should_publish else "no_status_flags"

        if not should_publish:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Status: {status_label}")
            skipped_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "action": "skipped",
                "reason": status_label,
                "success": None,
            })
            continue

        # Verifica che il messaggio sia effettivamente diverso
        if original_message == modified_message:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Nessuna modifica effettiva")
            skipped_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "action": "skipped",
                "reason": "no_changes",
                "success": None,
            })
            continue
        
        # Salta i post già pubblicati in esecuzioni precedenti
        if checkpoint_key and checkpoint_key in published_ids:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Già pubblicato (da checkpoint)")
            already_published_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "action": "skipped_checkpoint",
                "reason": "already_published",
                "success": True, # Considerato un successo in termini di stato finale
            })
            continue

        print(f"\n[POST #{post_number}] {post_id} - {created_time}")
        print("Testo originale:")
        print(original_message)
        print("\nNuovo testo:")
        print(modified_message)
        print("-" * 60)

        if dry_run:
            print("[DRY-RUN] Non aggiorno il post (solo anteprima).")
            updated_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "action": "dry_run",
                "reason": "preview_only",
                "success": True,
            })
        else:
            ok, err_msg = update_post(post_id, modified_message)
            if ok:
                updated_count += 1
                if checkpoint_key:
                    published_ids.add(checkpoint_key)
                    save_checkpoint(input_path, published_ids) # Salva subito dopo il successo
                
                report_details.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "updated",
                    "reason": None,
                    "success": True,
                })
            else:
                failed_count += 1
                http_status, fb_code, fb_subcode, fb_message = parse_fb_error_from_reason(err_msg or "")
                print("[WARN] Aggiornamento fallito, passo al prossimo post.")
                report_details.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "action": "update_failed",
                    "reason": err_msg or "api_error",
                    "error_http_status": http_status,
                    "error_code": fb_code,
                    "error_subcode": fb_subcode,
                    "error_message": fb_message,
                    "success": False,
                })
                failed_items.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "error": err_msg,
                    "error_http_status": http_status,
                    "error_code": fb_code,
                    "error_subcode": fb_subcode,
                    "error_message": fb_message,
                    "original_message": original_message,
                    "modified_message": modified_message,
                })

                if stop_on_rate_limit and fb_code == 368:
                    stopped_due_to_rate_limit = True
                    remaining_posts = posts[idx + 1 :]
                    not_processed_count = 0
                    print(
                        "\n[STOP] Rilevato blocco anti-spam/rate limit (code=368). "
                        "Interrompo la run per evitare ulteriori tentativi inutili.\n"
                    )
                    # Mantieni il report completo: marca i rimanenti come non processati (o già in checkpoint)
                    for remaining_post in remaining_posts:
                        remaining_post_id = remaining_post.get("post_id") or remaining_post.get("id")
                        remaining_post_number = remaining_post.get("post_number", "N/A")
                        remaining_created_time = remaining_post.get("created_time", "N/A")
                        remaining_checkpoint_key = normalize_identifier(remaining_post)

                        if remaining_checkpoint_key and remaining_checkpoint_key in published_ids:
                            already_published_count += 1
                            report_details.append(
                                {
                                    "post_number": remaining_post_number,
                                    "post_id": remaining_post_id,
                                    "created_time": remaining_created_time,
                                    "action": "skipped_checkpoint",
                                    "reason": "already_published",
                                    "success": True,
                                }
                            )
                        else:
                            not_processed_count += 1
                            report_details.append(
                                {
                                    "post_number": remaining_post_number,
                                    "post_id": remaining_post_id,
                                    "created_time": remaining_created_time,
                                    "action": "not_processed",
                                    "reason": "stopped_on_rate_limit",
                                    "success": None,
                                }
                            )
                    break
            # Attendi un attimo prima di procedere al prossimo update per evitare burst di richieste
            time.sleep(FIXED_PUBLISH_SLEEP_SECONDS)

    print("\n========== RIEPILOGO ==========")
    print(f"Post totali nel file: {len(posts)}")
    print(f"Post aggiornati in questa sessione: {updated_count}")
    print(f"Post falliti in questa sessione: {failed_count}")
    print(f"Post saltati (non modificati): {skipped_count}")
    print(f"Post già pubblicati (da checkpoint): {already_published_count}")
    if stopped_due_to_rate_limit:
        print(f"Post non processati (stop su rate limit): {not_processed_count}")
    print(f"Modalità dry-run: {'SI' if dry_run else 'NO'}")
    print("================================")

    # Calcola conteggi dettagliati per tipo di azione
    action_counts = {}
    for detail in report_details:
        action = detail.get("action", "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1

    return {
        "summary": {
            "total_posts": len(posts),
            "updated_this_run": updated_count,
            "failed_this_run": failed_count,
            "skipped_no_change": skipped_count,
            "skipped_from_checkpoint": already_published_count,
            "not_processed": not_processed_count,
            "stopped_due_to_rate_limit": stopped_due_to_rate_limit,
            "dry_run": dry_run,
        },
        "action_breakdown": action_counts,
        "details": report_details,
        "failed_items": failed_items,
    }


# ==========================
# CONFERMA INTERATTIVA
# ==========================


def confirm_publication(total_posts: int) -> bool:
    """
    Richiede la digitura 'modifica' per procedere alla pubblicazione reale.
    """
    candidate = input(
        f"Stai per modificare {total_posts} post. Digita 'modifica' per procedere: "
    ).strip().lower()

    if candidate != "modifica":
        print("[STOP] Conferma non valida. Scrivi esattamente: modifica")
        return False
    return True


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pubblica le modifiche dei post su Facebook tramite API Graph."
    )
    parser.add_argument(
        "--input",
        help="File JSON con i post da pubblicare (default automatico: DA-PUB/output_openai.json se esiste, altrimenti i file pronti della pipeline)",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Mostra cosa verrebbe modificato senza aggiornare davvero i post.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Forza la pubblicazione reale anche se DEFAULT_DRY_RUN è true.",
    )
    parser.add_argument(
        "--stop-on-rate-limit",
        dest="stop_on_rate_limit",
        action="store_true",
        help="Interrompe la run al primo errore anti-spam/rate limit (FB code=368).",
    )
    parser.add_argument(
        "--no-stop-on-rate-limit",
        dest="stop_on_rate_limit",
        action="store_false",
        help="Continua anche se compaiono errori rate limit (non consigliato).",
    )
    parser.set_defaults(dry_run=DEFAULT_DRY_RUN)
    parser.set_defaults(stop_on_rate_limit=DEFAULT_STOP_ON_RATE_LIMIT)

    args = parser.parse_args()
    
    # Forza il percorso del file "pronti" se non specificato
    default_pronti = PUBLISH_INPUT_FILE
    input_file = args.input if args.input else default_pronti

    print("===== PUBBLICAZIONE MODIFICHE SU FACEBOOK =====")
    print(f"Pagina FB: {PAGE_ID}")
    print(f"Input file: {input_file}")
    print(f"Dry-run: {'SI' if args.dry_run else 'NO'}")
    print(f"Sleep tra update (fisso): {FIXED_PUBLISH_SLEEP_SECONDS}s")
    print(f"Stop su rate limit (code=368): {'SI' if args.stop_on_rate_limit else 'NO'}")
    print("===============================================")

    posts = load_modified_posts(input_file)
    
    # Controlla se ci sono post effettivamente modificati
    modified_posts_exist = any(
        is_post_marked_as_modified(p) for p in posts
    )

    if not modified_posts_exist:
        print("\n[INFO] Nessun post risulta modificato (né meccanico né AI).")
        print("[INFO] Il processo di pubblicazione non verrà avviato perché non ci sono modifiche da pubblicare.")
        
        report = {
            "summary": {
                "total_posts": len(posts),
                "message": "Nessuna modifica da pubblicare.",
                "dry_run": args.dry_run
            },
            "details": []
        }
        save_publication_report(report, f"{OUTPUT_DIR}/{PUBBLICATI_SUBDIR}")
        print("\n[COMPLETATO] Nessuna azione eseguita.")
        return

    if not args.dry_run:
        if not confirm_publication(len(posts)):
            print("[INFO] Nessuna pubblicazione eseguita.")
            return

    report = publish_posts(
        posts,
        input_file,
        args.dry_run,
        stop_on_rate_limit=args.stop_on_rate_limit,
    )
    
    # Salva report nella cartella pubblicati
    save_publication_report(report, f"{OUTPUT_DIR}/{PUBBLICATI_SUBDIR}")
    
    summary = report.get("summary", {})
    # Salva i post falliti in una cartella dedicata
    if not args.dry_run and summary.get("failed_this_run", 0) > 0:
        save_failed_updates(report.get("failed_items", []), f"{OUTPUT_DIR}/{FAILED_SUBDIR}")

    # Se non ci sono stati errori e non siamo in dry-run, puliamo il checkpoint
    if not args.dry_run and summary.get("failed_this_run", 0) == 0:
        delete_checkpoint(input_file)
    elif not args.dry_run:
        print("[INFO] Il checkpoint non è stato eliminato a causa di errori di pubblicazione.")

    print("\n[COMPLETATO] Pubblicazione completata!")


if __name__ == "__main__":
    main()
