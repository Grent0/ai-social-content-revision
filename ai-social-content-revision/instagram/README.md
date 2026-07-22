# MODIFICA-POST-IG

Replica del flusso di MODIFICA-POST-FB per Instagram: scarica i post, applica sostituzioni meccaniche e (opzionale) riscrittura AI, poi aggiorna le caption via Graph API. Le credenziali e le opzioni principali stanno in `.env` nella cartella.

## Requisiti
- Python 3.x
- `pip install requests`
- Per il flusso Vision (OCR media): `pip install google-cloud-vision opencv-python`
- Access Token IG Graph con permessi per leggere i media (la pubblicazione usa Selenium con il profilo configurato in `edit_instagram_post.py`).

## Configurazione `.env`
Imposta almeno:
```
IG_USER_ID=...          # id account IG business/creator
ACCESS_TOKEN=...        # token IG Graph
IG_API_BASE=https://graph.facebook.com/v18.0
```
Opzioni AI (usa AI_API_KEY oppure OPENAI_API_KEY):
```
AI_API_KEY=...          # o OPENAI_API_KEY
AI_MODEL=gpt-4.1-mini   # o OPENAI_MODEL
AI_EXTRA_CONTEXT="Contesto opzionale per la riscrittura"
```
Sostituzioni e trigger AI (JSON):
```
REPLACEMENTS={"catalogo":"brochure","link in bio":"link nel primo commento"}
AI_KEYWORDS=["promo","offerta","sconto","nuova collezione"]
```
Compatibilità legacy:
```
MEC_REPLACE=("vecchio:nuovo","termine:altra_frase")   # se REPLACEMENTS non è impostato
AI_FILTER_TEXT=("keyword1","keyword2")                # se AI_KEYWORDS non è impostato
```
Altre variabili:
```
DEFAULT_YEAR_START=2025   # per default --since
DEFAULT_YEAR_END=2025     # per default --until
OUTPUT_DIR=output
SCARICATI_SUBDIR=scaricati
MODIFICATI_SUBDIR=modificati
CHECKPOINT_DIR=checkpoints
PUBBLICATI_SUBDIR=pubblicati
FAILED_SUBDIR=non_riusciti
START_FROM_POST=1
CHECKPOINT_INTERVAL=5
ENABLE_AI=true
DEFAULT_DRY_RUN=false
```

## Script disponibili (flusso a 4 step)
- `1_scarica_post.py`  
  Scarica i media IG nell'intervallo di date e salva il JSON (output/scaricati di default).
  ```
  python 1_scarica_post.py --since 2025-09-01 --until 2025-09-30 --output posts_scaricati.json
  ```

- `2_modifica_testo_meccanica.py`  
  Applica solo le sostituzioni meccaniche e produce l'intermedio `posts_con_stato_meccanico.json`.
  ```
  python 2_modifica_testo_meccanica.py --input output/scaricati/posts_scaricati.json
  ```

- `3_modifica_testo_ai.py`  
  Parte dall'intermedio meccanico e applica la riscrittura AI (se attiva). Gestisce checkpoint e file dei fallimenti AI.
  ```
  python 3_modifica_testo_ai.py --input output/modificati/posts_con_stato_meccanico.json --output posts_modificati.json
  python 3_modifica_testo_ai.py --no-ai           # usa solo il testo meccanico
  ```

- `4_pubblica_modifiche.py`  
  Aggiorna le caption aprendo i permalink con Selenium (`run_edit` di `edit_instagram_post.py`). Dry-run consigliato prima.
  ```
  python 4_pubblica_modifiche.py --input posts_modificati.json --dry-run
  python 4_pubblica_modifiche.py --input posts_modificati.json                # pubblica davvero
  ```

## OCR immagini/video (opzionale)
Flusso in `elab-imgevid` per cercare una frase dentro immagini e video IG (supporta anche caroselli). Richiede `GOOGLE_APPLICATION_CREDENTIALS` nel `.env` (JSON o base64) per Google Cloud Vision e usa lo stesso `PAGE_ACCESS_TOKEN/ACCESS_TOKEN` per scaricare i media.

```bash
# Download media (usa output di 1_scarica_post.py)
python elab-imgevid/1_download_images.py --posts-file output/scaricati/posts_scaricati.json --dest-dir elab-imgevid/contenuti/immagini
python elab-imgevid/2_download_videos.py --posts-file output/scaricati/posts_scaricati.json --dest-dir elab-imgevid/contenuti/video

# OCR + riepilogo
python elab-imgevid/3_analyze_images.py --phrase "frase da trovare" --output output/elab_imgevid/vision_images.json
python elab-imgevid/4_analyze_videos.py --phrase "frase da trovare" --output output/elab_imgevid/vision_videos.json
python elab-imgevid/5_combine_vision_reports.py --output output/elab_imgevid/vision_summary.json
```

## Note
- I file JSON sono allineati alla struttura FB: `output/scaricati` -> `output/modificati` -> report in `output/pubblicati`.
- Le sostituzioni meccaniche sono case-sensitive come in MODIFICA-POST-FB; se servono sostituzioni diverse usa `REPLACEMENTS`.
- Il checkpoint viene salvato ogni `CHECKPOINT_INTERVAL` post; per riprendere basta rilanciare lo script 2.
- Usa sempre `--dry-run` nello script 4 prima di aggiornare davvero le caption.
