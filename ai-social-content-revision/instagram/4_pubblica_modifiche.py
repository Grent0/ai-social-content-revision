#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script 4: Pubblica le modifiche su Instagram (via Selenium)
Legge i post modificati da un file JSON e aggiorna le caption
tramite la logica Selenium (ex edit_instagram_post) incorporata qui.
Pubblica solo se `status_meccanico` o `status_ai` sono `modificato`
oppure se il legacy `status` è `modified`.

Uso:
    python 4_pubblica_modifiche.py --input Testi/output_openai_text_edited.json --dry-run
    python 4_pubblica_modifiche.py --input Testi/output_openai_text_edited.json
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
import traceback

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, InvalidArgumentException
from selenium.webdriver import ActionChains


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

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(key: str, default: bool = False) -> bool:
    """
    Legge un booleano da env con fallback.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ["1", "true", "yes", "si", "on"]


load_env_from_file()


#
# === AUTOMAZIONE SELENIUM (copiata da edit_instagram_post.py) ===
#

# Config base per l'editor IG
PERMALINK = os.getenv(
    "INSTAGRAM_PERMALINK",
    "https://www.instagram.com/p/POST_ID/",
)  # placeholder generico: usare il permalink del post reale
NUOVO_TESTO = "Le rinnovabili sono il futuro."
FIREFOX_PROFILE_PATH = os.getenv(
    "FIREFOX_PROFILE_PATH",
    "/path/to/firefox/profile",
)
WINDOW_SIZE = (1200, 900)

# Modalità debug
MANUAL_SAVE_MODE = False
DEBUG_LOG_FILE = "debug_process_log.txt"
DEBUG_VERBOSE = True

# Timer
_TIMERS: Dict[str, float] = {}


def timer_start(label):
    _TIMERS[label] = time.perf_counter()
    log_debug(f"[TIMER START] {label}")


def timer_end(label):
    if label in _TIMERS:
        dur = time.perf_counter() - _TIMERS[label]
        log_debug(f"[TIMER END] {label}: {dur:.3f}s")
        _TIMERS[label] = dur
    else:
        log_debug(f"[TIMER WARN] Timer '{label}' non avviato")


def log_debug(msg, include_timestamp=True):
    timestamp = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] " if include_timestamp else ""
    log_line = f"{timestamp}{msg}"
    print(log_line)
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def capture_editor_state(driver, label=""):
    """Debug avanzato stato editor/dialog."""
    try:
        log_debug(f"\n{'='*60}")
        log_debug(f"[STATO EDITOR - {label}]")
        log_debug(f"{'='*60}")

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

        try:
            btn = driver.find_element(By.XPATH, "//div[@role='dialog']//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done']")
            log_debug(f"Pulsante Fine trovato: {btn.is_displayed()}, enabled: {btn.is_enabled()}")
            btn_classes = btn.get_attribute("class")
            log_debug(f"Pulsante classi: {btn_classes}")
            btn_aria = btn.get_attribute("aria-disabled")
            log_debug(f"Pulsante aria-disabled: {btn_aria}")
        except Exception as e:
            log_debug(f"Pulsante Fine non accessibile: {e}")

        try:
            has_changes = driver.execute_script("""
                const dialog = document.querySelector('[role="dialog"]');
                if (!dialog) return 'no-dialog';
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


def normalize_caption_text(text: str) -> str:
    """
    Normalizza spazi e newline per confronti robusti (evita mismatch per doppi spazi o \n compressi).
    """
    return re.sub(r"\s+", " ", (text or "").strip())


def safe_element_text(element):
    """
    Ritorna il testo di un elemento contenteditable senza scatenare bug di Gecko/hex escape.
    Usa prima .text, se fallisce per InvalidArgumentException, ricorre a innerText/textContent via JS.
    """
    try:
        return (element.text or "").strip()
    except InvalidArgumentException:
        try:
            driver = element.parent
            return (driver.execute_script("return arguments[0].innerText || arguments[0].textContent || '';", element) or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""


def force_set_caption_js(driver, element, text: str) -> str:
    """
    Ultima spiaggia: imposta la didascalia via JS su un contenteditable e genera evento input.
    Usa innerText per la massima compatibilità, sacrificando la struttura di Lexical.
    Ritorna il testo letto dopo la scrittura.
    """
    try:
        # Passa il testo direttamente. Selenium/WebDriver si occupa dell'escaping.
        # L'uso di `innerText` è più robusto che manipolare il DOM (span/br).
        return driver.execute_script(
            """
            const el = arguments[0];
            const val = arguments[1];
            if (!el) return '';
            
            el.focus();
            
            // Metodo 1: execCommand (più compatibile per la pulizia)
            try {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
            } catch(e) {
                console.warn('execCommand select/delete failed, fallback to innerText clear', e);
                el.innerText = '';
            }

            // Simula gli eventi per notificare il cambiamento
            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));

            // Metodo 2: insertText (più moderno e preferito per l'inserimento)
            try {
                 document.execCommand('insertText', false, val);
            } catch(e) {
                console.warn('execCommand insertText failed, fallback to innerText set', e);
                el.innerText = val;
            }
            
            // Simula gli eventi di input per notificare a React/Lexical il cambiamento.
            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

            return el.innerText || el.textContent || '';
            """,
            element,
            text,
        ) or ""
    except Exception:
        raise


def human_pause(min_s=0.25, max_s=0.8):
    try:
        time.sleep(random.uniform(min_s, max_s))
    except Exception:
        time.sleep(min_s)


def human_click(driver, element, jitter_px=0):
    try:
        element.click()
    except Exception:
        try:
            ActionChains(driver).move_to_element(element).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
    human_pause()


def human_mouse_click(driver, element, jitter_attempts=2):
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
        try:
            ActionChains(driver).move_to_element(element).pause(random.uniform(0.05,0.1)).click().perform()
        except Exception:
            element.click()
        human_pause(0.18, 0.35)
    except Exception:
        human_click(driver, element)


def robust_click_and_close(driver, button, dialog_xpath="//div[@role='dialog']", attempts=3):
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
            human_pause(0.3, 0.6)
            try:
                WebDriverWait(driver, 6).until(EC.invisibility_of_element_located((By.XPATH, dialog_xpath)))
                return True
            except Exception:
                pass
        except Exception:
            pass
    return False


def scroll_into_view(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
    human_pause(0.2, 0.4)


def ensure_profile_ready():
    profile_path = Path(FIREFOX_PROFILE_PATH)
    if not profile_path.exists():
        print(f"[ERRORE] Profilo Firefox non trovato: {FIREFOX_PROFILE_PATH}")
        sys.exit(1)
    for lf in ["lock", ".parentlock"]:
        if (profile_path / lf).exists():
            print(f"[ERRORE] Profilo in uso. Chiudi Firefox e riprova. Lock: {profile_path / lf}")
            sys.exit(1)


def avvia_driver():
    ensure_profile_ready()
    firefox_options = Options()
    firefox_options.profile = FIREFOX_PROFILE_PATH
    driver = webdriver.Firefox(options=firefox_options)
    driver.set_window_size(*WINDOW_SIZE)
    return driver


def attendi_pagina(driver, timeout=35):
    targets = [
        (By.XPATH, "//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]") ,
        (By.XPATH, "//article"),
        (By.XPATH, "//div[@role='dialog']"),
        (By.XPATH, "//button[contains(., 'Accetta') or contains(., 'Consenti') or contains(., 'Accept') or contains(., 'Allow')]")
    ]
    for locator in targets:
        try:
            return WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator))
        except TimeoutException:
            continue
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


def clicca_opzioni(driver, timeout=20):
    print("[DEBUG] Attendo che l'icona 'Altre opzioni/More options' compaia...")
    svg_variants = (
        "//article//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]",
        "//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]",
    )
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
    if not found_svg:
        print("[ERRORE] Icona SVG 'Altre opzioni/More options' non trovata")
        raise TimeoutException("Icona opzioni non presente")

    print("[DEBUG] Cerco il bottone che contiene l'icona...")
    button_variants = (
        "//article//button[.//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
        "//div[@role='button' and .//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
        "//button[.//*[name()='svg' and (@aria-label='Altre opzioni' or @aria-label='More options')]]",
    )
    btn = None
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
        print("[ERRORE] Pulsante 'Altre opzioni' non cliccabile con i selettori noti")
        raise TimeoutException("Bottone opzioni non cliccabile")

    scroll_into_view(driver, btn)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)
    print("[INFO] Menu opzioni cliccato.")


def dump_debug(driver, prefix):
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


def clicca_modifica(driver, timeout=15):
    candidates = (
        "//div[@role='dialog']//button[normalize-space()='Modifica' or normalize-space()='Edit']",
        "//button[normalize-space()='Modifica' or normalize-space()='Edit']",
        "//div[@role='dialog']//*[self::div or self::span][normalize-space()='Modifica' or normalize-space()='Edit']/ancestor::button[1]",
        "//*[self::div or self::span][normalize-space()='Modifica' or normalize-space()='Edit']/ancestor::*[@role='menuitem' or self::button][1]",
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
        raise TimeoutException("Voce 'Modifica' non trovata nel menu")
    human_click(driver, voce)


def aggiorna_testo(driver, nuovo_testo, timeout=15):
    dialog_xpath = "//div[@role='dialog']"
    try:
        dlg = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, dialog_xpath)))
    except Exception:
        raise TimeoutException("Dialog di modifica non trovato")

    log_debug("[DEBUG] Cerco campo didascalia...")
    field_selectors = (
        ".//*[@aria-label='Scrivi una didascalia' or @aria-label='Write a caption…' or @aria-label='Write a caption...']",
        ".//*[@data-testid='post-caption-textarea']",
        ".//*[@data-lexical-editor='true']//div[@contenteditable='true']",
        ".//div[@data-lexical-editor='true']//div[@contenteditable='true']",
        ".//*[@data-lexical-editor='true' and @role='textbox']",
        ".//*[@contenteditable='true' and @role='textbox']",
        ".//*[@contenteditable='true']",
        ".//textarea",
    )

    def trova_area():
        # Prova con attesa esplicita di clickabilità sul driver (non solo dlg)
        for sel in field_selectors:
            try:
                el = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, sel)))
                if el:
                    return el
            except Exception:
                pass
        # Fallback: presenza (non necessariamente cliccabile)
        for sel in field_selectors:
            try:
                el = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, sel)))
                if el:
                    return el
            except Exception:
                pass
        # Fallback: search manuale tra contenteditable
        try:
            els = driver.find_elements(By.XPATH, "//div[@role='dialog']//*[@contenteditable='true']")
            for el in els:
                if el.is_displayed():
                    return el
        except Exception:
            pass
        # Fallback JS finale
        try:
            el = driver.execute_script("""
                const dlg = document.querySelector('[role="dialog"]');
                if (!dlg) return null;
                const targets = dlg.querySelectorAll('[aria-label*="caption"], [data-lexical-editor="true"], [contenteditable="true"], textarea');
                return targets[0] || null;
            """)
            return el
        except Exception:
            return None

    area = trova_area()
    if not area:
        raise TimeoutException("Campo didascalia non trovato")

    log_debug("[DEBUG] Campo didascalia trovato, avvio focus e pulizia...")
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
    try:
        area.send_keys(Keys.CONTROL, 'a')
        human_pause(0.05, 0.15)
        area.send_keys(Keys.BACK_SPACE)
        human_pause(0.2, 0.35)
    except Exception as exc:
        log_debug(f"[ERRORE] Clear iniziale fallito: {exc}")
        log_debug(traceback.format_exc())
        raise

    # La digitazione via send_keys è inaffidabile. Uso un metodo JS più robusto con re-tentativi.
    log_debug("[INFO] Uso il metodo JS diretto per impostare il testo, con re-tentativi.")
    
    success = False
    last_known_text = ""
    for attempt in range(1, 4):
        log_debug(f"[DEBUG] Tentativo {attempt} di impostare il testo.")
        try:
            js_text = force_set_caption_js(driver, area, nuovo_testo)
            human_pause(0.5, 0.8)
            
            current = normalize_caption_text(js_text or safe_element_text(area))
            last_known_text = current
            target = normalize_caption_text(nuovo_testo)
            
            if target in current:
                log_debug(f"[DEBUG] Testo confermato al tentativo {attempt}.")
                success = True
                break
            
            # Controllo più lasco senza spazi
            if target.replace(" ", "") in current.replace(" ", ""):
                log_debug(f"[WARN] Il testo corrisponde solo dopo aver rimosso gli spazi (tentativo {attempt}). Procedo.")
                success = True
                break

            log_debug(f"[WARN] Tentativo {attempt} fallito. Testo attuale: '{current[:100]}...'")
            human_pause(0.5, 1.0) # Pausa aggiuntiva prima del re-tentativo

        except Exception as exc_js_force:
            log_debug(f"[ERRORE] Tentativo {attempt} ha generato un'eccezione: {exc_js_force}")
            log_debug(traceback.format_exc())
            human_pause(0.5, 1.0)

    if not success:
        log_debug(f"[ERRORE] Tutti i tentativi di impostare il testo sono falliti. Ultimo testo letto: '{last_known_text[:200]}...'")
        raise TimeoutException("Il testo non è stato impostato correttamente dopo 3 tentativi.")



    print("[INFO] Didascalia impostata.")

    for _ in range(15):
        try:
            btn_probe = driver.find_element(By.XPATH, "//div[@role='dialog']//div[@role='button'][normalize-space()='Fine' or normalize-space()='Done' or normalize-space()='Fatto']")
            if btn_probe.is_enabled() and not btn_probe.get_attribute('aria-disabled'):
                break
        except Exception:
            pass
        time.sleep(0.12)


def estrai_caption_da_sorgente(page_source):
    return ""


def estrai_caption_da_json(page_source):
    try:
        match = re.search(r'"caption":\{"text":"(.*?)"\}', page_source)
        if match:
            testo = match.group(1)
            testo = testo.encode('utf-8').decode('unicode_escape')
            return testo
    except Exception:
        pass
    return ""


def salva(driver, timeout=15, manual_mode=False):
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

    time.sleep(0.3)

    scroll_into_view(driver, btn)
    human_pause(0.2, 0.35)

    if manual_mode:
        print("[MODALITÀ MANUALE] Clicca 'Fine' nel browser, poi premi INVIO qui.")
        input(">>> Premi INVIO dopo aver cliccato manualmente 'Fine': ")
        human_pause(2.0, 3.0)
        return True

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
    candidates = (
        "//article//h1",
        "//article//h1//span",
        "//article//div[contains(@class, 'C4VMK')]//span",
        "//article//span[contains(text(), ' ')]",
        "//article//span[@dir='auto']",
        "//article//span",
    )
    texts_found: List[str] = []
    for xp in candidates:
        try:
            elements = driver.find_elements(By.XPATH, xp)
            for el in elements:
                text = el.text.strip()
                if text and len(text) > 3:
                    texts_found.append(text)
                    print(f"[DEBUG] Testo trovato con {xp}: '{text[:50]}...'")
        except Exception:
            continue
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
    target = nuovo_testo.strip()
    for i in range(1, tentativi + 1):
        human_pause(0.6, 1.0)
        caption = leggi_didascalia_corrente(driver, timeout=timeout_lettura).strip()
        if target and target in caption:
            print(f"[INFO] Didascalia confermata al tentativo {i}.")
            return caption
        print(f"[DEBUG] Tentativo {i}: didascalia non ancora aggiornata.")
        if i < tentativi:
            print("[DEBUG] Effettuo un refresh della pagina per forzare l'aggiornamento...")
            try:
                driver.refresh()
            except Exception:
                pass
    raise TimeoutException("La didascalia non risulta aggiornata dopo i tentativi previsti")


def edit_main(permalink: str | None = None, nuovo_testo: str | None = None, manual_mode: bool | None = None, return_success: bool = False):
    global MANUAL_SAVE_MODE
    permalink = permalink or PERMALINK
    nuovo_testo = nuovo_testo or NUOVO_TESTO
    if manual_mode is not None:
        MANUAL_SAVE_MODE = manual_mode

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
        driver.get(permalink)
        human_pause(0.6, 1.2)
        try:
            attendi_pagina(driver)
        except TimeoutException:
            driver.refresh()
            human_pause(0.6, 1.0)
            attendi_pagina(driver)
        timer_end("LOAD_PERMALINK")

        print("[INFO] Clicco su opzioni...")
        timer_start("OPEN_OPTIONS")
        clicca_opzioni(driver)
        timer_end("OPEN_OPTIONS")

        print("[INFO] Seleziono 'Modifica'...")
        timer_start("OPEN_EDIT_DIALOG")
        clicca_modifica(driver)
        timer_end("OPEN_EDIT_DIALOG")

        print("[INFO] Aggiorno il testo...")
        timer_start("EDIT_CAPTION")
        try:
            aggiorna_testo(driver, nuovo_testo)
        except Exception as exc:
            capture_editor_state(driver, "EDIT_CAPTION_ERROR")
            log_debug(f"[ERRORE] aggiorna_testo: {exc}")
            raise
        finally:
            timer_end("EDIT_CAPTION")

        log_debug("[INFO] Salvo le modifiche...")
        timer_start("SAVE_OPERATION")
        salva(driver, manual_mode=MANUAL_SAVE_MODE)
        timer_end("SAVE_OPERATION")

        ps = driver.page_source
        timer_start("VERIFY")
        json_caption = estrai_caption_da_json(ps).strip()
        target = nuovo_testo.strip()
        if target and normalize_caption_text(target) in normalize_caption_text(json_caption):
            print("[INFO] Salvataggio confermato (JSON, senza reload).")
        else:
            print("[DEBUG] JSON non conferma, eseguo reload rapido...")
            print("[DEBUG] Ricarico il permalink (reload mirato)...")
            timer_start("POST_RELOAD")
            try:
                driver.get(permalink)
                human_pause(0.5, 0.9)
                attendi_pagina(driver)
            except Exception as exc:
                print(f"[WARN] Reload permalink non completamente riuscito: {exc}")
            timer_end("POST_RELOAD")
            ps = driver.page_source

        json_caption = estrai_caption_da_json(ps).strip()
        dom_caption = leggi_didascalia_corrente(driver).strip()
        target = nuovo_testo.strip()
        target_norm = normalize_caption_text(target)
        json_norm = normalize_caption_text(json_caption)
        dom_norm = normalize_caption_text(dom_caption)
        if target_norm and (target_norm in json_norm or target_norm in dom_norm):
            print("[INFO] Salvataggio confermato (caption presente).")
        else:
            print("[WARN] Caption non confermata, tentativo di riapertura editor per controllo.")
            try:
                clicca_opzioni(driver)
                clicca_modifica(driver)
                editor_text = ""
                try:
                    editor_field = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//*[@contenteditable='true']")))
                    editor_text = (editor_field.text or '').strip()
                except Exception:
                    pass
                if target in editor_text:
                    print("[INFO] Salvataggio confermato dall'editor riaperto.")
                else:
                    raise TimeoutException("Salvataggio non verificato nei canali JSON/DOM/editor")
            except Exception as ve:
                raise TimeoutException(f"Verifica fallita: {ve}")
        timer_end("VERIFY")

        print("[✅] Modifica completata.")
        timer_end("TOTAL")
        log_debug("\n===== RIEPILOGO TEMPI =====")
        for k, v in _TIMERS.items():
            if isinstance(v, (int, float)):
                log_debug(f"{k}: {v:.3f}s")
        log_debug("==========================\n")
        success = True

    except TimeoutException as exc:
        print(f"[ERRORE] Elemento non trovato nei tempi attesi: {exc}")
        try:
            dump_debug(driver, "debug_failure_timeout")
        except Exception:
            pass
    except Exception as exc:
        print(f"[ERRORE] Errore inatteso: {exc}")
        log_debug(traceback.format_exc())
        try:
            dump_debug(driver, "debug_failure_generic")
        except Exception:
            pass
    finally:
        driver.quit()

    if return_success:
        return success
    if not success:
        sys.exit(1)
    return True


def run_edit(permalink: str, nuovo_testo: str, manual_mode: bool | None = None) -> bool:
    """
    Wrapper per aggiornare una singola caption.
    """
    return edit_main(permalink=permalink, nuovo_testo=nuovo_testo, manual_mode=manual_mode, return_success=True)


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Scrive un JSON in modo atomico: prima tmp, poi rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


# Cartelle di gestione
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
PUBBLICATI_SUBDIR = os.getenv("PUBBLICATI_SUBDIR", "pubblicati")
FAILED_SUBDIR = os.getenv("FAILED_SUBDIR", "non_riusciti")
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
CHECKPOINT_SUBDIR = "pubblicazione"

# Modalità dry-run di default (sovrascrivibile da CLI)
DEFAULT_DRY_RUN = env_bool("DEFAULT_DRY_RUN", False)


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
    Carica un checkpoint esistente e ritorna un set di chiavi già pubblicate.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    if not checkpoint_path.exists():
        return set()

    try:
        data = json.loads(checkpoint_path.read_text())
        published_ids = data.get("published_keys", [])
        print(f"[INFO] Trovato checkpoint: {len(published_ids)} post già pubblicati.")
        return set(published_ids)
    except (json.JSONDecodeError, KeyError):
        print("[WARN] Checkpoint corrotto, verrà ignorato e sovrascritto.")
        return set()


def save_checkpoint(input_path: str, published_ids: Set[str]) -> None:
    """
    Salva il set di ID/permalink dei post pubblicati nel file di checkpoint.
    """
    checkpoint_path = get_checkpoint_path(input_path)
    checkpoint_data = {
        "last_updated": dt.datetime.now().isoformat(),
        "published_keys": list(published_ids),
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
    Supporta diversi formati di file: posts_modificati.json (key 'posts'), 
    vecchi formati (key 'results'), e falliti.json (key 'items').
    """
    path = Path(input_path)
    if not path.is_file():
        base_dir = Path(__file__).parent
        alt_path = base_dir / path
        if alt_path.is_file():
            path = alt_path
        else:
            print(f"[ERRORE] File non trovato: {input_path}")
            print(f"[INFO] Percorsi verificati: {path.resolve()} e {alt_path.resolve()}")
            print("[INFO] Esegui prima gli script 2 e 3 per modificare i post.")
            sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
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
        results = data.get("items", []) # Per i file dei falliti
    elif "results" in data:
        results = data.get("results", []) # Fallback per vecchi formati
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
    return (
        str(post.get("post_id"))
        or str(post.get("id"))
        or str(post.get("permalink"))
        or str(post.get("post_number"))
    )


def publish_posts(posts: List[Dict[str, Any]], input_path: str, dry_run: bool = False) -> Dict[str, Any]:
    """
    Pubblica le modifiche su Instagram via Selenium.
    Se dry_run è True, mostra solo cosa verrebbe fatto senza aggiornare.
    Ritorna un dizionario con il report dettagliato.
    """
    if not posts:
        print("[WARN] La lista di post da pubblicare è vuota. Nessuna azione da eseguire.")
        return {
            "summary": {
                "total_posts": 0,
                "message": "Nessun post fornito.",
                "dry_run": dry_run,
            },
            "details": [],
            "failed_items": [],
        }
        
    updated_count = 0
    failed_count = 0
    skipped_count = 0
    already_published_count = 0
    report_details = []
    failed_items: List[Dict[str, Any]] = []
    consecutive_failures = 0  # Contatore per stop anticipato

    # Carica ID già pubblicati dal checkpoint
    published_ids = load_checkpoint(input_path)

    for post in posts:
        post_id = post.get("post_id") or post.get("id")
        post_number = post.get("post_number", "N/A")
        status_meccanico = post.get("status_meccanico")
        status_ai = post.get("status_ai")
        legacy_status = post.get("status")
        original_message = post.get("messaggio_originale") or post.get("original_message") or post.get("message") or ""
        modified_message = post.get("messaggio_modificato") or post.get("modified_message") or post.get("message") or ""
        created_time = post.get("created_time", post.get("timestamp", "N/A"))
        permalink = post.get("permalink") or post.get("permalink_url") or post.get("url") or ""

        # Determina se pubblicare secondo le nuove regole
        if status_meccanico is not None or status_ai is not None:
            should_publish = (status_meccanico == "modificato") or (status_ai == "modificato")
            status_label = f"status_meccanico={status_meccanico}, status_ai={status_ai}"
        elif legacy_status is not None:
            should_publish = legacy_status == "modified"
            status_label = f"status={legacy_status}"
        elif post.get("messaggio_modificato"):
            # Supporto per file Hashtags/output_openai_hashtag_only.json che non ha status ma ha i messaggi
            should_publish = True
            status_label = "implicit_modified (messaggio_modificato present)"
        else:
            should_publish = False
            status_label = "unknown_status"

        if not should_publish:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Status: {status_label}")
            skipped_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
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
                "permalink": permalink,
                "action": "skipped",
                "reason": "no_changes",
                "success": None,
            })
            continue

        if not permalink:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Permalink mancante")
            failed_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
                "action": "missing_permalink",
                "reason": "no_permalink",
                "success": False,
            })
            failed_items.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
                "error": "permalink_missing",
                "original_message": original_message,
                "modified_message": modified_message,
            })
            continue

        checkpoint_key = normalize_identifier(post)
        if checkpoint_key in published_ids:
            print(f"[SKIP] Post #{post_number} - {post_id} ({created_time}) - Già pubblicato (da checkpoint)")
            already_published_count += 1
            report_details.append({
                "post_number": post_number,
                "post_id": post_id,
                "created_time": created_time,
                "permalink": permalink,
                "action": "skipped_checkpoint",
                "reason": "already_published",
                "success": True,
            })
            continue

        print(f"\n[POST #{post_number}] {post_id} - {created_time}")
        print(f"Permalink: {permalink}")
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
                "permalink": permalink,
                "action": "dry_run",
                "reason": "preview_only",
                "success": True,
            })
        else:
            ok = run_edit(permalink, modified_message, manual_mode=False)
            if ok:
                updated_count += 1
                published_ids.add(checkpoint_key)
                save_checkpoint(input_path, published_ids)  # Salva subito dopo il successo

                report_details.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "permalink": permalink,
                    "action": "updated",
                    "reason": None,
                    "success": True,
                })
                consecutive_failures = 0  # Reset contatore successi

                if updated_count == 15:
                    print("\n" + "="*50)
                    print("[PAUSA DI CONTROLLO] Raggiunti i primi 15 post aggiornati.")
                    print("Controlla su Instagram che tutto sia corretto.")
                    user_input = input(">>> Scrivi 'procedi' per continuare, o premi Invio per fermarti: ")
                    if user_input.strip().lower() != "procedi":
                        print("[STOP] Interruzione su richiesta utente.")
                        break
                    print("="*50 + "\n")

                # Rate limiting: ~15 post/ora => 1 post ogni ~4 min (240s)
                # L'utente stima ~100s di esecuzione per post.
                # Quindi delay necessario = 240s - 100s = 140s.
                delay = random.uniform(130, 150)
                print(f"[RATE-LIMIT] Attendo {delay:.1f}s prima del prossimo post (ciclo tot ~240s)...")
                time.sleep(delay)
            else:
                failed_count += 1
                print("[WARN] Aggiornamento fallito, passo al prossimo post.")
                report_details.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "permalink": permalink,
                    "action": "update_failed",
                    "reason": "selenium_error",
                    "success": False,
                })
                failed_items.append({
                    "post_number": post_number,
                    "post_id": post_id,
                    "created_time": created_time,
                    "permalink": permalink,
                    "error": "selenium_error",
                    "original_message": original_message,
                    "modified_message": modified_message,
                })
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("\n[STOP] Raggiunti 3 errori consecutivi. Interrompo l'esecuzione per sicurezza.")
                    break
            # Attendi un attimo prima di procedere al prossimo update per evitare burst
            time.sleep(1)

    print("\n========== RIEPILOGO ==========")
    print(f"Post totali nel file: {len(posts)}")
    print(f"Post aggiornati in questa sessione: {updated_count}")
    print(f"Post falliti in questa sessione: {failed_count}")
    print(f"Post saltati (non modificati): {skipped_count}")
    print(f"Post già pubblicati (da checkpoint): {already_published_count}")
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
            "dry_run": dry_run,
        },
        "action_breakdown": action_counts,
        "details": report_details,
        "failed_items": failed_items,
    }


def save_publication_report(report: Dict[str, Any], output_dir: str = "output/pubblicati") -> None:
    """
    Salva il report di pubblicazione nella cartella pubblicati.
    """
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


def save_failed_updates(items: List[Dict[str, Any]], output_dir: str = "output/non_riusciti", overwrite: bool = False) -> None:
    """
    Aggiorna 'falliti.json'.
    - Se overwrite=False (default), aggiunge i nuovi fallimenti a quelli esistenti.
    - Se overwrite=True, rimpiazza il file con i dati forniti.
    - Se la lista finale di items da salvare è vuota, il file viene cancellato.
    """
    base_dir = Path(__file__).parent
    fail_dir = base_dir / output_dir
    fail_dir.mkdir(parents=True, exist_ok=True)
    output_path = fail_dir / "falliti.json"

    final_items_map = {}

    # Se non dobbiamo sovrascrivere, carichiamo prima i fallimenti esistenti.
    if not overwrite and output_path.exists():
        try:
            data = json.loads(output_path.read_text(encoding='utf-8'))
            for post in data.get("items", []):
                post_id = normalize_identifier(post)
                if post_id:
                    final_items_map[post_id] = post
        except (json.JSONDecodeError, KeyError):
            print(f"[WARN] File dei falliti '{output_path.name}' corrotto o malformato, sarà sovrascritto.")

    # Aggiungi/sovrascrivi gli items passati alla funzione
    for post in items:
        post_id = normalize_identifier(post)
        if post_id:
            final_items_map[post_id] = post

    final_failed_list = list(final_items_map.values())

    # Se la lista finale è vuota, puliamo e usciamo.
    if not final_failed_list:
        if output_path.exists():
            output_path.unlink()
            print(f"[INFO] Coda dei falliti è vuota. '{output_path.name}' eliminato.")
        return
    
    payload = {
        "failed_count": len(final_failed_list),
        "last_updated": dt.datetime.now().isoformat(),
        "items": final_failed_list,
    }

    write_json_atomic(output_path, payload)
    print(f"[INFO] File dei falliti aggiornato: {output_path.resolve()} ({len(final_failed_list)} post in coda)")


# ==========================
# ENTRYPOINT
# ==========================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pubblica le modifiche dei post su Instagram tramite Selenium (logica integrata)."
    )
    parser.add_argument(
        "--input",
        help="File JSON con i post da pubblicare (default: Testi/output_openai_text_edited.json)",
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
    parser.set_defaults(dry_run=DEFAULT_DRY_RUN)

    args = parser.parse_args()

    # Forza il percorso da 'Testi' se non specificato
    input_file = args.input if args.input else "Testi/output_openai_text_edited.json"

    print("===== PUBBLICAZIONE MODIFICHE SU INSTAGRAM =====")
    print(f"Input file: {input_file}")
    print(f"Dry-run: {'SI' if args.dry_run else 'NO'}")
    print("===============================================")

    posts = load_modified_posts(input_file)

    report = publish_posts(posts, input_file, args.dry_run)

    # Salva report nella cartella pubblicati
    save_publication_report(report, f"{OUTPUT_DIR}/{PUBBLICATI_SUBDIR}")

    summary = report.get("summary", {})
    # Salva i post falliti in una cartella dedicata (in modo cumulativo)
    if not args.dry_run and summary.get("failed_this_run", 0) > 0:
        save_failed_updates(report.get("failed_items", []))

    # Se non ci sono stati errori e non siamo in dry-run, puliamo il checkpoint
    if not args.dry_run and summary.get("failed_this_run", 0) == 0 and summary.get("updated_this_run", 0) > 0:
        delete_checkpoint(input_file)
    elif not args.dry_run and summary.get("failed_this_run", 0) > 0:
        print("[INFO] Il checkpoint non è stato eliminato a causa di errori di pubblicazione.")
    
    if summary.get("updated_this_run", 0) == 0 and summary.get("failed_this_run", 0) == 0:
        print("\n[INFO] Nessun post è stato aggiornato in questa esecuzione.")

    print("\n[COMPLETATO] Pubblicazione completata!")


if __name__ == "__main__":
    main()
