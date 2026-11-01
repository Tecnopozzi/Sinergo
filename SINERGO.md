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

## Ruoli: ascolto e due voci

Kairos può assegnare a questa macchina i ruoli **ascolto** (trascrizione),
**voce_chiacchierata** (dal vivo, conta la velocità) e **voce_messaggi**
(vocali preparati, conta la qualità). Sul canale firmato:

| Lavoro | Cosa fa |
|--------|---------|
| `prepara_ruolo` | Scarica i pesi fissati nel codice (URL + sha256), verifica l'impronta |
| `ascolto` → `ascolta` | WAV 16 kHz mono → testo |
| `voce_*` → `parla` | Testo → WAV (`per`: `chiacchierata` \| `messaggi`, default chiacchierata) |
| `prova_motore` | Misura `rtf` e `primo_audio_s` su 3 frasi fisse → `esito` verde/rosso |

Motori: ascolto solo **parakeet**; le voci **kokoro** (fp32), **supertonic** o
**chatterbox** (Chatterbox Multilingual / ResembleAI, solo con GPU NVIDIA).
Senza NVIDIA, chatterbox risponde `motivo: hardware`. Dopo il download,
chatterbox fa da solo la prova di velocità (verde = rtf < 0,5 e primo_audio_s ≤ 1,0):
per `voce_chiacchierata` un rosso diventa `stato: errore, motivo: lento` (con i numeri);
per `voce_messaggi` resta `pronto`. Lo stato salvato col vecchio ruolo `voce` migra a
`voce_chiacchierata` alla lettura.

Kairos sceglie solo ruolo e motore, mai un URL. I pesi stanno nella cartella dati di
Sinergo (`~/.config/localrouter/voce/` su Linux), fuori dal codice. Dipendenze
opzionali: `uv sync --extra voce` (CPU) e `uv sync --extra voce --extra voce-gpu`
(chatterbox). Senza l'extra, `prepara_ruolo` risponde `stato: errore, motivo: dipendenze`.
La scheda riporta i tre ruoli con `motore`, `stato` e, per le voci, `rtf` /
`primo_audio_s` se misurati.
