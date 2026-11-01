# Sinergo

[English](#english) · [Italiano](#italiano)

---

<a id="english"></a>
## English

*[Versione italiana ↓](#italiano)*

### What Sinergo is

**Sinergo** is the piece of [Kairos](https://github.com/Tecnopozzi) that lives on your *other* computers. It puts that machine’s GPU, local models, and voice tools at the service of your Kairos — without opening ports on your router.

You install Sinergo on a PC (Windows or Linux). It shows a short pairing code. You type that code into Kairos. After that, Kairos can use that computer. The connection is always opened *from* Sinergo *to* Kairos.

### What it does not do

- It does **not** replace Kairos. Kairos stays the place where you talk, plan, and decide.
- It does **not** require you to configure port forwarding or a public IP on the Sinergo machine.
- It does **not** grant Kairos administrator rights. Programs run with the same permissions as the user who started Sinergo.
- It is **not** only a “remote desktop”: it exposes LocalRouter APIs (chat, models, optional OCR/media/whisper) and a signed command channel under your control.

### Roles: listening and two voices

Kairos can ask this machine to **listen** (speech-to-text) and **speak** (text-to-speech) on the signed channel. Speaking splits into live chat (`voce_chiacchierata`, speed matters) and prepared voice notes (`voce_messaggi`, quality matters):

| Job | Body (summary) | Result |
|-----|----------------|--------|
| `prepara_ruolo` | `ruolo`: `ascolto` \| `voce_chiacchierata` \| `voce_messaggi`, `motore`: `parakeet` \| `kokoro` \| `supertonic` \| `chatterbox`; optional `soglie` | Immediate `stato`: `in_corso` / `pronto` / `errore`; downloads only the pinned file list with sha256 checks |
| `ascolta` | base64 WAV (16 kHz mono, max 3 MiB decoded) | `{testo, secondi_audio, secondi_lavoro}` |
| `parla` | text (max 4000 chars), optional voice name, `per`: `chiacchierata` \| `messaggi` (default chiacchierata) | `{audio_b64, secondi_audio, secondi_lavoro}` |
| `prova_motore` | `motore`: `kokoro` \| `supertonic` \| `chatterbox`; optional `soglie` | `{rtf, primo_audio_s, esiti: {chiacchierata, messaggi}, soglie}` |

Listening accepts only **parakeet**; speaking **kokoro** (fp32), **supertonic**, or **chatterbox** (ResembleAI Multilingual on CPU, AMD, or NVIDIA). After download, chatterbox runs a fixed 3-sentence speed check. Thresholds come in the body (`soglie`: chat `{rtf_max, primo_audio_max_s}`, messages `{rtf_max}`; defaults 0.5 / 1.0 / 8.0): red for that role → `stato: errore, motivo: lento`. Engine that will not start → `ok: false, motivo: dipendenze|motore`. Saved state with the old role `voce` migrates to `voce_chiacchierata` on read. Kairos never sends URLs or file paths. Weights live under Sinergo’s data dir (`~/.config/localrouter/voce/` on Linux), outside the repo. Optional deps: `uv sync --extra voce` plus one chatterbox extra — `chatterbox-cuda` if NVIDIA is present, else `chatterbox-cpu` (`installa.py --sinergo` picks via `nvidia-smi`). Without them, `prepara_ruolo` returns `stato: errore, motivo: dipendenze`. The machine card includes the three roles with `motore`, `stato`, and for voices `rtf` / `primo_audio_s` when measured.

### Requirements

| | |
|---|---|
| OS | Windows 10/11 or Linux |
| Python | 3.11 or newer |
| GPU | Optional. Useful if you want local LLM/OCR/whisper on that machine; Sinergo itself runs without a GPU |
| Network | Same LAN as Kairos, **or** the public HTTPS address of your Kairos |

### Install (Kairos pairing)

1. Download and unpack [Sinergo](https://github.com/Tecnopozzi/Sinergo) on the computer you want to connect. Use a normal user account (no administrator required for everyday use).
2. Open a terminal in that folder and run:
   ```bash
   python installa.py --sinergo
   ```
   On Linux: `python3 installa.py --sinergo`. This creates a virtualenv, installs dependencies, and starts Sinergo.
3. A **Sinergo** page opens in the browser with a code in three blocks, for example `K7QM-2XRA-91VD`.
4. In Kairos open **Add a computer**, pick the waiting machine, and type that code.
5. On the same network Sinergo looks for Kairos for about 10 seconds. If it finds more than one, choose yours. If it finds none, paste the address shown in Kairos into **Where is your Kairos?**. Off the LAN the address must start with `https://`.

When you see **Paired with Kairos**, you can close the page. Leave Sinergo running. On Windows start it again with **`Sinergo.bat`** (install also registers start-at-login for your user). On Linux: `.venv/bin/python -m collegato` from the Sinergo folder.

To disconnect: **Remove this computer** in Kairos, or run `.venv/bin/python -m collegato scollega` (Windows: `.venv\Scripts\python.exe -m collegato scollega`). Pairing again needs a new code.

### Pairing with Kairos (same network or address)

| Situation | What you do |
|---|---|
| Same LAN | Usually automatic (DNS-SD). Confirm if several Kairos appear. |
| Different networks | Copy Kairos’s HTTPS address into Sinergo when asked. |
| Code expired / connection dropped during pairing | Sinergo shows a **new** code; use that one. |

The code never leaves the Sinergo machine in logs. Details: `SINERGO_ABBINAMENTO.md`.

### Security in plain words

- **API key (standalone LocalRouter mode):** the server refuses almost everything without a key. Install generates a strong key; store it like a password. Rotate with `python installa.py --rigenera-chiave`.
- **What Kairos can do on the machine:** after pairing, Kairos may use LocalRouter features on that PC and run programs **as your user**. Prefer a non-admin Windows/Linux account.
- **Revoke from either side:**
  - In Kairos: **Remove this computer**.
  - On the machine: `python -m collegato scollega` (keys are kept as dated `.revocato` files, not left active).
- Pairing uses short-lived proofs tied to the on-screen code; the local pairing page listens only on `127.0.0.1`.

More detail: sections on compromised API key and Sinergo in `SICUREZZA.md`, and `SINERGO_ABBINAMENTO.md`.

### Advanced: Sinergo as a standalone LocalRouter server

Experienced users can run this repository as an OpenAI-compatible local gateway (LM Studio, Ollama, OCR, media, Whisper) with or without Kairos:

```bash
python installa.py --server
python server.py
```

See `INSTALLAZIONE.md` and `CONFIGURAZIONE.md`. Client mode (pipe to a remote GPU server): `python installa.py --client --help`.

### License

**To be defined** — Luca will choose a license at public release.

---

<a id="italiano"></a>
## Italiano

*[English version ↑](#english)*

### Cos’è Sinergo

**Sinergo** è il pezzo di Kairos che vive sugli *altri* tuoi computer: mette GPU, modelli e voce di quel computer al servizio del tuo Kairos, senza aprire porte sul router.

Lo installi sul PC da collegare. Mostra un codice. Scrivi il codice in Kairos. Da quel momento Kairos può usare quella macchina. La connessione parte sempre **da** Sinergo **verso** Kairos.

### Cosa non fa

- Non sostituisce Kairos (là restano chat, piani e decisioni).
- Non chiede port forwarding né un IP pubblico sul PC di Sinergo.
- Non dà a Kairos poteri di amministratore: i programmi girano con i permessi dell’utente che ha avviato Sinergo.
- Non è solo un desktop remoto: espone le API di LocalRouter e un canale comandi firmato sotto il tuo controllo.

### Ruoli: ascolto e due voci

Kairos può chiedere a questa macchina di **ascoltare** (STT) e **parlare** (TTS) sul canale firmato. La voce si divide in chiacchierata dal vivo (`voce_chiacchierata`, conta la velocità) e vocali preparati (`voce_messaggi`, conta la qualità):

| Lavoro | Corpo (sintesi) | Risultato |
|--------|-----------------|-----------|
| `prepara_ruolo` | `ruolo`: `ascolto` \| `voce_chiacchierata` \| `voce_messaggi`, `motore`: `parakeet` \| `kokoro` \| `supertonic` \| `chatterbox`; `soglie` opzionali | Subito `stato`: `in_corso` / `pronto` / `errore`; scarica solo l’elenco fisso con verifica sha256 |
| `ascolta` | WAV in base64 (16 kHz mono, max 3 MiB decodificati) | `{testo, secondi_audio, secondi_lavoro}` |
| `parla` | testo (max 4000 caratteri), voce opzionale, `per`: `chiacchierata` \| `messaggi` (default chiacchierata) | `{audio_b64, secondi_audio, secondi_lavoro}` |
| `prova_motore` | `motore`: `kokoro` \| `supertonic` \| `chatterbox`; `soglie` opzionali | `{rtf, primo_audio_s, esiti: {chiacchierata, messaggi}, soglie}` |

Ascolto accetta solo **parakeet**; voce **kokoro** (fp32), **supertonic** o **chatterbox** (Multilingual ResembleAI su CPU, AMD o NVIDIA). Dopo il download chatterbox misura da solo la velocità su 3 frasi fisse. Soglie dal corpo (`soglie`: chiacchierata `{rtf_max, primo_audio_max_s}`, messaggi `{rtf_max}`; default 0,5 / 1,0 / 8,0): rosso per quel ruolo → `stato: errore, motivo: lento`. Motore che non parte → `ok: false, motivo: dipendenze|motore`. Lo stato col vecchio ruolo `voce` migra a `voce_chiacchierata` alla lettura. Kairos non manda URL né percorsi. I pesi stanno nella cartella dati di Sinergo (`~/.config/localrouter/voce/` su Linux), fuori dal codice. Extra: `uv sync --extra voce` più un extra chatterbox — `chatterbox-cuda` se c’è NVIDIA, altrimenti `chatterbox-cpu` (`installa.py --sinergo` sceglie con `nvidia-smi`). Senza dipendenze, `prepara_ruolo` risponde `stato: errore, motivo: dipendenze`. La scheda include i tre ruoli con `motore`, `stato` e, per le voci, `rtf` / `primo_audio_s` se misurati.

### Requisiti

| | |
|---|---|
| Sistema | Windows 10/11 o Linux |
| Python | 3.11 o successivo |
| GPU | Facoltativa (serve se vuoi LLM/OCR/whisper locali su quella macchina) |
| Rete | Stessa LAN di Kairos, **oppure** l’indirizzo HTTPS pubblico del tuo Kairos |

### Installazione (abbinamento a Kairos)

1. Scarica e scompatta [Sinergo](https://github.com/Tecnopozzi/Sinergo) sul computer da collegare. Usa il tuo utente normale, senza privilegi di amministratore per l’uso quotidiano.
2. Apri un terminale nella cartella e avvia:
   ```bash
   python installa.py --sinergo
   ```
   (su Linux: `python3 installa.py --sinergo`). L’installazione prepara le dipendenze e avvia Sinergo.
3. Nel browser si apre una pagina **Sinergo** con un codice in tre blocchi, per esempio `K7QM-2XRA-91VD`.
4. In Kairos apri **Aggiungi un computer**, scegli il computer in attesa e scrivi quel codice.
5. Sulla stessa rete Sinergo cerca Kairos per circa 10 secondi. Se ne trova più di uno, scegli il tuo. Se non lo trova, copia nel campo **Dove si trova il tuo Kairos?** l’indirizzo mostrato in Kairos. Fuori dalla rete locale l’indirizzo deve cominciare con `https://`.

Quando compare **Abbinato a Kairos**, puoi chiudere la pagina. Lascia Sinergo in esecuzione. Su Windows riapri con **`Sinergo.bat`**; l’installazione aggiunge anche l’avvio automatico all’accesso del tuo utente. Su Linux: `.venv/bin/python -m collegato` dalla cartella di Sinergo.

Per scollegarlo: **Togli questo computer** in Kairos, oppure `.venv/bin/python -m collegato scollega` (Windows: `.venv\Scripts\python.exe -m collegato scollega`). Per ricollegarlo servirà un nuovo codice.

Questa sezione allinea e sostituisce il vecchio foglio breve `SINERGO.md` (che ora rimanda qui).

### Abbinamento (stessa rete o indirizzo)

| Situazione | Cosa fai |
|---|---|
| Stessa LAN | Di solito automatico (DNS-SD). Conferma se compaiono più Kairos. |
| Reti diverse | Incolla in Sinergo l’indirizzo HTTPS di Kairos. |
| Codice scaduto / connessione interrotta in abbinamento | Sinergo mostra un **nuovo** codice. |

Il codice non lascia il computer nei log. Dettaglio: `SINERGO_ABBINAMENTO.md`.

### Sicurezza in parole semplici

- **Chiave API (uso LocalRouter autonomo):** senza chiave il server rifiuta quasi tutto. L’installer ne genera una forte; trattala come una password. Rotazione: `python installa.py --rigenera-chiave`.
- **Cosa può fare Kairos sulla macchina:** dopo l’abbinamento può usare le funzioni LocalRouter di quel PC ed eseguire programmi **con i permessi del tuo utente**. Meglio un account senza poteri di amministratore.
- **Revoca da entrambi i lati:**
  - In Kairos: **Togli questo computer**.
  - Sul PC: `python -m collegato scollega` (le chiavi restano come file `.revocato` datati, non restano attive).
- L’abbinamento usa prove legate al codice a schermo; la pagina del codice ascolta solo su `127.0.0.1`.

Approfondimenti: sezioni su chiave compromessa e Sinergo in `SICUREZZA.md`, e `SINERGO_ABBINAMENTO.md`.

### Uso avanzato: server LocalRouter autonomo

Chi è esperto può usare questo stesso codice come gateway locale compatibile OpenAI (LM Studio, Ollama, OCR, media, Whisper), con o senza Kairos:

```bash
python installa.py --server
python server.py
```

Vedi `INSTALLAZIONE.md` e `CONFIGURAZIONE.md`. Modo client (tubo verso un server GPU remoto): `python installa.py --client --help`.

### Licenza

**Da definire** — Luca deciderà al rilascio pubblico.
