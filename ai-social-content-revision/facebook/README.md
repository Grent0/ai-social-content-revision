# Bot Social - Modifica Post Facebook

Suite di script Python per scaricare, modificare (prima meccanicamente, poi con AI) e ripubblicare i post di una pagina Facebook. Ogni fase usa checkpoint e scrittura atomica per riprendere senza corruzione dei file.

## Requisiti rapidi

- Python 3.8+
- Ambiente virtuale (consigliato)
- Token della pagina Facebook (`PAGE_ACCESS_TOKEN`) e `PAGE_ID`
- (Opzionale) Chiave API AI (`AI_API_KEY`) per la riscrittura

## Setup veloce (prompt-ready)

```bash
git clone <repo>
cd <repo>
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # richiede almeno 'requests'

cat > .env <<'EOF'
PAGE_ID="id_della_pagina_fb"
PAGE_ACCESS_TOKEN="token_della_pagina"
AI_API_KEY="chiave_api_ai_opzionale"
AI_MODEL="gpt-4.1-mini"
DEFAULT_DRY_RUN="true"
REPLACEMENTS='{"vecchia": "nuova", "link in bio": "link nel primo commento"}'
AI_KEYWORDS='["promo", "offerta", "sconto"]'
START_FROM_POST="1"
CHECKPOINT_INTERVAL="5"
OUTPUT_DIR="output"
MODIFICATI_SUBDIR="modificati"
PRONTI_SUBDIR="pronti_per_pubblicazione"
CHECKPOINT_DIR="checkpoints"
PUBBLICATI_SUBDIR="pubblicati"
FAILED_SUBDIR="non_riusciti"
# Vision (OCR): incolla qui la chiave JSON del service account (anche base64)
GOOGLE_APPLICATION_CREDENTIALS='{"type":"service_account","project_id":"<project_id>","private_key_id":"<private_key_id>","private_key":"-----BEGIN PRIVATE KEY-----\\n<key>\\n-----END PRIVATE KEY-----\\n","client_email":"<service_account>@<project_id>.iam.gserviceaccount.com","client_id":"<client_id>","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs","client_x509_cert_url":"https://www.googleapis.com/robot/v1/metadata/x509/<service_account>@<project_id>.iam.gserviceaccount.com"}'
EOF
```

## Workflow (4 passi + opzionali)

1) **Scarica**  
   ```bash
   python 1_scarica_post.py --since YYYY-MM-DD --until YYYY-MM-DD
   ```  
   Output: `output/scaricati/posts_scaricati.json`

2) **Modifica meccanica**  
   ```bash
   python 2_modifica_testo_meccanica.py \
     --input output/scaricati/posts_scaricati.json \
     --output posts_con_stato_meccanico.json \
     --start-from 1 \
     --checkpoint-interval 5
   ```  
   Output intermedio: `output/modificati/posts_con_stato_meccanico.json` con `status_meccanico` (`modificato`/`invariato`).

2b) **Estrai post con coppie trigger (opzionale)**  
   Utile se vuoi far passare all’AI solo i post che contengono determinate coppie di parole (es. `topicA` + `topicB`).  
   ```bash
   python 2b_estrai_post_con_coppie.py \
     --input output/modificati/posts_con_stato_meccanico.json \
     --output output/modificati/posts_con_coppie.json
   ```  
   Output: `output/modificati/posts_con_coppie.json` (solo i post con almeno una coppia in `AI_WORD_PAIRS`).

3) **Modifica AI**  
   ```bash
   python 3_modifica_testo_ai.py \
     --input output/modificati/posts_con_stato_meccanico.json \
     --start-from 1 \
     --checkpoint-interval 5 \
     [--no-ai]  # se vuoi solo riportare il testo meccanico
   ```  
   Se usi lo step 2b, imposta invece `--input output/modificati/posts_con_coppie.json`.  
   Output finale: `output/pronti_per_pubblicazione/pronti.json` (solo i post con `status_meccanico`/`status_ai == "modificato"`).

4) **Pubblica**  
   ```bash
   python 4_pubblica_modifiche.py --dry-run                               # anteprima
   python 4_pubblica_modifiche.py --input output/pronti_per_pubblicazione/pronti.json  # esplicito
   python 4_pubblica_modifiche.py --no-dry-run                           # pubblicazione reale
   ```  
   Pubblica un post solo se `status_meccanico == "modificato"` oppure `status_ai == "modificato"` (il file `pronti.json` è già filtrato).  
   Report: `output/pubblicati/pubblicazione_<timestamp>.json`.  
   Falliti: `output/non_riusciti/falliti_<timestamp>.json`.

## Variabili d’ambiente chiave

- Obbligatorie: `PAGE_ID`, `PAGE_ACCESS_TOKEN`
- AI: `AI_API_KEY` (se vuoi la riscrittura), `AI_MODEL`
- Sostituzioni: `REPLACEMENTS` (JSON dict), `AI_KEYWORDS` (JSON list)
- Controllo run: `START_FROM_POST`, `CHECKPOINT_INTERVAL`
- Cartelle: `OUTPUT_DIR`, `MODIFICATI_SUBDIR`, `PRONTI_SUBDIR`, `CHECKPOINT_DIR`, `PUBBLICATI_SUBDIR`, `FAILED_SUBDIR`
- Pubblicazione: `DEFAULT_DRY_RUN` (true/false)

## Opzioni CLI principali

- **1_scarica_post.py**: `--since`, `--until` (ISO date), percorsi opzionali input/output se previsti dallo script.
- **2_modifica_testo_meccanica.py**: `--input`, `--output`, `--start-from`, `--checkpoint-interval`.
- **3_modifica_testo_ai.py**: `--input`, `--output` (default: `output/pronti_per_pubblicazione/pronti.json`), `--start-from`, `--checkpoint-interval`, `--no-ai` (riporta solo testo meccanico).
- **4_pubblica_modifiche.py**: `--input` (default: `output/pronti_per_pubblicazione/pronti.json`), `--dry-run` / `--no-dry-run`.

## Checkpoint e file generati (scrittura atomica)

- **Step 2 – Meccanico**
  - Checkpoint: `checkpoints/meccanici/checkpoint_meccanico_<input>.json` (`last_processed`, `results` parziali). Se non usi `--start-from`, chiede se riprendere; se rifiuti, elimina il checkpoint.
  - Output: `output/modificati/posts_con_stato_meccanico.json` (`status_meccanico`, `original_message`, `message_meccanico`/`modified_message`).

- **Step 3 – AI**
  - Checkpoint: `checkpoints/ai/checkpoint_ai_<input>.json` (stesso schema).
  - Output: `output/pronti_per_pubblicazione/pronti.json` con `summary` (totali meccanici/AI) e `posts` solo per i post modificati (`status_meccanico`/`status_ai`, `used_ai`, `ai_error`, `ai_error_message`, `original_message`, `message_meccanico`, `modified_message`).
  - Falliti AI: `output/modificati/posts_ai_falliti_<timestamp>.json` con i soli post che hanno dato errore AI.

- **Step 4 – Pubblicazione**
  - Checkpoint: `checkpoints/pubblicazione/checkpoint_pubblicazione_<input>.json` con `published_post_ids` già aggiornati (viene cancellato se il run reale non ha errori).
  - Report: `output/pubblicati/pubblicazione_<timestamp>.json`.
  - Falliti: `output/non_riusciti/falliti_<timestamp>.json` se qualche update va in errore.

Tutti i JSON sono scritti in modo atomico (tmp + rename) per evitare file corrotti in caso di interruzioni.

## Struttura del progetto (principali)

- `1_scarica_post.py`: download post Facebook.
- `2_modifica_testo_meccanica.py`: sostituzioni semplici + `status_meccanico`.
- `2b_estrai_post_con_coppie.py`: filtra/estrae i post che contengono coppie trigger (output separato per step 3).
- `3_modifica_testo_ai.py`: riscrittura condizionale AI + `status_ai`, log errori AI.
- `4_pubblica_modifiche.py`: pubblicazione condizionata.
- `output/`: file generati.
  - `scaricati/`: input grezzo.
  - `modificati/`: intermedio meccanico, finale AI, falliti AI.
  - `pronti_per_pubblicazione/`: file già filtrati e pronti per lo step 4.
  - `pubblicati/`: report pubblicazione.
  - `non_riusciti/`: post falliti in pubblicazione.
- `checkpoints/`: stati per ripresa (meccanico, ai, pubblicazione).
- `.env`: variabili di ambiente.

## Schemi dati essenziali

- `output/scaricati/posts_scaricati.json`
  - `{ "total_posts", "downloaded_at", "media_summary": { "images", "videos", "videos_total_duration_sec" }, "posts": [ { "id", "message", "created_time", "post_number", "has_image", "has_video", "videos": [ { "id", "url", "title", "description", "duration_sec" } ]?, ... } ] }`

- `output/modificati/posts_con_stato_meccanico.json`
  - `{ "posts": [ { post_number, post_id?, original_message, message_meccanico/modified_message, status_meccanico } ], "total_posts", "modificati_meccanici" }`

- `output/pronti_per_pubblicazione/pronti.json`
  - `{ "summary": { total_posts_processed, total_modificati_meccanici, total_modificati_ai, total_pronti_per_pubblicazione }, "posts": [ { post_number, post_id, original_message, message_meccanico, modified_message, status_meccanico, status_ai, used_ai, ai_error, ai_error_message, parte_modificata? } ] }`

- `output/modificati/posts_ai_falliti_<ts>.json`
  - `{ "failed": N, "timestamp", "posts": [ { post_number, post_id, message_meccanico, ai_error_message, ... } ] }`

- `output/pubblicati/pubblicazione_<ts>.json`
  - `summary` (totali, aggiornati, falliti, skipped), `details` (per post), `action_breakdown`.

- `output/non_riusciti/falliti_<ts>.json`
  - `{ "failed": N, "items": [ { post_id, post_number, error, original_message, modified_message } ], "timestamp" }`

## Gestione errori e ripresa

- Step 2 e 3: se presenti checkpoint e `--start-from` è 1, viene chiesto se riprendere; `--start-from` diverso da 1 ignora il checkpoint. I risultati parziali sono nel checkpoint.
- Step 3: errori AI marcati per post (`ai_error`, `ai_error_message`) e file dedicato con i falliti AI.
- Step 4: se un post è pubblicato con successo, il suo ID viene salvato nel checkpoint di pubblicazione; i retry saltano quelli già pubblicati. Gli errori di update finiscono nei report `falliti_*`.
- Se una fase termina senza errori (meccanico/AI/pubblicazione reale), il relativo checkpoint viene eliminato; altrimenti resta per la ripresa.

## Note utili

- `--start-from` (step 2 e 3) forza il punto di ripresa e ignora il checkpoint se presente.
- `--checkpoint-interval` controlla ogni quanti post salvare il checkpoint (default 5).
- In pubblicazione, `--dry-run` non scrive su Facebook ma produce il report; senza `--dry-run` aggiorna davvero.
- I post non modificati da nessuna delle due fasi vengono ignorati in pubblicazione.***

## Vision: frase in immagini/video

- Nuovi script in `elab-imgevid/` per cercare una frase dentro media con Google Cloud Vision.
- Immagini: `python elab-imgevid/analyze_images.py --phrase "frase" --images-dir output/scaricati/images`.
- Video (frame ogni 2s di default): `python elab-imgevid/analyze_videos.py --phrase "frase" --videos-dir output/scaricati/videos`.
- Dipendenze: `pip install google-cloud-vision opencv-python`, credenziali GCP incollando il JSON (o il base64 del JSON) nella variabile `GOOGLE_APPLICATION_CREDENTIALS`; gli script salvano il file temporaneo automaticamente.
