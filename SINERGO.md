# Sinergo

La guida completa (italiano e inglese), installazione, abbinamento a Kairos e sicurezza
in parole semplici è nel [README](README.md#italiano) e in [sinergo/README.md](sinergo/README.md).

Avvio rapido:

```bash
python installa.py --sinergo
```

Su Windows, dopo la prima installazione: `Sinergo.bat`.
Per scollegare: `.venv/bin/python -m collegato scollega`
(Windows: `.venv\Scripts\python.exe -m collegato scollega`).

## Ruoli: ascolto e voce

Kairos può assegnare a questa macchina i ruoli **ascolto** (trascrizione) e **voce**
(sintesi). Sul canale firmato arrivano tre lavori:

| Lavoro | Cosa fa |
|--------|---------|
| `prepara_ruolo` | Scarica i pesi fissati nel codice (URL + sha256), verifica l'impronta |
| `ascolto` → `ascolta` | WAV 16 kHz mono → testo |
| `voce` → `parla` | Testo → WAV |

Motori ammessi: ascolto solo **parakeet**; voce **kokoro** (fp32) o **supertonic**.
Kairos sceglie solo ruolo e motore, mai un URL. I pesi stanno nella cartella dati di
Sinergo (`~/.config/localrouter/voce/` su Linux), fuori dal codice. Dipendenze
opzionali: `uv sync --extra voce` (o `pip install '.[voce]'`).
Senza l'extra, `prepara_ruolo` risponde `stato: errore, motivo: dipendenze`.
La scheda della macchina riporta `ruoli.ascolto` / `ruoli.voce` con motore e stato.
