# AI Social Content Revision

Pipeline in **Python** per la **revisione massiva e automatica** di contenuti social storici
(Facebook e Instagram): individua un determinato claim/associazione di parole nei post e lo rimuove
o riscrive, lasciando intatto tutto il resto — testo, emoji, hashtag, link e lingua originale.

Ho realizzato questo progetto lavorando come **AI Specialist presso Theenq S.r.l.**, per un cliente
reale nel settore della ristorazione professionale. Il sistema è stato usato in
produzione per revisionare migliaia di post effettivamente pubblicati.

Questo repository ne pubblica il codice a scopo di **portfolio**: i riferimenti al cliente sono
stati resi generici e le credenziali rimosse, ma la logica e l'implementazione sono quelle
effettivamente utilizzate.

## Idea chiave: approccio ibrido

Non tutto passa dall'AI. Il sistema combina due livelli:

- **Regole deterministiche** per i casi prevedibili (sostituzioni, rimozione hashtag) — veloci e a costo zero.
- **Large Language Model** (via API) solo dove serve *comprendere il linguaggio* (parafrasi, frasi da riscrivere mantenendo il senso).

Un filtro semantico (coppie di parole co-occorrenti) seleziona in anticipo **solo** i post da mandare
all'AI, riducendo costi e rischi.

## Controllo dell'output (la parte più importante)

Un LLM è non deterministico: può sbagliare. Per renderlo affidabile:

- **frasi protette** mascherate prima della chiamata e ripristinate dopo;
- **verifica automatica** del risultato → se un vincolo è violato, la modifica è annullata e si tiene l'originale (*fail-safe*);
- **audit log** per rendere spiegabile ogni decisione.

## Computer Vision

Modulo OCR (Google Cloud Vision) che cerca lo stesso claim **dentro immagini e video** (analisi dei
video fotogramma per fotogramma), per intercettare i casi che l'analisi del solo testo non vede.

## Struttura

```
facebook/    pipeline Facebook (Graph API)
instagram/   pipeline Instagram (Graph API + automazione browser)
  <n>_*.py         passi numerati: scarica → modifica meccanica → modifica AI → pubblica
  elab-imgevid/    modulo Computer Vision (download media, OCR, report)
  .env.example     variabili d'ambiente (copiare in .env e compilare)
  requirements.txt
```

Ogni passo legge un file JSON e ne produce un altro: fasi **disaccoppiate**, ispezionabili e
ripetibili. Robustezza: checkpoint (ripresa dopo interruzioni), scrittura atomica, esecuzioni
simulate (*dry-run*).

## Setup

```bash
cd facebook   # oppure instagram
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # poi compilare con le proprie credenziali
```

Poi si eseguono i passi in ordine (`1_*.py`, `2_*.py`, …). Dettagli nel `README.md` di ciascuna
cartella. Usare sempre `--dry-run` prima della pubblicazione reale.

## Nota

Demo a scopo dimostrativo. Le credenziali nei file `.env` / `auth.json` sono **segnaposto**: vanno
inserite le proprie. Nessun dato reale del cliente è incluso.

**Stack:** Python · OpenAI API · Meta Graph API · Google Cloud Vision · Selenium
