"""
Script Selenium per modificare un post Instagram usando Firefox con profilo dedicato (desktop).
Requisiti:
- Profilo Firefox già loggato a Instagram configurato tramite FIREFOX_PROFILE_PATH
- geckodriver compatibile nella PATH
"""

from selenium import webdriver
import argparse
import json
import os
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver import ActionChains
from pathlib import Path
import sys
import time
import random
import re
from datetime import datetime
from typing import List, Dict, Set
from urllib.parse import urlparse

# === CONFIGURAZIONE ===
PERMALINK = os.getenv(
    "INSTAGRAM_PERMALINK",
    "https://www.instagram.com/p/POST_ID/",
)  # placeholder generico: usare il permalink del post reale
NUOVO_TESTO = "Le rinnovabili sono il futuro."   # nuovo contenuto del post
FIREFOX_PROFILE_PATH = os.getenv(
    "FIREFOX_PROFILE_PATH",
    "/path/to/firefox/profile",
)
WINDOW_SIZE = (1200, 900)  # dimensioni desktop comode
# Emulazione disattivata: uso profilo desktop così com'è

# === MODALITÀ DEBUG INTERATTIVA ===
MANUAL_SAVE_MODE = False  # Se True, lo script si ferma prima di cliccare Fine e aspetta input utente
DEBUG_LOG_FILE = "debug_process_log.txt"  # File di log dettagliato
DEBUG_VERBOSE = True  # Se False, riduce logging dettagliato stato editor
VERIFY_REFRESH = False  # Se True, fa refresh pagina durante verifica (su reel può tornare alla pagina iniziale)
VERIFY_STRICT = True  # Se False, non blocca il processo quando la verifica fallisce
VERIFY_SKIP_FOR_REELS = True  # Se True, evita il fallimento verifica su reel/real
VERIFY_AFTER_SAVE = False  # Se True, esegue verifica post-salvataggio sui post normali
CHECK_ONLY_EVERY = 0  # Ogni N post (indice JSON): verifica extra post-salvataggio (0=disattivato)
LOCK_WAIT_SECONDS = 12  # Attende rilascio lock profilo prima di fallire
AUTO_CLEAR_STALE_LOCK = True  # Rimuove lock residui se Firefox non è in esecuzione
RESULT_LOG_PATH = "output/edited_posts.json"  # Report JSON delle modifiche
LAST_ERROR_MESSAGE = ""
WAIT_TARGET_SECONDS = 240  # Durata totale per post (modifica + pausa) in secondi
CHECK_ONLY_EXTRA_SLEEP_MIN = 20  # Minuti di attesa extra dopo la verifica extra
CHECK_ONLY_EXTRA_SLEEP_MAX = 30  # Max minuti di attesa extra dopo la verifica extra
LONG_PAUSE_EVERY = 15  # Pausa lunga ogni N post (indice JSON)
LONG_PAUSE_MIN = 20  # Minuti di pausa lunga
LONG_PAUSE_MAX = 30  # Max minuti di pausa lunga
SKIP_RANDOM_PAUSES = os.getenv("SKIP_RANDOM_PAUSES", "0").strip().lower() in ["1", "true", "yes", "si", "on"]

# === CHECKPOINT ===
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "edit_instagram_post"
CHECKPOINT_ENABLED = os.getenv("CHECKPOINT_ENABLED", "1").strip().lower() not in ["0", "false", "no"]
DEFAULT_JSON_PATH = "Testi/output_openai_text_edited.json"

# === TIMER ===
_TIMERS = {}
def timer_start(label):
    _TIMERS[label] = time.perf_counter()
    log_debug(f"[TIMER START] {label}")

def timer_end(label):
    if label in _TIMERS:
        dur = time.perf_counter() - _TIMERS[label]
        log_debug(f"[TIMER END] {label}: {dur:.3f}s")
        _TIMERS[label] = dur  # sovrascrivo con la durata finale
    else:
        log_debug(f"[TIMER WARN] Timer '{label}' non avviato")

def log_debug(msg, include_timestamp=True):
    """Scrive nel log di debug."""
    timestamp = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] " if include_timestamp else ""
    log_line = f"{timestamp}{msg}"
    print(log_line)
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def append_result_log(entry):
    """Appende un record al report JSON delle modifiche."""
    path = Path(RESULT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = []
    if not isinstance(data, list):
        data = []
    data.append(entry)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


def normalize_text(text):
    """Normalizza spazi e caratteri invisibili per confronti più robusti."""
    if not text:
        return ""
    cleaned = text.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def capture_editor_state(driver, label=""):
    """Cattura e logga lo stato completo dell'editor e del dialog."""
    try:
        log_debug(f"\n{'='*60}")
        log_debug(f"[STATO EDITOR - {label}]")
        log_debug(f"{'='*60}")
        
        # 1. Stato del campo editor
        try:
            editor = driver.find_element(By.XPATH, "//div[@role='dialog']//*[@contenteditable='true']")
            editor_text = (editor.text or '').strip()
            editor_html = driver.execute_script("return arguments[0].innerHTML;", editor)
            log_debug(f"Editor text: '{editor_text}'")
            log_debug(f"Editor text length: {len(editor_text)}")
            log_debug(f"Editor HTML (primi 200 char): {editor_html[:200]}")
            log_debug(f"Editor is_displayed: {editor.is_displayed()}")
            log_debug(f"Editor is_enabled: {editor.is_enabled()}")
        except Exception as e:
            log_debug(f"Editor non accessibile: {e}")
        
        # 2. Stato del pulsante Fine
        try:
            btn = driver.find_element(By.XPATH, "//div[@role='dialog']//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done']")
            log_debug(f"Pulsante Fine trovato: {btn.is_displayed()}, enabled: {btn.is_enabled()}")
            btn_classes = btn.get_attribute("class")
            log_debug(f"Pulsante classi: {btn_classes}")
            btn_aria = btn.get_attribute("aria-disabled")
            log_debug(f"Pulsante aria-disabled: {btn_aria}")
        except Exception as e:
            log_debug(f"Pulsante Fine non accessibile: {e}")
        
        # 3. Controlla se ci sono cambiamenti pending
        try:
            has_changes = driver.execute_script("""
                // Cerca indicatori di modifiche non salvate
                const dialog = document.querySelector('[role="dialog"]');
                if (!dialog) return 'no-dialog';
                
                // Cerca bottoni disabilitati o indicatori
                const saveBtn = Array.from(dialog.querySelectorAll('[role="button"]'))
                    .find(btn => btn.textContent.trim() === 'Fine' || btn.textContent.trim() === 'Done');
                
                if (!saveBtn) return 'no-save-button';
                
                const isDisabled = saveBtn.getAttribute('aria-disabled') === 'true' || 
                                  saveBtn.disabled || 
                                  saveBtn.classList.contains('disabled');
                
                return {
                    buttonDisabled: isDisabled,
                    buttonClasses: saveBtn.className,
                    hasAriaDisabled: saveBtn.hasAttribute('aria-disabled'),
                    ariaDisabledValue: saveBtn.getAttribute('aria-disabled')
                };
            """)
            log_debug(f"Stato JS pulsante: {has_changes}")
        except Exception as e:
            log_debug(f"Check JS fallito: {e}")
        
        log_debug(f"{'='*60}\n")
    except Exception as e:
        log_debug(f"[ERRORE capture_editor_state]: {e}")


def human_pause(min_s=0.25, max_s=0.8):
    """Pausa con durata random per simulare tempi umani."""
    try:
        time.sleep(random.uniform(min_s, max_s))
    except Exception:
        time.sleep(min_s)


def human_click(driver, element, jitter_px=0):
    """Click diretto (legacy)."""
    try:
        element.click()
    except Exception:
        try:
            ActionChains(driver).move_to_element(element).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
    human_pause()


def human_mouse_click(driver, element, jitter_attempts=2):
    """Simula un hover progressivo verso il centro e click con lieve jitter."""
    try:
        chain = ActionChains(driver)
        chain.move_to_element(element).pause(random.uniform(0.12, 0.25)).perform()
        rect = element.rect
        w = rect.get('width', element.size.get('width'))
        h = rect.get('height', element.size.get('height'))
        for _ in range(jitter_attempts):
            jx = random.uniform(-0.2*w, 0.2*w)
            jy = random.uniform(-0.2*h, 0.2*h)
            try:
                ActionChains(driver).move_by_offset(jx, jy).pause(random.uniform(0.05,0.12)).perform()
            except Exception:
                break
        # Ri-centra prima del click
        try:
            ActionChains(driver).move_to_element(element).pause(random.uniform(0.05,0.1)).click().perform()
        except Exception:
            element.click()
        human_pause(0.18, 0.35)
    except Exception:
        human_click(driver, element)


def robust_click_and_close(driver, button, dialog_xpath="//div[@role='dialog']", attempts=3):
    """Esegue vari tentativi di click e verifica chiusura dialog. Ritorna True se chiuso."""
    for i in range(1, attempts+1):
        try:
            if i == 1:
                human_mouse_click(driver, button)
            elif i == 2:
                human_click(driver, button)
            else:
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));" , button)
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));" , button)
                driver.execute_script("arguments[0].click();", button)
            # Attesa breve per reazione UI
            human_pause(0.3, 0.6)
            # Verifica chiusura
            try:
                WebDriverWait(driver, 6).until(EC.invisibility_of_element_located((By.XPATH, dialog_xpath)))
                return True
            except Exception:
                pass
        except Exception:
            pass
    return False


def scroll_into_view(driver, element):
    """Scrolla l'elemento in vista via JS (aiuta con header post)."""
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
    human_pause(0.2, 0.4)


def ensure_profile_ready():
    """Verifica esistenza profilo e lock."""
    profile_path = Path(FIREFOX_PROFILE_PATH)
    if not profile_path.exists():
        print(f"[ERRORE] Profilo Firefox non trovato: {FIREFOX_PROFILE_PATH}")
        sys.exit(1)
    lock_files = ["lock", ".parentlock"]
    lock_paths = [profile_path / lf for lf in lock_files if (profile_path / lf).exists()]
    if not lock_paths:
        return

    def firefox_running() -> bool:
        proc_root = Path("/proc")
        try:
            for pid in proc_root.iterdir():
                if not pid.name.isdigit():
                    continue
                cmd_path = pid / "cmdline"
                try:
                    cmdline = cmd_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if "firefox" in cmdline or "geckodriver" in cmdline:
                    return True
        except Exception:
            return False
        return False

    start = time.time()
    while time.time() - start < LOCK_WAIT_SECONDS:
        # Se i lock spariscono, ok
        lock_paths = [profile_path / lf for lf in lock_files if (profile_path / lf).exists()]
        if not lock_paths:
            return
        # Se non ci sono processi Firefox, rimuovi lock residui
        if AUTO_CLEAR_STALE_LOCK and not firefox_running():
            for lp in lock_paths:
                try:
                    lp.unlink()
                    print(f"[WARN] Rimosso lock residuo: {lp}")
                except Exception:
                    pass
            # Ricontrolla
            lock_paths = [profile_path / lf for lf in lock_files if (profile_path / lf).exists()]
            if not lock_paths:
                return
        time.sleep(0.5)

    # Se siamo qui, lock ancora presente
    for lp in lock_paths:
        print(f"[ERRORE] Profilo in uso. Chiudi Firefox e riprova. Lock: {lp}")
    sys.exit(1)


def clear_profile_locks():
    """Rimuove i lock del profilo dopo la chiusura del driver."""
    profile_path = Path(FIREFOX_PROFILE_PATH).expanduser()
    lock_files = ["lock", ".parentlock"]
    for lf in lock_files:
        lp = profile_path / lf
        if lp.exists():
            try:
                lp.unlink()
                print(f"[WARN] Lock rimosso a fine modifica: {lp}")
            except Exception:
                pass


def avvia_driver():
    """Avvia Firefox con profilo dedicato."""
    ensure_profile_ready()
    firefox_options = Options()
    profile_path = Path(FIREFOX_PROFILE_PATH).expanduser()
    # Forza il profilo specifico con argomento -profile (più affidabile delle API legacy)
    firefox_options.add_argument("-profile")
    firefox_options.add_argument(str(profile_path))
    print(f"[INFO] Avvio Firefox con profilo: {profile_path}")
    driver = webdriver.Firefox(options=firefox_options)
    driver.set_window_size(*WINDOW_SIZE)
    return driver


def attendi_pagina(driver, timeout=35):
    """Attende caricamento del post: preferisce l'icona opzioni o l'elemento article.
    Usa selettori robusti per SVG e include fallback su banner cookie o overlay.
    """
    print(f"[DEBUG] attendi_pagina: timeout={timeout}s")
    targets = [
        (By.XPATH, "//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]"),
        (By.XPATH, "//*[name()='svg' and (@aria-label='Altro' or @aria-label='More' or @aria-label='More actions' or @aria-label='Options')]"),
        (By.XPATH, "//button[@aria-label='More actions' or @aria-label='More options' or @aria-label='Opzioni' or @aria-label='Altro']"),
        (By.XPATH, "//div[@role='button' and (@aria-label='More actions' or @aria-label='More options' or @aria-label='Opzioni' or @aria-label='Altro')]"),
        (By.XPATH, "//button[.='⋯' or .='…']"),
        (By.XPATH, "//div[@role='button'][.='⋯' or .='…']"),
        (By.XPATH, "//article"),
        (By.XPATH, "//div[@role='dialog']"),  # overlay/dialog visibile
        (By.XPATH, "//button[contains(., 'Accetta') or contains(., 'Consenti') or contains(., 'Accept') or contains(., 'Allow')]")
    ]
    for locator in targets:
        try:
            print(f"[DEBUG] attendi_pagina: aspetto {locator}")
            return WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator))
        except TimeoutException:
            continue
    # Se compare la pagina di login, avvisa esplicitamente
    try:
        driver.find_element(By.NAME, "username")
        raise TimeoutException("Sembra che Instagram chieda il login. Accedi nel profilo e riprova.")
    except Exception:
        pass
    raise TimeoutException(f"Post non caricato entro {timeout}s. URL corrente: {driver.current_url}")


def accetta_cookie(driver, timeout=8):
    selettori = [
        "//button[contains(., 'Accetta') or contains(., 'Consenti')]",
        "//button[contains(., 'Accept') or contains(., 'Allow')]",
        "//div[@role='button' and (contains(., 'Accetta') or contains(., 'Consenti'))]",
        "//div[@role='button' and (contains(., 'Accept') or contains(., 'Allow'))]",
    ]
    for sel in selettori:
        try:
            btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, sel)))
            human_click(driver, btn)
            print("[INFO] Cookie accettati.")
            return True
        except TimeoutException:
            continue
    print("[INFO] Nessun banner cookie da gestire.")
    return False


def ensure_post_view(driver, timeout=10):
    """Se la pagina mostra un overlay/contenitore, clicca 'Vai al post'/'View post'."""
    selectors = (
        "//button[contains(., 'Vai al post') or contains(., 'View post') or contains(., 'Go to post')]",
        "//div[@role='button' and (contains(., 'Vai al post') or contains(., 'View post') or contains(., 'Go to post'))]",
        "//a[contains(., 'Vai al post') or contains(., 'View post') or contains(., 'Go to post')]",
    )
    for sel in selectors:
        try:
            btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, sel)))
            print(f"[INFO] Trovato link 'Vai al post' ({sel}), lo clicco...")
            human_click(driver, btn)
            try:
                attendi_pagina(driver, timeout=12)
                print("[DEBUG] Navigato alla vista post.")
            except Exception as exc:
                print(f"[WARN] Dopo 'Vai al post' attesa fallita: {exc}")
            return True
        except TimeoutException:
            continue
    return False


def ensure_reel_go_to_post(driver, timeout=10):
    """Su permalink /reel/ che aprono l'overlay, apre il menu e clicca 'Vai al post'."""
    # Porta in alto: il menu dei 3 puntini è in header
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

    menu_selectors = (
        "//button[@aria-label='More actions']",
        "//button[@aria-label='More options']",
        "//button[@aria-label='Opzioni']",
        "//button[.//*[name()='svg' and (@aria-label='Altro' or @aria-label='More' or @aria-label='More actions' or @aria-label='Options')]]",
        "//div[@role='button'][.//*[name()='svg' and (@aria-label='Altro' or @aria-label='More' or @aria-label='More actions' or @aria-label='Options')]]",
        "//button[contains(., 'Altro') or contains(., 'More')]",
        "//div[@role='button'][contains(., 'Altro') or contains(., 'More')]",
        "//button[.='⋯' or .='…']",
    )
    menu_btn = None
    for sel in menu_selectors:
        try:
            menu_btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, sel)))
            if menu_btn:
                break
        except Exception:
            continue
    if not menu_btn:
        return False
    scroll_into_view(driver, menu_btn)
    human_click(driver, menu_btn)
    human_pause(0.2, 0.4)

    item_selectors = (
        "//div[@role='menu']//*[normalize-space()='Vai al post' or normalize-space()='View post' or normalize-space()='Go to post']/ancestor::*[@role='menuitem' or self::button or @role='button'][1]",
        "//*[normalize-space()='Vai al post' or normalize-space()='View post' or normalize-space()='Go to post']/ancestor::*[@role='menuitem' or self::button or @role='button'][1]",
        "//span[normalize-space()='Vai al post' or normalize-space()='View post' or normalize-space()='Go to post']/ancestor::*[@role='menuitem' or self::button or @role='button' or self::div][1]",
        "//div[normalize-space()='Vai al post' or normalize-space()='View post' or normalize-space()='Go to post']",
    )
    for sel in item_selectors:
        try:
            item = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, sel)))
            human_click(driver, item)
            try:
                attendi_pagina(driver, timeout=12)
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def clicca_opzioni(driver, timeout=20):
    """Trova e clicca il pulsante dei tre puntini (opzioni) su un post Instagram."""
    print("[DEBUG] Attendo che l'icona 'Altre opzioni/More options' compaia...")
    svg_variants = (
        "//article//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]",
        "//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]",
    )

    # Attendi la presenza dell'icona in pagina
    found_svg = None
    for sel in svg_variants:
        try:
            found_svg = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, sel))
            )
            if found_svg:
                break
        except Exception:
            continue
    btn = None
    if found_svg:
        # Trova un bottone cliccabile che contiene tale icona
        print("[DEBUG] Cerco il bottone che contiene l'icona...")
        button_variants = (
            "//article//button[.//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
            "//div[@role='button' and .//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
            "//button[.//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
        )
        for bx in button_variants:
            try:
                btn = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((By.XPATH, bx))
                )
                if btn:
                    break
            except Exception:
                continue

    if not btn:
        print("[DEBUG] Fallback: cerco pulsanti opzioni con selettori alternativi...")
        fallback_variants = (
            "//button[@aria-label='More actions' or @aria-label='More options' or @aria-label='Opzioni' or @aria-label='Altro']",
            "//div[@role='button' and (@aria-label='More actions' or @aria-label='More options' or @aria-label='Opzioni' or @aria-label='Altro')]",
            "//button[.//*[name()='svg' and (@aria-label='Altro' or @aria-label='More' or @aria-label='More actions' or @aria-label='Options')]]",
            "//div[@role='button'][.//*[name()='svg' and (@aria-label='Altro' or @aria-label='More' or @aria-label='More actions' or @aria-label='Options')]]",
            "//button[contains(., 'Altro') or contains(., 'More')]",
            "//div[@role='button'][contains(., 'Altro') or contains(., 'More')]",
            "//button[.='⋯' or .='…']",
            "//div[@role='button'][.='⋯' or .='…']",
        )
        for bx in fallback_variants:
            try:
                btn = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((By.XPATH, bx))
                )
                if btn:
                    break
            except Exception:
                continue
    if not btn:
        print("[ERRORE] Pulsante opzioni non cliccabile con i selettori noti")
        raise TimeoutException("Bottone opzioni non cliccabile")

    scroll_into_view(driver, btn)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)
    print("[INFO] Menu opzioni cliccato.")


def dump_debug(driver, prefix):
    """Forza il dump di sorgente e screenshot per analisi."""
    try:
        html_path = f"{prefix}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"[DEBUG] Salvato HTML in {html_path}")
    except Exception as exc:
        print(f"[WARN] Impossibile salvare HTML: {exc}")
    try:
        screenshot_path = f"{prefix}.png"
        driver.save_screenshot(screenshot_path)
        print(f"[DEBUG] Salvato screenshot in {screenshot_path}")
    except Exception as exc:
        print(f"[WARN] Impossibile salvare screenshot: {exc}")


def get_menu_items(driver):
    """Ritorna una lista dei testi visibili nel menu/dialog aperto."""
    try:
        items = driver.execute_script("""
            const root = document.querySelector('[role="dialog"], [role="menu"]') || document.body;
            const els = root.querySelectorAll('[role="menuitem"], [role="button"], button, div, span');
            const out = [];
            els.forEach(el => {
                const t = (el.textContent || '').trim();
                if (t && t.length <= 80) out.push(t);
            });
            return Array.from(new Set(out));
        """)
        return items or []
    except Exception:
        return []


def dump_menu_items(driver, label=""):
    """Logga i testi visibili nel menu/dialog aperto per debug selettori."""
    try:
        items = get_menu_items(driver)
        if items:
            log_debug(f"[DEBUG] Menu items {label}: {items}", include_timestamp=False)
    except Exception as exc:
        log_debug(f"[WARN] Dump menu items fallito: {exc}", include_timestamp=False)


def inserisci_testo_via_js(driver, element, text, clear_first=True):
    """Inserisce testo nell'editor via JS usando codepoint per gestire emoji/non-BMP."""
    codepoints = [ord(ch) for ch in text]
    driver.execute_script(
        """
        const el = arguments[0];
        const cps = arguments[1] || [];
        const clear = arguments[2];
        el.focus();
        let out = '';
        for (let i = 0; i < cps.length; i++) {
            out += String.fromCodePoint(cps[i]);
        }
        try {
            if (clear) {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
            }
            if (document.queryCommandSupported && document.queryCommandSupported('insertText')) {
                document.execCommand('insertText', false, out);
            } else if (clear) {
                el.textContent = out;
            } else {
                el.textContent += out;
            }
        } catch (e) {
            if (clear) {
                el.textContent = out;
            } else {
                el.textContent += out;
            }
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        codepoints,
        clear_first,
    )


def leggi_testo_editor(driver, element):
    """Legge il testo dall'editor senza modificarlo (simula una selezione/copia)."""
    try:
        return driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return '';
            const tag = (el.tagName || '').toLowerCase();
            if (tag === 'textarea') {
                el.focus();
                try { el.select(); } catch (e) {}
                return el.value || '';
            }
            el.focus();
            try {
                const range = document.createRange();
                range.selectNodeContents(el);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                return sel.toString();
            } catch (e) {
                return (el.innerText || el.textContent || '');
            }
            """,
            element,
        )
    except Exception:
        try:
            return (element.text or "").strip()
        except Exception:
            return ""


def verifica_editor_post_salvato(driver, permalink, nuovo_testo, timeout=12):
    """Riapre la procedura di modifica e verifica il testo in editor dopo il salvataggio."""
    human_pause(0.6, 1.0)
    clicca_opzioni(driver)
    if permalink_richiede_gestisci_post(permalink, driver.current_url):
        menu_items = get_menu_items(driver)
        lower_items = [it.lower() for it in menu_items]
        has_manage = any("gestisci" in it or "manage" in it for it in lower_items)
        has_modify = any("modifica" in it or "edit" in it for it in lower_items)
        if has_manage and not has_modify:
            clicca_gestisci_post(driver)
    clicca_modifica(driver)

    editor_selectors = (
        "//div[@role='dialog']//*[@data-lexical-editor='true']",
        "//div[@role='dialog']//*[@contenteditable='true']",
        "//div[@role='dialog']//*[@role='textbox']",
        "//div[@role='dialog']//textarea",
    )
    editor_field = find_element_with_selectors(driver, editor_selectors, timeout, clickable=False)
    if not editor_field:
        raise TimeoutException("Campo didascalia non trovato per verifica extra")
    editor_text = (leggi_testo_editor(driver, editor_field) or "").strip()
    target_norm = normalize_text(nuovo_testo)
    editor_norm = normalize_text(editor_text)
    if target_norm and target_norm in editor_norm:
        print("[INFO] Verifica extra OK: testo presente nell'editor.")
        return True
    raise TimeoutException("Verifica extra fallita: testo non presente nell'editor")


def find_element_with_selectors(scope, selectors, timeout, clickable=False):
    condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
    end_time = time.time() + timeout
    last_exc = None
    for sel in selectors:
        remaining = max(0.5, end_time - time.time())
        if remaining <= 0:
            break
        try:
            return WebDriverWait(scope, remaining).until(condition((By.XPATH, sel)))
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        return None
    return None


def clicca_gestisci_post(driver, timeout=15):
    """Seleziona la voce 'Gestisci post/Manage post' dal menu contestuale."""
    text_lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    aria_lower = "translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    candidates = (
        f"//div[@role='dialog']//*[self::button or @role='button' or @role='menuitem'][contains({text_lower}, 'gestisci post') or contains({text_lower}, 'gestisci il post') or contains({text_lower}, 'manage post') or contains({text_lower}, 'manage reel')]",
        f"//*[self::button or @role='button' or @role='menuitem'][contains({text_lower}, 'gestisci post') or contains({text_lower}, 'gestisci il post') or contains({text_lower}, 'manage post') or contains({text_lower}, 'manage reel')]",
        f"//*[@aria-label and (self::button or @role='button' or @role='menuitem') and (contains({aria_lower}, 'gestisci') or contains({aria_lower}, 'manage'))]",
        f"//div[@role='dialog']//*[self::div or self::span][contains({text_lower}, 'gestisci') or contains({text_lower}, 'manage')]/ancestor::*[@role='menuitem' or @role='button' or self::button][1]",
        f"//*[self::div or self::span][contains({text_lower}, 'gestisci') or contains({text_lower}, 'manage')]/ancestor::*[@role='menuitem' or @role='button' or self::button][1]",
    )
    voce = None
    for xp in candidates:
        try:
            voce = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            if voce:
                break
        except Exception:
            continue
    if not voce:
        dump_menu_items(driver, label="(gestisci-post)")
        raise TimeoutException("Voce 'Gestisci post' non trovata nel menu")
    human_click(driver, voce)


def clicca_modifica(driver, timeout=15):
    """Seleziona la voce 'Modifica' dal menu contestuale del post."""
    text_lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    aria_lower = "translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    candidates = (
        # Dialog/menu variants with direct button text
        f"//div[@role='dialog']//*[self::button or @role='button' or @role='menuitem'][contains({text_lower}, 'modifica') or contains({text_lower}, 'edit')]",
        f"//*[self::button or @role='button' or @role='menuitem'][contains({text_lower}, 'modifica') or contains({text_lower}, 'edit')]",
        f"//*[@aria-label and (self::button or @role='button' or @role='menuitem') and (contains({aria_lower}, 'modifica') or contains({aria_lower}, 'edit'))]",
        # Nested text inside spans/divs
        f"//div[@role='dialog']//*[self::div or self::span][contains({text_lower}, 'modifica') or contains({text_lower}, 'edit')]/ancestor::*[@role='menuitem' or @role='button' or self::button][1]",
        f"//*[self::div or self::span][contains({text_lower}, 'modifica') or contains({text_lower}, 'edit')]/ancestor::*[@role='menuitem' or @role='button' or self::button][1]",
    )
    voce = None
    for xp in candidates:
        try:
            voce = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            if voce:
                break
        except Exception:
            continue
    if not voce:
        dump_menu_items(driver, label="(modifica)")
        raise TimeoutException("Voce 'Modifica' non trovata nel menu")
    human_click(driver, voce)


def aggiorna_testo(driver, nuovo_testo, timeout=15):
    """Imposta la didascalia con strategia a parole (più naturale per Lexical):
    1. Trova editor
    2. CTRL+A + Backspace per pulire
    3. Digita parola per parola con spazio e pause brevi
    4. Verifica; se mismatch, fallback a digitazione carattere per carattere
    """
    dialog_xpath = "//div[@role='dialog']"
    try:
        dlg = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, dialog_xpath)))
    except Exception:
        raise TimeoutException("Dialog di modifica non trovato")

    aria_lower = "translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    placeholder_lower = "translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    labeled_selectors = (
        f".//*[self::textarea or @role='textbox' or @contenteditable='true'][@aria-label and (contains({aria_lower}, 'didascalia') or contains({aria_lower}, 'caption') or contains({aria_lower}, 'scrivi') or contains({aria_lower}, 'write'))]",
        f".//*[self::textarea or @role='textbox' or @contenteditable='true'][@placeholder and (contains({placeholder_lower}, 'didascalia') or contains({placeholder_lower}, 'caption'))]",
        ".//textarea[@name='caption' or @name='description']",
    )
    generic_selectors = (
        ".//*[@data-lexical-editor='true']",
        ".//*[@contenteditable='true']",
        ".//*[@role='textbox']",
        ".//textarea",
    )

    area = find_element_with_selectors(dlg, labeled_selectors, timeout, clickable=False)
    if not area:
        area = find_element_with_selectors(dlg, generic_selectors, timeout, clickable=False)
    if not area:
        # Fallback globale (alcune viste non usano dialog tradizionale)
        area = find_element_with_selectors(driver, labeled_selectors, timeout, clickable=False)
    if not area:
        area = find_element_with_selectors(driver, generic_selectors, timeout, clickable=False)
    if not area:
        try:
            counts = driver.execute_script("""
                return {
                    contenteditable: document.querySelectorAll('[contenteditable=\"true\"]').length,
                    textareas: document.querySelectorAll('textarea').length,
                    textboxes: document.querySelectorAll('[role=\"textbox\"]').length
                };
            """)
            log_debug(f"[DEBUG] Nessun editor trovato. Counts: {counts}", include_timestamp=False)
        except Exception:
            pass
        raise TimeoutException("Campo didascalia non trovato")

    scroll_into_view(driver, area)
    human_pause(0.3, 0.5)
    try:
        area.click()
    except Exception:
        driver.execute_script("arguments[0].click();", area)
    human_pause(0.15, 0.25)
    try:
        driver.execute_script("arguments[0].focus();", area)
    except Exception:
        pass
    human_pause(0.15, 0.25)

    from selenium.webdriver.common.keys import Keys
    has_emoji = any(ord(ch) > 0xFFFF for ch in nuovo_testo)
    if has_emoji:
        print("[INFO] Digitazione simulata con supporto emoji (JS solo per non-BMP).")
    try:
        # Pulizia semplice
        area.send_keys(Keys.CONTROL, 'a')
        human_pause(0.05, 0.15)
        area.send_keys(Keys.BACK_SPACE)
        human_pause(0.2, 0.35)

        # Digitazione parola per parola
        parole = []
        # Preserviamo eventuali newline: split su spazi ma manteniamo '\n'
        for segment in nuovo_testo.split('\n'):
            seg_words = segment.split()
            parole.extend(seg_words)
            parole.append('\n')  # marker newline
        if parole and parole[-1] == '\n':
            parole.pop()  # rimuove newline finale superflua

        for w in parole:
            if w == '\n':
                area.send_keys(Keys.SHIFT, Keys.ENTER)  # newline soft
                human_pause(0.15, 0.3)
                continue
            for ch in w:
                if ord(ch) > 0xFFFF:
                    inserisci_testo_via_js(driver, area, ch, clear_first=False)
                else:
                    area.send_keys(ch)
                if ch in ['.', ',', '!', '?', ':', ';']:
                    human_pause(0.05, 0.12)
                else:
                    human_pause(0.02, 0.06)
            area.send_keys(' ')
            human_pause(0.02, 0.06)

        human_pause(0.4, 0.7)

        # Verifica base dopo parole
        current = normalize_text(leggi_testo_editor(driver, area))
        target = nuovo_testo.strip()
        if target not in current:
            print("[WARN] Mismatch dopo digitazione, fallback a inserimento JS completo...")
            inserisci_testo_via_js(driver, area, nuovo_testo, clear_first=True)
            human_pause(0.5, 0.9)
            current = normalize_text(leggi_testo_editor(driver, area))
            if normalize_text(target) not in current:
                raise TimeoutException("Il testo non è stato impostato correttamente (fallback fallito)")
    except WebDriverException as exc:
        log_debug(f"[WARN] send_keys fallito: {exc}. Uso inserimento JS.")
        inserisci_testo_via_js(driver, area, nuovo_testo, clear_first=True)
        human_pause(0.2, 0.4)

    print("[INFO] Didascalia impostata.")
    
    # Polling rapido disponibilità pulsante Fine invece di sleep fisso
    for _ in range(15):
        try:
            btn_probe = driver.find_element(By.XPATH, "//div[@role='dialog']//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']")
            if btn_probe.is_enabled() and not btn_probe.get_attribute('aria-disabled'):
                break
        except Exception:
            pass
        time.sleep(0.12)

    return area



def estrai_caption_da_sorgente(page_source):
    """Stub legacy (non usata, mantenuta per compatibilità futura)."""
    return ""

def estrai_caption_da_json(page_source):
    """Estrae la caption dal blob JSON della pagina (se presente)."""
    try:
        # Cerca pattern caption":{"text":"..." con escaping
        match = re.search(r'"caption":\{"text":"(.*?)"\}', page_source)
        if match:
            testo = match.group(1)
            # Decodifica sequenze escaped base
            testo = testo.encode('utf-8').decode('unicode_escape')
            return testo
    except Exception:
        pass
    return ""


def salva(driver, timeout=15, manual_mode=False):
    """Individua il pulsante di conferma. Se manual_mode=True, NON clicca e attende input utente."""
    human_pause(0.4, 0.7)

    dialog_candidates = (
        "//div[@role='dialog' and contains(@aria-label, 'Modifica informazioni')]",
        "//div[@role='dialog' and contains(@aria-label, 'Edit info')]",
        "//div[@role='dialog']",
    )
    dlg = None
    for dsel in dialog_candidates:
        try:
            dlg = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, dsel)))
            if dlg:
                break
        except Exception:
            continue
    if not dlg:
        raise TimeoutException("Dialog di salvataggio non disponibile")

    # Selettori più estesi: Instagram può usare <div role=button> invece di <button>
    button_selectors_dialog = (
        ".//button[normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']",
        ".//*[self::div or self::span][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']/ancestor::*[@role='button' or self::button][1]",
        ".//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']",
        ".//*[@role='button'][.//*[self::div or self::span][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']]",
    )
    btn = None
    for xp in button_selectors_dialog:
        try:
            btn = WebDriverWait(dlg, timeout).until(EC.element_to_be_clickable((By.XPATH, xp)))
            if btn:
                print(f"[DEBUG] Pulsante conferma (dialog) trovato: {xp}")
                break
        except Exception:
            continue
    # Fallback globale se non trovato nel dialog (a volte il layer cambia struttura)
    if not btn:
        print("[DEBUG] Fallback ricerca globale pulsante 'Fine/Done/Fatto'...")
        global_selectors = (
            "//button[normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']",
            "//*[self::div or self::span][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']/ancestor::*[@role='button' or self::button][1]",
            "//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']",
            "//*[@role='button'][.//*[self::div or self::span][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']]",
        )
        for xp in global_selectors:
            try:
                btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xp)))
                if btn:
                    print(f"[DEBUG] Pulsante conferma (globale) trovato: {xp}")
                    break
            except Exception:
                continue
    if not btn:
        raise TimeoutException("Bottone di salvataggio non trovato")

    # Verifica rapida pulsante
    time.sleep(0.3)

    scroll_into_view(driver, btn)
    human_pause(0.2, 0.35)
    
    if manual_mode:
        print("[MODALITÀ MANUALE] Clicca 'Fine' nel browser, poi premi INVIO qui.")
        input(">>> Premi INVIO dopo aver cliccato manualmente 'Fine': ")
        human_pause(2.0, 3.0)
        return True
    
    # Modalità automatica: tentativi robusti con verifica chiusura dialog
    closed = robust_click_and_close(driver, btn)
    if not closed:
        print("[WARN] Dialog non chiuso dopo tentativi, procedo comunque.")

    print("[DEBUG] Attendo chiusura dialog...")
    try:
        WebDriverWait(driver, 10).until(EC.invisibility_of_element_located((By.XPATH, "//div[@role='dialog']")))
        print("[DEBUG] Dialog chiuso.")
    except Exception as e:
        print(f"[WARN] Dialog non si è chiuso rapidamente: {e}")

    print("[DEBUG] Pausa per aggiornamento SPA...")
    human_pause(1.5, 2.5)

    return True


def leggi_didascalia_corrente(driver, timeout=10):
    """Legge la didascalia corrente dalla pagina del post dopo il salvataggio.
    Nota: data-lexical-text='true' esiste solo nell'editor, non nella pagina del post.
    Sulla pagina normale la didascalia è in <h1> o span dentro article.
    """
    candidates = (
        # Selettori per la pagina del post normale (non l'editor)
        "//article//h1",
        "//article//h1//span",
        "//article//div[contains(@class, 'C4VMK')]//span",  # classe container didascalia
        "//article//span[contains(text(), ' ')]",  # span con testo significativo
        # Fallback generici
        "//article//span[@dir='auto']",
        "//article//span",
    )
    
    # Prova tutti i selettori e raccoglie i testi trovati
    texts_found = []
    for xp in candidates:
        try:
            elements = driver.find_elements(By.XPATH, xp)
            for el in elements:
                text = el.text.strip()
                if text and len(text) > 3:  # ignora testi troppo corti (pulsanti, etc)
                    texts_found.append(text)
                    print(f"[DEBUG] Testo trovato con {xp}: '{text[:50]}...'")
        except Exception:
            continue
        # Fallback JS se nulla trovato con XPath noti
        if not texts_found:
                try:
                        js_texts = driver.execute_script("""
const arts = document.querySelectorAll('article');
let out = [];
arts.forEach(a => {
    a.querySelectorAll('*').forEach(el => {
        if (el.childElementCount === 0) {
            const t = (el.textContent || '').trim();
            if (t.length > 25 && !/^\\d+[smhd]$/.test(t) && !/^\\d+\\s*commenti?$/i.test(t) && !/^Mostra altro$/i.test(t) && !/^Traduci$/i.test(t)) {
                out.push(t);
            }
        }
    });
});
return out;
                        """) or []
                        for t in js_texts:
                                print(f"[DEBUG] Fallback JS testo raccolto: '{t[:60]}...'")
                        texts_found.extend(js_texts)
                except Exception as exc:
                        print(f"[WARN] Fallback JS lettura didascalia fallito: {exc}")

        if texts_found:
                longest = max(texts_found, key=len)
                print(f"[DEBUG] Didascalia letta: '{longest[:100]}...'")
                return longest

        print("[WARN] Nessuna didascalia trovata sulla pagina")
        return ""


def attendi_aggiornamento_didascalia(driver, nuovo_testo, tentativi=5, timeout_lettura=6):
    """Tenta più volte di confermare che la didascalia sia aggiornata.
    Effettua refresh moderati se il testo non compare. Dump finale se fallisce.
    """
    target = nuovo_testo.strip()
    for i in range(1, tentativi + 1):
        human_pause(0.6, 1.0)
        caption = leggi_didascalia_corrente(driver, timeout=timeout_lettura).strip()
        if target and target in caption:
            print(f"[INFO] Didascalia confermata al tentativo {i}.")
            return caption
        print(f"[DEBUG] Tentativo {i}: didascalia non ancora aggiornata.")
        # Refresh solo se non è l'ultimo tentativo
        if i < tentativi:
            print("[DEBUG] Effettuo un refresh della pagina per forzare l'aggiornamento...")
            try:
                driver.refresh()
            except Exception:
                pass
    # Fallimento
    raise TimeoutException("La didascalia non risulta aggiornata dopo i tentativi previsti")


def permalink_richiede_gestisci_post(permalink: str | None, current_url: str | None = None) -> bool:
    urls = [u for u in [permalink, current_url] if u]
    for url in urls:
        try:
            path = urlparse(url).path.lower()
        except Exception:
            path = (url or "").lower()
        segments = [seg for seg in path.split("/") if seg]
        if any(seg in ("real", "reel", "reels") for seg in segments):
            return True
    return False


def main(
    permalink: str | None = None,
    nuovo_testo: str | None = None,
    manual_mode: bool | None = None,
    return_success: bool = False,
    check_only: bool = False,
    verify_after_save: bool = True,
):
    """
    Esegue l'aggiornamento della didascalia.
    - permalink/nuovo_testo sovrascrivono i default del file.
    - manual_mode permette di forzare MANUAL_SAVE_MODE.
    - se return_success=True ritorna bool invece di sys.exit.
    """
    global MANUAL_SAVE_MODE, LAST_ERROR_MESSAGE
    permalink = permalink or PERMALINK
    nuovo_testo = nuovo_testo or NUOVO_TESTO
    if manual_mode is not None:
        MANUAL_SAVE_MODE = manual_mode
    LAST_ERROR_MESSAGE = ""

    success = False
    try:
        driver = avvia_driver()
    except Exception as exc:
        print(f"[ERRORE] Avvio Firefox fallito: {exc}")
        if return_success:
            return False
        sys.exit(1)

    try:
        timer_start("TOTAL")
        print("[INFO] Apro il permalink...")
        timer_start("LOAD_PERMALINK")
        print(f"[DEBUG] GET {permalink}")
        driver.get(permalink)
        print(f"[DEBUG] GET completato, current_url={driver.current_url}")
        human_pause(0.6, 1.2)
        try:
            attendi_pagina(driver)
            print("[DEBUG] attendi_pagina completata")
        except TimeoutException as exc:
            print(f"[WARN] attendi_pagina timeout: {exc}. Provo refresh e retry")
            driver.refresh()
            human_pause(0.6, 1.0)
            attendi_pagina(driver)

        # Se siamo in una view intermedia con overlay, prova a cliccare "Vai al post"; sui reel prova anche menu 3 puntini
        view_done = ensure_post_view(driver)
        if not view_done and ("/reel/" in (permalink or "") or "/reel/" in driver.current_url):
            ensure_reel_go_to_post(driver)
        # Cookie step rimosso (profilo già persiste consenso)
        timer_end("LOAD_PERMALINK")

        print("[INFO] Clicco su opzioni...")
        timer_start("OPEN_OPTIONS")
        clicca_opzioni(driver)
        timer_end("OPEN_OPTIONS")

        if permalink_richiede_gestisci_post(permalink, driver.current_url):
            menu_items = get_menu_items(driver)
            lower_items = [it.lower() for it in menu_items]
            has_manage = any("gestisci" in it or "manage" in it for it in lower_items)
            has_modify = any("modifica" in it or "edit" in it for it in lower_items)
            if has_manage and not has_modify:
                print("[INFO] Seleziono 'Gestisci post'...")
                timer_start("OPEN_MANAGE_POST")
                clicca_gestisci_post(driver)
                timer_end("OPEN_MANAGE_POST")
            elif has_modify:
                print("[INFO] Menu principale contiene 'Modifica'; salto 'Gestisci post'.")
            else:
                print("[INFO] Nessuna voce 'Gestisci post' visibile; provo direttamente 'Modifica'.")

        print("[INFO] Seleziono 'Modifica'...")
        timer_start("OPEN_EDIT_DIALOG")
        try:
            clicca_modifica(driver)
        except TimeoutException:
            if permalink_richiede_gestisci_post(permalink, driver.current_url):
                print("[WARN] 'Modifica' non trovata, provo 'Gestisci post' e ritento...")
                timer_start("OPEN_MANAGE_POST_RETRY")
                clicca_gestisci_post(driver)
                timer_end("OPEN_MANAGE_POST_RETRY")
                clicca_modifica(driver)
            else:
                raise
        timer_end("OPEN_EDIT_DIALOG")

        print("[INFO] Aggiorno il testo...")
        timer_start("EDIT_CAPTION")
        editor_el = aggiorna_testo(driver, nuovo_testo)
        timer_end("EDIT_CAPTION")

        log_debug("[INFO] Salvo le modifiche...")
        timer_start("SAVE_OPERATION")
        salva(driver, manual_mode=MANUAL_SAVE_MODE)
        timer_end("SAVE_OPERATION")

        if check_only:
            print("[INFO] Verifica extra post-salvataggio: riapro editor per controllo.")
            verifica_editor_post_salvato(driver, permalink, nuovo_testo)

        if verify_after_save:
            # Reload esplicito del permalink per forzare il re-render dell'articolo e della didascalia
            # Verifica ridotta: prima controllo JSON sulla pagina corrente
            ps = driver.page_source
            timer_start("VERIFY")
            json_caption = estrai_caption_da_json(ps).strip()
            target = nuovo_testo.strip()
            if target and target in json_caption:
                print("[INFO] Salvataggio confermato (JSON, senza reload).")
            else:
                if VERIFY_REFRESH:
                    print("[DEBUG] JSON non conferma, eseguo refresh pagina...")
                    timer_start("POST_RELOAD")
                    try:
                        driver.refresh()
                        human_pause(0.5, 0.9)
                        attendi_pagina(driver)
                    except Exception as exc:
                        print(f"[WARN] Refresh pagina non completamente riuscito: {exc}")
                    timer_end("POST_RELOAD")
                else:
                    print("[DEBUG] JSON non conferma, continuo verifica senza refresh...")
                    human_pause(0.6, 1.0)
                ps = driver.page_source

            json_caption = estrai_caption_da_json(ps).strip()
            dom_caption = leggi_didascalia_corrente(driver).strip()
            target = nuovo_testo.strip()
            target_norm = normalize_text(target)
            json_norm = normalize_text(json_caption)
            dom_norm = normalize_text(dom_caption)
            if target and (target in json_caption or target in dom_caption or target_norm in json_norm or target_norm in dom_norm):
                print("[INFO] Salvataggio confermato (caption presente).")
            else:
                print("[WARN] Caption non confermata, tentativo di riapertura editor per controllo.")
                try:
                    clicca_opzioni(driver)
                    if permalink_richiede_gestisci_post(permalink, driver.current_url):
                        menu_items = get_menu_items(driver)
                        lower_items = [it.lower() for it in menu_items]
                        has_manage = any("gestisci" in it or "manage" in it for it in lower_items)
                        has_modify = any("modifica" in it or "edit" in it for it in lower_items)
                        if has_manage and not has_modify:
                            clicca_gestisci_post(driver)
                    clicca_modifica(driver)
                    # Rileggi dall'editor
                    # Trova campo e leggi
                    editor_text = ""
                    try:
                        editor_field = WebDriverWait(driver, 6).until(
                            EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//*[@contenteditable='true']"))
                        )
                        editor_text = (leggi_testo_editor(driver, editor_field) or "").strip()
                    except Exception:
                        editor_text = ""
                    editor_norm = normalize_text(editor_text)
                    if target in editor_text or target_norm in editor_norm:
                        print("[INFO] Salvataggio confermato dall'editor riaperto.")
                    else:
                        raise TimeoutException("Salvataggio non verificato nei canali JSON/DOM/editor")
                except Exception as ve:
                    strict_verify = VERIFY_STRICT and not (
                        VERIFY_SKIP_FOR_REELS and permalink_richiede_gestisci_post(permalink, driver.current_url)
                    )
                    if strict_verify:
                        raise TimeoutException(f"Verifica fallita: {ve}")
                    print(f"[WARN] Verifica non riuscita, continuo comunque: {ve}")
            timer_end("VERIFY")
        else:
            print("[INFO] Verifica post-salvataggio disattivata.")

        print("[✅] Modifica completata.")
        timer_end("TOTAL")
        log_debug("\n===== RIEPILOGO TEMPI =====")
        for k, v in _TIMERS.items():
            if isinstance(v, (int, float)):
                log_debug(f"{k}: {v:.3f}s")
        log_debug("==========================\n")
        success = True
        # Attesa finale rimossa per snellire

    except TimeoutException as exc:
        LAST_ERROR_MESSAGE = f"TimeoutException: {exc}"
        print(f"[ERRORE] Elemento non trovato nei tempi attesi: {exc}")
    except Exception as exc:
        LAST_ERROR_MESSAGE = f"Exception: {exc}"
        print(f"[ERRORE] Errore inatteso: {exc}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        # Attendi un attimo e rimuovi lock residui (richiesto).
        time.sleep(0.5)
        clear_profile_locks()

    if return_success:
        return success
    if not success:
        sys.exit(1)
    return True


def load_posts_from_json(path: str) -> List[Dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    posts = data.get("posts", []) if isinstance(data, dict) else []
    cleaned = []
    for idx, p in enumerate(posts):
        if not isinstance(p, dict):
            continue
        permalink = p.get("permalink") or p.get("link")
        testo = p.get("messaggio_modificato") or p.get("messaggio_originale")
        if permalink and testo:
            key = p.get("id") or permalink
            cleaned.append({"permalink": permalink, "testo": testo, "id": p.get("id"), "index": idx, "key": key})
    return cleaned


def get_checkpoint_path(input_path: str | None) -> Path:
    base_dir = Path(__file__).parent
    checkpoint_dir = base_dir / CHECKPOINT_DIR / CHECKPOINT_SUBDIR
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    if input_path:
        input_stem = Path(input_path).stem
        name = f"checkpoint_edit_instagram_post_{input_stem}.json"
    else:
        name = "checkpoint_edit_instagram_post.json"
    return checkpoint_dir / name


def load_checkpoint(input_path: str | None) -> Set[str]:
    if not CHECKPOINT_ENABLED or not input_path:
        return set()
    ckpt_path = get_checkpoint_path(input_path)
    if not ckpt_path.exists():
        return set()
    try:
        data = json.loads(ckpt_path.read_text(encoding="utf-8"))
        return set(data.get("processed_keys", [])) if isinstance(data, dict) else set()
    except Exception as exc:
        print(f"[WARN] Impossibile leggere il checkpoint {ckpt_path}: {exc}")
        return set()


def save_checkpoint(input_path: str | None, processed_keys: Set[str]) -> None:
    if not CHECKPOINT_ENABLED or not input_path:
        return
    ckpt_path = get_checkpoint_path(input_path)
    payload = {"processed_keys": sorted(processed_keys)}
    try:
        ckpt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[CKPT] Salvato checkpoint: {ckpt_path}")
    except Exception as exc:
        print(f"[WARN] Impossibile salvare il checkpoint {ckpt_path}: {exc}")


def run_edit(
    permalink: str,
    nuovo_testo: str,
    manual_mode: bool | None = None,
    check_only: bool = False,
    verify_after_save: bool = True,
) -> bool:
    """
    Wrapper per aggiornare una singola caption da altri script.
    Ritorna True/False in base all'esito.
    """
    return main(
        permalink=permalink,
        nuovo_testo=nuovo_testo,
        manual_mode=manual_mode,
        return_success=True,
        check_only=check_only,
        verify_after_save=verify_after_save,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modifica una caption IG o più caption da JSON Testi/output_openai_text_edited.json")
    parser.add_argument("--permalink", help="Permalink del post da modificare")
    parser.add_argument("--text", help="Nuovo testo/caption da applicare")
    parser.add_argument("--manual", action="store_true", help="Modalità manuale: non clicca Fine in automatico")
    parser.add_argument("--profile", help="Percorso profilo Firefox da usare (override di FIREFOX_PROFILE_PATH)")
    parser.add_argument("--json", help="Percorso file JSON (es. Testi/output_openai_text_edited.json)")
    parser.add_argument(
        "--skip-random-pauses",
        action="store_true",
        help="Salta le pause random (pause lunghe e verifiche extra).",
    )
    parser.add_argument("--start", type=int, default=0, help="Indice di partenza nella lista JSON")
    parser.add_argument("--limit", type=int, help="Numero massimo di post da processare")
    args = parser.parse_args()

    if args.profile:
        FIREFOX_PROFILE_PATH = args.profile
        print(f"[INFO] Profilo Firefox impostato da CLI: {FIREFOX_PROFILE_PATH}")

    skip_random_pauses = args.skip_random_pauses or SKIP_RANDOM_PAUSES
    if skip_random_pauses:
        print("[INFO] Skip pause random attivo.")

    if not args.json and not args.permalink and not args.text:
        candidate = Path(__file__).parent / DEFAULT_JSON_PATH
        if candidate.exists():
            args.json = str(candidate)
            print(f"[INFO] Nessun permalink fornito: uso JSON di default {args.json}")
        else:
            print(f"[ERRORE] Nessun input fornito e JSON di default non trovato: {candidate}")
            sys.exit(1)

    if args.json:
        jobs_raw = load_posts_from_json(args.json)
        if args.start:
            jobs_raw = jobs_raw[args.start:]
        if args.limit:
            jobs_raw = jobs_raw[:args.limit]

        if not jobs_raw:
            print("[ERRORE] Nessun post valido trovato nel JSON")
            sys.exit(1)

        processed_keys = load_checkpoint(args.json)
        if processed_keys:
            print(f"[CKPT] Riprendo da checkpoint: {len(processed_keys)} post già completati.")
        jobs = [j for j in jobs_raw if j.get("key") not in processed_keys]

        if not jobs:
            print("[INFO] Tutti i post risultano già completati secondo il checkpoint.")
            sys.exit(0)

        planned = len(jobs)
        success_count = 0
        processed_count = 0
        check_only_count = 0
        aborted = False

        for idx, job in enumerate(jobs, start=1):
            job_start = time.perf_counter()
            check_only = False
            long_pause = False
            json_index = job.get("index") if isinstance(job.get("index"), int) else None
            if CHECK_ONLY_EVERY:
                if json_index is not None:
                    check_only = (json_index + 1) % CHECK_ONLY_EVERY == 0
                else:
                    check_only = idx % CHECK_ONLY_EVERY == 0
            if LONG_PAUSE_EVERY:
                if json_index is not None:
                    long_pause = (json_index + 1) % LONG_PAUSE_EVERY == 0
                else:
                    long_pause = idx % LONG_PAUSE_EVERY == 0
            print(f"[INFO] Processo index={job['index']} id={job.get('id')} permalink={job['permalink']}")
            verify_after_save = VERIFY_AFTER_SAVE and not check_only
            ok = run_edit(
                job["permalink"],
                job["testo"],
                manual_mode=args.manual,
                check_only=check_only,
                verify_after_save=verify_after_save,
            )
            entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "permalink": job.get("permalink"),
                "id": job.get("id"),
                "index": job.get("index"),
                "check_after_save": bool(check_only),
                "success": bool(ok),
                "saved": bool(ok),
            }
            if LAST_ERROR_MESSAGE:
                entry["error"] = LAST_ERROR_MESSAGE
            append_result_log(entry)
            processed_count += 1
            if ok:
                success_count += 1
                if check_only:
                    check_only_count += 1
                processed_keys.add(job["key"])
                save_checkpoint(args.json, processed_keys)

            if processed_count < planned:
                if long_pause and LONG_PAUSE_MAX > 0:
                    if skip_random_pauses:
                        print("[PAUSA] Skip pausa lunga (random) attivo.")
                    else:
                        min_m = max(0, LONG_PAUSE_MIN)
                        max_m = max(min_m, LONG_PAUSE_MAX)
                        extra_s = random.uniform(min_m, max_m) * 60
                        try:
                            user_input = input(">>> Pausa lunga: scrivi 'skip' per saltare, Invio per attendere: ").strip().lower()
                        except KeyboardInterrupt:
                            print("[INFO] Interruzione manuale prima della pausa lunga.")
                            aborted = True
                            break
                        if user_input == "skip":
                            print("[PAUSA] Pausa lunga saltata su richiesta.")
                        else:
                            print(f"[PAUSA] Pausa lunga: attendo {extra_s/60:.1f} min prima del prossimo post...")
                            try:
                                time.sleep(extra_s)
                            except KeyboardInterrupt:
                                print("[INFO] Interruzione manuale durante la pausa lunga.")
                                aborted = True
                                break
                if check_only and CHECK_ONLY_EXTRA_SLEEP_MAX > 0:
                    if skip_random_pauses:
                        print("[PAUSA] Skip pausa verifica extra (random) attivo.")
                    else:
                        min_m = max(0, CHECK_ONLY_EXTRA_SLEEP_MIN)
                        max_m = max(min_m, CHECK_ONLY_EXTRA_SLEEP_MAX)
                        extra_s = random.uniform(min_m, max_m) * 60
                        print(f"[PAUSA] Verifica extra: attendo {extra_s/60:.1f} min prima del prossimo post...")
                        try:
                            time.sleep(extra_s)
                        except KeyboardInterrupt:
                            print("[INFO] Interruzione manuale durante la pausa (verifica extra).")
                            aborted = True
                            break
                if WAIT_TARGET_SECONDS and WAIT_TARGET_SECONDS > 0:
                    elapsed = time.perf_counter() - job_start
                    wait_s = max(0.0, WAIT_TARGET_SECONDS - elapsed)
                    if wait_s > 0:
                        try:
                            prompt = f">>> Pausa pacing {wait_s:.1f}s: scrivi 'skip' per saltare, Invio per attendere: "
                            user_input = input(prompt).strip().lower()
                        except KeyboardInterrupt:
                            print("[INFO] Interruzione manuale prima della pausa pacing.")
                            aborted = True
                            break
                        if user_input == "skip":
                            print("[PAUSA] Pausa pacing saltata su richiesta.")
                        else:
                            print(f"[PAUSA] Attendo {wait_s:.1f}s per arrivare a {WAIT_TARGET_SECONDS:.0f}s totali...")
                            try:
                                time.sleep(wait_s)
                            except KeyboardInterrupt:
                                print("[INFO] Interruzione manuale durante la pausa.")
                                aborted = True
                                break
                    else:
                        print(f"[PAUSA] Nessuna attesa: durata {elapsed:.1f}s >= {WAIT_TARGET_SECONDS:.0f}s.")
                else:
                    print("[PAUSA] Nessuna attesa configurata.")

        print(f"[INFO] Completati {success_count}/{processed_count} post (richiesti {planned}). Checkpoint salvato per eventuale ripresa.")
        if check_only_count:
            print(f"[INFO] Post con verifica extra post-salvataggio: {check_only_count}.")
        if aborted:
            sys.exit(0)
        if success_count != processed_count or processed_count != planned:
            sys.exit(1)
    else:
        main(
            permalink=args.permalink,
            nuovo_testo=args.text,
            manual_mode=args.manual,
            verify_after_save=VERIFY_AFTER_SAVE,
        )
