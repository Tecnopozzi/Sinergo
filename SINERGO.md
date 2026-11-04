# Sinergo

La guida completa (italiano e inglese), installazione, abbinamento a Kairos e sicurezza
in parole semplici è nel [README](README.md#italiano) e in [sinergo/README.md](sinergo/README.md).

Avvio rapido:

```bash
python installa.py --sinergo
```

Cosa fa `--sinergo` oggi:

1. `uv sync --extra voce --extra chatterbox`
2. prepara il `.env` del gateway (chiave generata se manca, **mai stampata**;
   `LOCALROUTER_MODO=sinergo`; ascolto solo su `127.0.0.1` via `SINERGO_BIND_HOST`)
3. registra **una sola** voce di avvio automatico Windows (`HKCU\...\Run\Sinergo` →
   `autostart.vbs sinergo` → `Sinergo.bat`)
4. se `http://127.0.0.1:<PORT>/health` non risponde, avvia il gateway (`server.py`);
   se risponde gia' (LocalRouter acceso), prova anche `GET /v1/models` con la
   chiave che usera' il collegato: 200 → riusa; 401/403 → **non** avvia un
   secondo processo (porta occupata) ed esce con messaggio chiaro
5. avvia il collegato (`python -m collegato`), che legge `API_KEY`/`PORT` dallo
   stesso `.env` (o da `SINERGO_GATEWAY_ENV`, vedi sotto)

Niente cloudflared / tunnel: il gateway Sinergo resta sul computer.

### Limiti e riuso del gateway gia' acceso

- **Motore modelli non incluso.** Sinergo non installa llama-swap, Ollama né
  LM Studio: serve un motore di modelli gia' presente sulla macchina. Senza
  quello il gateway parte ma `/v1/models` resta vuoto.
- **Gateway acceso con un'altra chiave.** Se sulla stessa `PORT` c'e' gia' un
  LocalRouter (es. installazione `--server` in `C:\Localrouter`) e la chiave
  nel `.env` di Sinergo non coincide, l'avvio si ferma senza lanciare un
  secondo gateway. Due soluzioni (la chiave non viene mai stampata):
  1. copia la `API_KEY` del LocalRouter acceso nel `.env` di Sinergo; oppure
  2. imposta `SINERGO_GATEWAY_ENV=<percorso del suo .env>` (es.
     `C:\Localrouter\.env`): Sinergo legge da li' `API_KEY` e `PORT` per il
     controllo di riuso e per il collegato, senza duplicare segreti a mano.
     Se il file indicato non esiste, errore chiaro.

Su Windows, dopo la prima installazione: `Sinergo.bat` (stesso ordine: gateway se
serve, poi collegato).
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
**chatterbox** (Chatterbox Multilingual via [codec.cpp](https://github.com/Tecnopozzi/codec.cpp)
v0.1.0-kairos1: una sola via per CPU, AMD e NVIDIA/Vulkan).
Dopo il download, chatterbox fa da solo la prova di velocità. Soglie dal corpo
(`soglie`: chiacchierata `{rtf_max, primo_audio_max_s}`, messaggi `{rtf_max}`;
default 0,5 / 1,0 / 8,0): rosso per quel ruolo → `stato: errore, motivo: lento`
(con i numeri). Motore che non parte → `ok: false, motivo: dipendenze|motore`.
`prova_motore` risponde `{rtf, primo_audio_s, esiti, soglie}` (niente più `esito`
singolo). Lo stato salvato col vecchio ruolo `voce` migra a `voce_chiacchierata`
alla lettura.

Kairos sceglie solo ruolo e motore, mai un URL. I pesi stanno nella cartella dati di
Sinergo (`~/.config/localrouter/voce/` su Linux), fuori dal codice. Dipendenze
opzionali: `uv sync --extra voce --extra chatterbox` (ruota ufficiale codec.cpp;
su macOS/arm l'extra non installa nulla). `installa.py --sinergo` lo fa da solo.
Senza l'extra, `prepara_ruolo` risponde `stato: errore, motivo: dipendenze`.
La scheda riporta i tre ruoli con `motore`, `stato` e, per le voci, `rtf` /
`primo_audio_s` se misurati.
