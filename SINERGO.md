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
| `prepara_ruolo` | Scarica i pesi fissati nel codice (URL + sha256), verifica l'impronta; soglie opzionali |
| `ascolto` → `ascolta` | WAV 16 kHz mono → testo |
| `voce_*` → `parla` | Testo → WAV (`per`: `chiacchierata` \| `messaggi`, default chiacchierata) |
| `prova_motore` | Misura `rtf` e `primo_audio_s` su 3 frasi fisse → `esiti` chiacchierata/messaggi |

Motori: ascolto solo **parakeet**; le voci **kokoro** (fp32), **supertonic** o
**chatterbox** (Chatterbox Multilingual / ResembleAI, su CPU, AMD o NVIDIA).
Dopo il download, chatterbox fa da solo la prova di velocità. Soglie dal corpo
(`soglie`: chiacchierata `{rtf_max, primo_audio_max_s}`, messaggi `{rtf_max}`;
default 0,5 / 1,0 / 8,0): rosso per quel ruolo → `stato: errore, motivo: lento`
(con i numeri). Motore che non parte → `ok: false, motivo: dipendenze|motore`.
`prova_motore` risponde `{rtf, primo_audio_s, esiti, soglie}` (niente più `esito`
singolo). Lo stato salvato col vecchio ruolo `voce` migra a `voce_chiacchierata`
alla lettura.

Kairos sceglie solo ruolo e motore, mai un URL. I pesi stanno nella cartella dati di
Sinergo (`~/.config/localrouter/voce/` su Linux), fuori dal codice. Dipendenze
opzionali: `uv sync --extra voce` più **un** extra chatterbox scelto dalla scheda —
`chatterbox-cuda` se c’è GPU NVIDIA, altrimenti `chatterbox-cpu` (torch 2.6 CPU;
`setuptools<81` per perth). `installa.py --sinergo` lo sceglie da solo via
`nvidia-smi`. Senza l'extra, `prepara_ruolo` risponde `stato: errore, motivo: dipendenze`.
La scheda riporta i tre ruoli con `motore`, `stato` e, per le voci, `rtf` /
`primo_audio_s` se misurati.
