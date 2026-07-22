# Elaborazione immagini e video (Google Cloud Vision)

Cinque script numerati per scaricare i media e cercare una frase dentro immagini e video con OCR:

- `1_download_images.py`: scarica le immagini dai post IG (media_url/carousel) o dagli attachment FB.
- `2_download_videos.py`: scarica i video dai post IG (media_url/carousel) o dagli attachment FB.
- `3_analyze_images.py`: OCR sulle immagini scaricate.
- `4_analyze_videos.py`: estrae frame dai video a intervalli regolari e fa OCR su ciascun frame.
- `5_combine_vision_reports.py`: combina i report in un riepilogo.

## Requisiti

- Python 3.8+
- Pacchetti: `pip install google-cloud-vision opencv-python`
- Token Graph: `PAGE_ACCESS_TOKEN` (o `ACCESS_TOKEN`) nel `.env`, opzionale `IG_API_BASE`/`FB_API_BASE` per cambiare endpoint.
- Credenziali GCP: incolla il JSON (o il base64 del JSON) della service account in `GOOGLE_APPLICATION_CREDENTIALS`; gli script scrivono un file temporaneo e lo impostano per Google Cloud Vision.

## Struttura attesa dei file

- File post: `output/scaricati/posts_scaricati.json`
- Cartelle media di default (creati dagli script di download):
  - Immagini: `elab-imgevid/contenuti/immagini/<post_id>/`
  - Video: `elab-imgevid/contenuti/video/<post_id>/`
- Se non trova la sottocartella, il fallback cerca ricorsivamente file che contengono `post_id` o `post_number` nel nome.

## Uso rapido

```bash
# 1) Scarica immagini
python elab-imgevid/1_download_images.py \
  --posts-file output/scaricati/posts_scaricati.json \
  --dest-dir elab-imgevid/contenuti/immagini

# 2) Scarica video
python elab-imgevid/2_download_videos.py \
  --posts-file output/scaricati/posts_scaricati.json \
  --dest-dir elab-imgevid/contenuti/video

# 3) Immagini: OCR
python elab-imgevid/3_analyze_images.py \
  --phrase "frase da trovare" \
  --images-dir elab-imgevid/contenuti/immagini \
  --posts-file output/scaricati/posts_scaricati.json \
  --output output/elab_imgevid/vision_images.json

# 4) Video: OCR (frame ogni 2s, max 40 frame per video di default)
python elab-imgevid/4_analyze_videos.py \
  --phrase "frase da trovare" \
  --videos-dir elab-imgevid/contenuti/video \
  --posts-file output/scaricati/posts_scaricati.json \
  --frame-every 2 \
  --output output/elab_imgevid/vision_videos.json
```

Opzioni utili:

- `--case-sensitive` per rendere la ricerca sensibile al maiuscolo/minuscolo.
- `--language-hints it en` per passare hint linguistici a Vision.
- `--keep-text-non-match` per salvare nel report anche il testo OCR dei file senza match.
- Video: `--resize-width` per downscale dei frame, `--max-frames-per-video` e `--frame-every` per controllare i costi API.

## Flusso in 5 passi

1) Scarica immagini (`1_download_images.py`)  
2) Scarica video (`2_download_videos.py`)  
3) OCR immagini (`3_analyze_images.py`)  
4) OCR video (`4_analyze_videos.py`)  
5) Riepilogo combinato (`5_combine_vision_reports.py`)  

Opzioni utili (passale ai singoli script): `--case-sensitive`, `--language-hints it en`, `--keep-text-non-match`; per i video `--resize-width`, `--max-frames-per-video`, `--frame-every`; per il download puoi cambiare `--dest-dir`.

## Output

Entrambi gli script generano un JSON con:

- `summary`: conteggi dei file analizzati e dei match.
- `results`: dettaglio per immagine/frame con flag `match` e (se richiesto) testo OCR ritagliato.

Nota: l'OCR e l'estrazione di frame possono generare costi su GCP; regola i parametri di sampling per limitare le chiamate.
