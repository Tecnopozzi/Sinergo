# Configurazione — Riferimento variabili d'ambiente

Tutte le variabili si impostano nel file `.env` nella root del progetto.  
Copia `.env.example` come punto di partenza:

```powershell
copy .env.example .env
notepad .env
```

---

## Autenticazione e rete

### `API_KEY`
- **Default**: `""` (vuoto = autenticazione disabilitata)
- **Formato**: stringa arbitraria, consigliati 32+ caratteri casuali
- **Effetto**: se valorizzata, tutti gli endpoint tranne `/health` richiedono uno dei seguenti header:
  - `Authorization: Bearer <API_KEY>`
  - `X-API-Key: <API_KEY>`
- **Nota**: `/health` è sempre pubblico — `09B` lo chiama senza header.
- **Generazione sicura**:
  ```powershell
  -join ((48..57 + 65..90 + 97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})
  ```

### `OLLAMA_HOST`
- **Default**: `http://127.0.0.1:11434`
- **Formato**: URL base del server Ollama (senza slash finale)
- **Uso**: tutte le chiamate chat e OCR vengono inoltrate qui

### `HOST`
- **Default**: `127.0.0.1`
- **Formato**: indirizzo IP di bind del server FastAPI
- **Nota**: lasciare `127.0.0.1` per uso locale + tunnel. Cambiare in `0.0.0.0` solo se necessario esporre il server sulla LAN direttamente.

### `PORT`
- **Default**: `8088`
- **Formato**: intero (1-65535)
- **Nota**: 8088 è la stessa porta del progetto `10_personal_openrouter`. Cambiare solo se si vuole far girare entrambi in parallelo.

### `ALLOW_ORIGINS`
- **Default**: `*`
- **Formato**: lista di origin separati da virgola, oppure `*` per permettere tutto
- **Esempio**: `https://app.example.com,https://other.example.com`

### `RATE_LIMIT_PER_MINUTE`
- **Default**: `120`
- **Formato**: intero (0 = disabilitato)
- **Effetto**: limite massimo di richieste per minuto per token API (se `API_KEY` è impostata) o per IP remoto.

### `OLLAMA_TIMEOUT_S`
- **Default**: `600` (10 minuti)
- **Formato**: float (secondi)
- **Effetto**: timeout per le chiamate chat verso Ollama. Aumentare per modelli molto lenti o richieste molto lunghe.

### `ADMIN_UNLOAD_COOLDOWN_S`
- **Default**: `60` (intero, secondi; `0` disabilita, negativi ricondotti a `0`). `POST /admin/unload`, autenticato con `API_KEY`, inoltra `GET /unload` a `LMSTUDIO_HOST` (llama-swap); il cooldown per processo conta anche i tentativi falliti e risponde **429** con `Retry-After`. Esito JSON in `upstream` (testo se non JSON), errori upstream **502**, timeout **10 s**.

### `PULL_RESTART_HTTP_KEY`
- **Default**: `""` (endpoint disattivato)
- **Effetto**: se non vuoto, il server registra **`POST /admin/pull-restart`** (risposta **202**). Il corpo della richiesta è vuoto; l’autenticazione è solo tramite token dedicato (non usa `API_KEY`).
- **Header supportati**:
  - `X-Pull-Restart-Token: <token>`
  - oppure `Authorization: Bearer <token>`
- **Sicurezza**: usare un segreto lungo e casuale; il tunnel è già su HTTPS, ma chi conosce il token può aggiornare il repo e riavviare il servizio sul PC.
- **Privilegi**: il worker PowerShell gira con **lo stesso utente del processo uvicorn** (es. SYSTEM se il task è registrato come tale). In genere può terminare il processo sulla `PORT` e rilanciare `server.py` senza UAC aggiuntivo. Se il processo sulla porta è di un altro utente, potrebbe servire intervento manuale (riavvio manuale del processo).

### `PULL_RESTART_PREDELAY_S`
- **Default**: `3`
- **Effetto**: secondi di attesa all’inizio dello script esterno, così la risposta HTTP **202** torna al client prima del `git pull` e del kill sulla porta.

### `PULL_RESTART_COOLDOWN_S`
- **Default**: `120`
- **Effetto**: minimo intervallo in secondi tra due `POST /admin/pull-restart` accettate. `0` = nessun cooldown.

### `PULL_RESTART_REPO_ROOT`
- **Default**: vuoto → directory due livelli sopra il progetto se il clone è annidato; altrimenti imposta il path della cartella con `.git`.
- **Formato**: path assoluto della cartella che contiene `.git` (es. `C:\path\to\git-root`).

---

## Backend LLM: llama-swap o LM Studio

> **Quale dei due è attivo cambia il significato di metà delle variabili qui sotto.**
> Dall'11/08/2026 la macchina 5060 è in **proxy puro verso llama-swap**
> (`LMSTUDIO_PROXY_PURE=1`, `LMSTUDIO_HOST=http://127.0.0.1:8093`): il ciclo di
> vita dei modelli — quale caricare, con che context, quando liberare la VRAM —
> è tutto di llama-swap e si configura in `C:\llamaswap\config.yaml`, non qui.
> Le variabili `LMSTUDIO_CONTEXT_LENGTH`, `LMSTUDIO_PARALLEL`, `LMSTUDIO_TTL_S`,
> `LMSTUDIO_MODEL_OVERRIDES` e il warmup **restano lettera morta** finché il
> proxy puro è attivo: valgono solo nella via di ritorno a LM Studio.
> Il prefisso degli id (`lmstudio:`) non cambia con il backend.

### `LMSTUDIO_ENABLED`
- **Default**: `1` (abilitato)
- **Valori**: `1` / `0`
- **Effetto**: se `1`, il server contatta il backend all'indirizzo `LMSTUDIO_HOST` per elencare i modelli aggiuntivi. Se il backend non è raggiungibile, il server continua normalmente con soli modelli Ollama (graceful degradation). Impostare `0` per disabilitare completamente ogni interazione con LM Studio/llama-swap.

### `LMSTUDIO_HOST`
- **Default**: `http://127.0.0.1:1234` (LM Studio Local Server)
- **In produzione sulla 5060**: `http://127.0.0.1:8093` (llama-swap)
- **Formato**: URL base del backend (senza slash finale)
- **Prerequisito**: con LM Studio, **Local Server** attivo (menù Server → Start Server); con llama-swap, il processo avviato da `C:\llamaswap\start-llamaswap.cmd`
- **Uso**: tutte le chiamate chat e list-models verso questo backend usano questo URL

### `LMSTUDIO_PROXY_PURE`
- **Default**: `0`
- **In produzione sulla 5060**: `1`
- **Effetto**: con `1` il gateway diventa un **proxy trasparente**: niente CLI `lms`, niente `/api/v0`, niente `ensure_model_context()`, niente TTL né revive. Le richieste chat passano a llama-swap così come arrivano, con i campi extra inclusi (`thinking_budget_tokens`, `chat_template_kwargs`).
- **Perché serve**: llama-swap carica e scarica i modelli da solo in base al modello richiesto. Se il gateway continuasse a gestire il ciclo di vita, i due si contenderebbero la VRAM.
- **Via di ritorno**: `LMSTUDIO_PROXY_PURE=0` + `LMSTUDIO_HOST=http://127.0.0.1:1234` riporta tutto su LM Studio senza toccare gli id dei modelli.

### `LLAMASWAP_CONFIG_PATH`
- **Default**: `""` (disabilitato, nessun percorso implicito).
- **Formato**: percorso del file YAML di llama-swap leggibile dal gateway. Si consiglia un percorso assoluto; quelli relativi sono risolti dalla directory di avvio.
- **Effetto**: durante la ricostruzione della cache di `/v1/models`, legge `models.<id>.cmd`. Se contiene il flag esatto `--mmproj` (anche `--mmproj=...`), aggiunge `vision` alle capability euristiche di quel modello. `--no-mmproj-offload` da solo non basta. Match sull'id del backend, senza `lmstudio:`, case-insensitive.
- **Precedenza**: override `LMSTUDIO_MODEL_CAPABILITIES` (anche lista vuota) > aggiunta da `--mmproj` > euristica sul nome. L'assenza di `--mmproj` non elimina `vision` dedotta dal nome.
- **Fallback**: percorso non configurato, file assente, illeggibile o YAML non valido lasciano il comportamento euristico esistente. Nessun comando viene eseguito e nessun processo viene modificato.

### `LMSTUDIO_MODEL_CAPABILITIES`
- **Default**: `""` (nessun override → capability dedotte da `LLAMASWAP_CONFIG_PATH` e dal nome del modello)
- **Formato**: JSON su una riga `{"<id-modello>":["chat","tools","thinking","vision","coding"], ...}`
- **Match**: uguaglianza esatta sull'`id` come esposto dal backend (senza il prefisso `lmstudio:`).
- **Perché serve**: salvo `vision` ricavata da `--mmproj`, senza override il gateway usa il **nome** e può sbagliare. Per esempio `qwen3-coder-30b` riceve `thinking` perché contiene `qwen3`, anche se non lo supporta. Chi interroga il gateway legge queste capability per decidere a chi mandare cosa: **una capability dichiarata e non vera è peggio di un modello mancante**.
- **Regola**: provare il modello sul campo prima di scriverla (`scripts\prova-capability-modelli.py` fa la prova di `tools`, `thinking` e `vision` su ogni id).
  ```env
  LMSTUDIO_MODEL_CAPABILITIES={"qwen/qwen3-coder-30b":["chat","tools","coding"],"qwen/qwen3-vl-8b":["chat","tools","vision"]}
  ```

### `LMSTUDIO_CONTEXT_LENGTH` / `LMSTUDIO_GPU_OFFLOAD_RATIO`
- **Default**: `16384` / `max`
- **Effetto**: context e GPU offload **di default** applicati al caricamento di un modello LM Studio. Al primo uso di un modello (o se è caricato con un context diverso), il server scarica i modelli LLM già in VRAM e ricarica quello richiesto con questi parametri, tramite la CLI `lms load` (sincrona e affidabile; l'endpoint HTTP `/api/v0/models/load` non applica realmente il context).
- **GPU offload**: `max` = tutti i layer in VRAM; valore `0`–`1` = frazione di layer su GPU (il resto su CPU/RAM). I MoE grandi (`-a4b` / pochi miliardi attivi su decine di miliardi totali) richiedono offload al massimo.

### `LMSTUDIO_PARALLEL`
- **Default**: `1` (il default LM Studio GUI/CLI è `4`)
- **Effetto**: numero massimo di predizioni parallele (slot KV cache). Localrouter passa `--parallel` a `lms load` via `load_model_with_context()`.
- **MoE su 16 GB**: `4` satura la VRAM (~3 tok/s invece di ~60+). `parallel=1` + `context=16384` è la regola; vedi sezione sotto.

### `LMSTUDIO_RETRY_MAX` / `LMSTUDIO_RETRY_DELAY_S`
- **Default**: `18` / `5.0`
- **Effetto**: retry **lungo** su cold-start (modello in caricamento in VRAM). Delay lineare tra tentativi.

### `LMSTUDIO_CHAT_RETRY_MAX` / `LMSTUDIO_CHAT_RETRY_DELAY_S`
- **Default**: `3` / `0.4`
- **Effetto**: retry **breve** su errori transitori chat (502/503, "busy"/slot/queue, errori di connessione/timeout httpx). Delay progressivo (~0.4s → 0.8s → 1.2s). I tentativi compaiono in audit (`lmstudio_retries`); gli errori classificati in `lmstudio_error`.
- **Non ritenta** gli overflow di context (→ 413, vedi sotto).

### `LMSTUDIO_TTL_S`
- **Default**: `86400` (24 ore)
- **Effetto**: il gateway aggiunge `"ttl": <valore>` a **ogni** richiesta chat verso LM Studio e passa `--ttl` al caricamento CLI (`lms load`), così il TTL vale anche quando il modello viene precaricato dal gateway (non solo in JIT puro). LM Studio mantiene il modello caricato per quel tempo dall'**ultimo uso** (idle auto-evict), indipendentemente dalle impostazioni GUI — utile per evitare cold-start serali su modelli come `gemma-4-26b-a4b-it`.
- **`0`**: non inviare il campo `ttl` (comportamento default LM Studio, ~60 min idle).
- **Unload espliciti**: `POST /v1/models/unload` e `/v1/models/unload-all` continuano a scaricare il modello subito (il TTL non li blocca).

### `LMSTUDIO_MODEL_OVERRIDES`
- **Default**: `""` (nessun override → si usano i default sopra)
- **Formato**: JSON su una riga `{"<id-modello>":{"context":<int>,"gpu":<num|"max">,"parallel":<int>}, ...}`
- **Match**: uguaglianza esatta sull'`id` del modello (case-insensitive), poi sottostringa.
- **Perché serve**: con una sola GPU modelli diversi entrano in VRAM con parametri diversi. Esempio RTX 5060 Ti 16 GB:
  ```env
  LMSTUDIO_MODEL_OVERRIDES={"gemma-4-26b-a4b-it":{"context":16384,"gpu":"max","parallel":1},"google/gemma-3-12b":{"context":16384,"gpu":"max"}}
  ```
  Il 26B MoE: **ctx 16384 + gpu max + parallel 1**. Il 12B sta tutto in GPU a ctx 16384.
- **Un solo LLM per volta**: prima di caricare un modello il server scarica gli altri LLM presenti in VRAM. Cambiare modello comporta uno switch di ~20–40 s (unload + load); le richieste successive allo stesso modello non ricaricano.

### Context e modelli MoE — settaggi obbligatori

Meccanismo nato su Meccan-IA per `qwen3.5-27b` e riusato su ogni modello LM Studio nuovo. **Non ripetere i due giorni di debug tipici** (velocità collassata + 502 misteriosi).

1. **Il context lo imposta il gateway, mai `lms load -c …` a mano.**
   - `LMSTUDIO_CONTEXT_LENGTH` (default **16384**)
   - `load_model_with_context()` / `ensure_model_context()` — warmup, primo chat, reload dopo unload/mismatch
   - override per-modello in `LMSTUDIO_MODEL_OVERRIDES`
   - Un `lms load -c 8192` (o parallelo diverso) **scavalca** tutto questo: il context sbagliato resta in VRAM e **sopravvive ai riavvii** del gateway finché non ricarichi via Localrouter. Per correggere: unload (`POST /v1/models/unload`) e lascia che il gateway ricarichi, oppure riavvia Localrouter con warmup attivo.

2. **Perché 16384 e non meno.**
   - Client agentici (system prompt grande + risultati tool accumulati nel loop) sforano **8192** facilmente.
   - Sintomo LM Studio: HTTP **400** `exceeds the available context size` / `exceed_context_size_error`.
   - Versioni vecchie del gateway mascheravano quel 400 come **502** generico → sembrava rete/concorrenza/transitorio. Ora: **413** con `error_kind=context_overflow` e dettaglio in audit (`lmstudio_error`).

3. **Trade-off context × parallel su GPU 16 GB.**
   - KV cache ≈ **context × parallel**. Su MoE grosso (es. `gemma-4-26b-a4b-it`) non si possono avere entrambi alti: **16384×2** spilla su RAM/pagefile e la velocità crolla a ~3 tok/s.
   - Regola: **`parallel=1` + `context=16384`** (stessa VRAM di `8192×2`, senza spill).
   - `parallel>1` serve solo a **vere** richieste concorrenti. Orchestratori tipo Kairos fanno richieste **sequenziali** (L1 poi tool poi L1): i “502 da concorrenza” sospettati erano quasi sempre **overflow di context** travestiti da 502.

4. **Gemma è un MoE (`-a4b` = ~4B attivi su 26B).**
   - Serve **GPU Offload al massimo** (`gpu: "max"`).
   - Il default LM Studio **`Parallel=4`** satura la VRAM: evitarlo sempre su questo modello (override o `LMSTUDIO_PARALLEL=1`).

> **Nota visione 12B**: il modello `gemma-4-12b-it-uncensored` (zaakirio) ha un proiettore vision `gemma4uv` non ancora supportato dal runtime llama.cpp di LM Studio (2.14.0). Il `mmproj` è disabilitato (`.disabled`) e il modello è esposto **solo-testo**. La visione resta disponibile sul 26B. Per riabilitarla servirà un runtime LM Studio basato su llama.cpp b8637+.

---

## Gateway OpenAI-compat

### `MODEL_ALLOWLIST`
- **Default**: `""` (vuoto = tutti i modelli)
- **Formato**: lista di token separati da virgola, case-insensitive, matching parziale
- **Esempio**: `qwen3,magistral,llava` → espone solo modelli il cui nome contiene uno di questi token
- **Effetto**: filtra la risposta di `/v1/models`. Le richieste a modelli non in lista vengono rifiutate con 400.

### `MODELS_CACHE_TTL_S`
- **Default**: `300` (5 minuti)
- **Formato**: float (secondi)
- **Effetto**: la lista modelli viene interrogata da Ollama al massimo ogni N secondi. Riduci a 0 per disabilitare la cache.

### `NON_STREAM_HEARTBEAT_INTERVAL_S`
- **Default**: `10`
- **Formato**: float (secondi), 0 = disabilitato
- **Effetto**: per richieste non-streaming con tempi di risposta lunghi, invia un carattere spazio ogni N secondi per mantenere viva la connessione (evita timeout di Cloudflare a 100s).

### `MODELS_WARMUP_ON_STARTUP`
- **Default**: `1` (abilitato)
- **Valori**: `1` / `0`
- **Effetto**: all'avvio pre-carica la lista modelli da Ollama, così la prima `/v1/models` è istantanea.

### `AUTO_NUM_CTX`
- **Default**: `1` (abilitato)
- **Valori**: `1` / `0`
- **Effetto**: calcola automaticamente `num_ctx` in base alla lunghezza del prompt, entro i limiti `DEFAULT_NUM_CTX` / `MAX_NUM_CTX`. Evita troncamenti su contesti lunghi.

### `DEFAULT_NUM_CTX`
- **Default**: `8192`
- **Formato**: intero (token)
- **Effetto**: `num_ctx` minimo usato quando `AUTO_NUM_CTX` è attivo.

### `MAX_NUM_CTX`
- **Default**: `32768`
- **Formato**: intero (token)
- **Effetto**: `num_ctx` massimo — non verrà mai superato anche con prompt molto lunghi.

### `NUM_CTX_CHARS_PER_TOKEN`
- **Default**: `3.2`
- **Formato**: float
- **Effetto**: stima caratteri per token usata nel calcolo automatico del contesto. Per testo italiano/inglese misto 3.0-3.5 è accurato.

### `NUM_CTX_SAFETY_TOKENS`
- **Default**: `512`
- **Formato**: intero (token)
- **Effetto**: margine aggiunto al `num_ctx` calcolato per la risposta del modello.

---

## OCR Server

### `OCR_MODEL`
- **Default**: `glm-ocr`
- **Formato**: nome esatto del modello Ollama
- **Prerequisito**: `ollama pull glm-ocr` (o il modello specificato)
- **Effetto**: modello usato per tutte le operazioni OCR su immagini e pagine PDF

### `PDF_DPI`
- **Default**: `150`
- **Formato**: intero (DPI)
- **Effetto**: risoluzione di rendering delle pagine PDF prima dell'invio a glm-ocr
- **Suggerimenti**:
  - `100-150`: veloce, sufficiente per testo normale
  - `200`: buon bilanciamento qualità/velocità per documenti tecnici
  - `300`: massima qualità, molto più lento e pesante in RAM
- **Impatto**: DPI alto → immagini più grandi → più tempo per pagina → più RAM usata

### `MAX_PAGES`
- **Default**: `50`
- **Formato**: intero
- **Effetto**: le pagine oltre questo limite vengono ignorate silenziosamente. Protezione contro PDF molto grandi che saturerebbero la RAM o bloccherebbero il server per ore.

### `JOB_TTL_S`
- **Default**: `3600` (1 ora)
- **Formato**: intero (secondi)
- **Effetto**: i job completati (o in errore) vengono rimossi dal job store dopo questo tempo. Libera memoria su sessioni lunghe.

### `OCR_TIMEOUT_S`
- **Default**: `300` (5 minuti)
- **Formato**: float (secondi)
- **Effetto**: timeout per la chiamata OCR verso Ollama **per singola pagina**. Se glm-ocr non risponde entro questo tempo, la pagina viene marcata in errore.
- **Nota**: con PDF di 50 pagine il tempo totale massimo è `MAX_PAGES × OCR_TIMEOUT_S` = 250 minuti nel caso peggiore.

### `IMAGE_MAX_SIDE`
- **Default**: `4000`
- **Formato**: intero (pixel)
- **Effetto**: se il lato lungo di un'immagine supera questo valore, viene ridimensionata proporzionalmente prima di essere inviata a glm-ocr. Evita OOM su immagini ad altissima risoluzione.

---

## Coda e worker OCR

### `OCR_MAX_CONCURRENT_JOBS`
- **Default**: `1`
- **Formato**: intero ≥ 1
- **Effetto**: numero di task worker che eseguono `run_ocr()` in parallelo. Ogni worker preleva dalla coda centrale. Valori >1 aumentano il carico simultaneo su Ollama/GPU.

### `OCR_QUEUE_MAX_SIZE`
- **Default**: `0`
- **Formato**: intero ≥ 0 (0 = coda illimitata)
- **Effetto**: massimo numero di job **accodati ma non ancora avviati** (backlog). Se è piena, `POST /process` risponde `503`.

### `OCR_ALLOW_CANCEL`
- **Default**: `1`
- **Valori**: `1` / `0`
- **Effetto**: se `0`, `POST /cancel/{job_id}` e `POST /cancel-all` rispondono sempre `403`.

### `OCR_JOB_SHUTDOWN_GRACE_S`
- **Default**: `30`
- **Formato**: float (secondi)
- **Effetto**: nella chiusura dell'app, tempo massimo di attesa che i worker finiscano dopo le sentinelle di shutdown prima del `cancel` forzato.

---

## Media Enhancement — SUPIR (opzionale)

Modulo **restore + upscale** foto via [SUPIR-v0F](https://github.com/Fanghua-Yu/SUPIR). Richiede **GPU NVIDIA dedicata** e venv Python **3.10** separato (`.venv-media`).

**Setup automatico** (dopo `git clone` su macchina nuova):

```powershell
cd <cartella-del-progetto>
.\scripts\setup_media.ps1
.\scripts\verify_media.ps1
.\start-gateway.cmd
```

I pesi modelli (~12 GB SDXL+SUPIR + ~10 GB CLIP in cache locale) **non sono in git** — lo script li scarica in `MEDIA_MODELS_DIR` (es. `D:\Model\media`).

### `MEDIA_ENABLED`
- **Default**: `0`
- **Valori**: `1` / `0`
- **Effetto**: se `1`, registra `/media/process`, `/media/status`, `/media/result` e espone `media_ok` in `/health`.

### `MEDIA_VENV_PYTHON`
- **Default**: `.venv-media/Scripts/python.exe` (relativo alla root progetto)
- **Formato**: path assoluto o relativo al `.exe` Python 3.10 del venv media
- **Nota**: impostato automaticamente da `setup_media.ps1`

### `MEDIA_MODELS_DIR`
- **Default**: `models/media` (relativo alla root del server)
- **Consigliato**: path assoluto fuori server, es. `D:\Model\media`
- **Contenuto**: `sd_xl_base_1.0_0.9vae.safetensors`, `SUPIR-v0F.ckpt`, cache CLIP in `hf_cache/`

### `MEDIA_SUPIR_ROOT` / `MEDIA_SUPIR_YAML`
- **Default**: `vendor/SUPIR` e `options/SUPIR_v0_12gb.yaml`
- **Nota**: YAML generato da setup con path assoluti alla macchina corrente

### `MEDIA_DEFAULT_UPSCALE`
- **Default**: `2`
- **Valori**: `2` o `4`

### `MEDIA_DEFAULT_FIDELITY`
- **Default**: `0.85`
- **Range**: `0.0`–`1.0` — più alto = output più fedele all'originale (meno rigenerazione)

### `MEDIA_SUPIR_NO_LLAVA` / `MEDIA_SUPIR_USE_TILE_VAE` / `MEDIA_SUPIR_HALF_PARAMS`
- **Default**: `1` / `1` / `1`
- **Effetto**: profilo **12 GB VRAM** — disabilita LLaVA, usa VAE tiled e pesi half

### `MEDIA_MAX_CONCURRENT_JOBS`
- **Default**: `1`
- **Nota**: consigliato `1` su GPU 12 GB

### `MEDIA_INFERENCE_TIMEOUT_S`
- **Default**: `1800` (30 min)
- **Effetto**: timeout subprocess inferenza SUPIR per job

### `MEDIA_MAX_INPUT_BYTES` / `MEDIA_MAX_OUTPUT_SIDE`
- **Default**: `26214400` (25 MB) / `4096`
- **Effetto**: limiti input upload e lato lungo output

---

## Whisper — trascrizione audio (opzionale)

Modulo di **trascrizione audio** basato su [faster-whisper](https://github.com/SYSTRAN/faster-whisper) in un venv Python **3.10/3.11/3.12** separato (`.venv-whisper`). Su PC con GPU NVIDIA il setup usa **large-v3 + CUDA**; senza GPU usa **base + CPU**.

**Setup automatico** (dopo `git clone` su macchina nuova — auto-installante, nessun path hardcoded):

```powershell
cd <cartella-del-progetto>
.\scripts\setup_whisper.ps1            # GPU: large-v3 cuda float16 + prefetch ~3 GB
.\scripts\setup_whisper.ps1 -CpuProfile # CPU: base int8
.\scripts\verify_whisper.ps1
.\start-gateway.cmd
```

Il modello viene scaricato in **`WHISPER_MODELS_DIR`** (default `models/whisper`, path assoluto consigliato es. `D:\Model\whisper`) — **non è in git**. Su GPU RTX 3060 12 GB `large-v3` entra in VRAM dopo unload degli LLM LM Studio (`WHISPER_AUTO_UNLOAD_LLMS=1`).

**Endpoint admin** (stesso `Authorization: Bearer <API_KEY>` degli altri moduli):

| Endpoint | Effetto |
|----------|---------|
| `POST /whisper/download-model` | Prefetch pesi (query opzionale: `model`, `device`, `compute_type`) |
| `POST /whisper/unload-llms` | Scarica tutti i modelli LM Studio dalla VRAM prima di trascrivere |

### `WHISPER_ENABLED`
- **Default**: `0`
- **Valori**: `1` / `0`
- **Effetto**: se `1`, registra `/v1/audio/transcriptions`, `/v1/audio/translations`, `/audio/transcribe` ed espone `whisper_ok` in `/health`.

### `WHISPER_VENV_PYTHON`
- **Default**: `.venv-whisper/Scripts/python.exe` (relativo alla root progetto)
- **Nota**: impostato automaticamente da `setup_whisper.ps1`

### `WHISPER_MODELS_DIR`
- **Default**: `models/whisper` (relativo alla root del server)
- **Consigliato**: path assoluto fuori dalla cartella server, es. `D:\Model\media` o `D:\Model\whisper`
- **Contenuto**: modelli faster-whisper convertiti (cache locale, offline dopo il setup)
- **Nota**: il runtime (`whisper_pipeline`, `POST /whisper/download-model`) legge sempre questa variabile da `.env`

### `WHISPER_MODEL`
- **Default**: `large-v3` (GPU) / `base` (CPU via `-CpuProfile`)
- **Valori**: `tiny` | `base` | `small` | `medium` | `large-v3` (o path locale)
- **Effetto**: più grande = più accurato ma più lento/pesante. `large-v3` ~3 GB, consigliato su 3060 12 GB con unload LLM.

### `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`
- **Default GPU**: `cuda` / `float16`
- **Default CPU** (`-CpuProfile`): `cpu` / `int8`
- **Nota**: su Windows il setup installa `nvidia-cublas-cu12` e `nvidia-cudnn-cu12` nel venv; il server aggiunge automaticamente le DLL al PATH del worker.

### `WHISPER_AUTO_UNLOAD_LLMS`
- **Default**: `1`
- **Effetto**: prima di ogni trascrizione CUDA, chiama unload di tutti i modelli LM Studio (libera VRAM per large-v3).

### `WHISPER_DEFAULT_LANGUAGE`
- **Default**: *(vuoto = autodetect)*
- **Formato**: codice ISO 639-1 (es. `it`, `en`)

### `WHISPER_BEAM_SIZE` / `WHISPER_VAD_FILTER`
- **Default**: `5` / `1`
- **Effetto**: beam search e filtro VAD (rimozione silenzi) per qualità/velocità

### `WHISPER_MAX_CONCURRENT_JOBS` / `WHISPER_TIMEOUT_S`
- **Default**: `1` / `1800`
- **Effetto**: trascrizioni serializzate (semaphore) e timeout per file

### `WHISPER_MAX_INPUT_BYTES`
- **Default**: `104857600` (100 MB)
- **Effetto**: dimensione massima upload audio

---

## Voce esterna — speaches (`VOCE_URL`)

Se `VOCE_URL` punta a un servizio [speaches](https://github.com/speaches-ai/speaches) locale
(OpenAI-compat, tipicamente `http://127.0.0.1:8201`), LocalRouter **inoltra** le rotte
audio OpenAI-compat a quel servizio invece di usare il Whisper interno.

| Endpoint | Comportamento con `VOCE_URL` |
|----------|------------------------------|
| `POST /v1/audio/transcriptions` | multipart inoltrato a `VOCE_URL` + stesso percorso |
| `POST /v1/audio/translations` | idem |
| `POST /v1/audio/speech` | JSON inoltrato a `VOCE_URL/v1/audio/speech` → byte audio |

`/audio/transcribe` e gli admin Whisper restano sul percorso interno (`WHISPER_ENABLED`).
Senza `VOCE_URL` il comportamento e' quello storico (Whisper se abilitato; `/speech` → 404).
Timeout verso speaches: **180 s**. Errore remoto → **502** con messaggio chiaro.

### `VOCE_URL`
- **Default**: vuoto
- **Esempio**: `http://127.0.0.1:8201`
- **Effetto**: abilita l'inoltro STT/TTS OpenAI-compat verso speaches

Default se il chiamante non passa i campi:
- trascrizione `model`: `Systran/faster-whisper-small`
- speech `model`: `speaches-ai/Kokoro-82M-v1.0-ONNX-int8`, `voice`: `if_sara`, `response_format`: `wav`
  (sul 5060 non c'e' FFmpeg: mp3/opus non disponibili)

---

## Logging

### `REQUESTS_LOG_PATH`
- **Default**: `logs/requests.jsonl`
- **Formato**: path relativo alla root del progetto, oppure path assoluto
- **Effetto**: ogni richiesta completata viene aggiunta come riga JSON a questo file. E' il file di **audit log** del server.
- **Contenuto**: ogni record include metodo, path, start/end, durata, status, IP, applicativo chiamante (se noto), modello richiesto, payload richiesta e riepilogo risposta.
- **Compatibilità**: non richiede modifiche ai client esistenti. I client nuovi **possono** identificarsi con header opzionali:
  - `X-Client-Name`
  - `X-Client-Id`
  - `X-Client-Version`
- **Fallback legacy**: se gli header non ci sono, il server usa `User-Agent` e IP per popolare `client.application.resolved_name`.
- **Formato riga log**:
  ```json
  {
    "request_id": "req-...",
    "method": "POST",
    "path": "/v1/chat/completions",
    "started_at": "2026-07-07T10:14:17.371000+00:00",
    "finished_at": "2026-07-07T10:14:24.912000+00:00",
    "duration_ms": 7541,
    "status_code": 200,
    "model": "qwen3:14b",
    "client": {
      "ip": "2a01:4f8:1c19:f539::1",
      "application": {
        "name": "speaker-splitter",
        "id": "worker-01",
        "version": "2026.07.07",
        "resolved_name": "speaker-splitter"
      }
    },
    "request_items": [
      {"type": "message", "index": 0, "value": {"role": "user", "content": "Prompt..."}}
    ]
  }
  ```
- **Disattivare**: impostare a stringa vuota `REQUESTS_LOG_PATH=`

### Consultazione log richieste

- Endpoint: `GET /debug/requests`
- **Auth**: stessa di `API_KEY` (se abilitata)
- Query opzionali:
  - `n` → ultime N righe (max 500)
  - `path` → filtro per path esatto
  - `model` → filtro per sottostringa del modello
  - `client_name` → filtro per sottostringa su `client.application.resolved_name`

---

## Esempi .env completi

### Sviluppo locale (no auth, no CORS)
```env
API_KEY=
OLLAMA_HOST=http://127.0.0.1:11434
LMSTUDIO_ENABLED=1
LMSTUDIO_HOST=http://127.0.0.1:1234
PORT=8088
OCR_MODEL=glm-ocr
PDF_DPI=150
```

### Produzione (API_KEY, rate limit, allowlist modelli, proxy puro verso llama-swap)
```env
API_KEY=replace-me-with-installa-py
OLLAMA_HOST=http://127.0.0.1:11434
LMSTUDIO_ENABLED=1
LMSTUDIO_HOST=http://127.0.0.1:8093
LMSTUDIO_PROXY_PURE=1
LMSTUDIO_MODEL_CAPABILITIES={"qwen/qwen3-coder-30b":["chat","tools","coding"],"qwen/qwen3-vl-8b":["chat","tools","vision"]}
HOST=127.0.0.1
PORT=8088
ALLOW_ORIGINS=https://app.example.com
RATE_LIMIT_PER_MINUTE=60
OLLAMA_TIMEOUT_S=600
MODEL_ALLOWLIST=qwen3,magistral,lmstudio
OCR_MODEL=glm-ocr
PDF_DPI=200
MAX_PAGES=30
OCR_TIMEOUT_S=300
IMAGE_MAX_SIDE=4000
REQUESTS_LOG_PATH=logs/requests.jsonl
```

> **Nota `MODEL_ALLOWLIST` con LM Studio**: aggiungere `lmstudio` all'allowlist per includere tutti i modelli LM Studio (il filtro confronta per sottostringa sull'`id` con prefisso già applicato, quindi `lmstudio` matcha `lmstudio:qualsiasi-modello`).

### OCR ad alta qualità (documenti tecnici)
```env
PDF_DPI=300
MAX_PAGES=20
OCR_TIMEOUT_S=600
IMAGE_MAX_SIDE=6000
```

## Client: più server LocalRouter

`LOCALROUTER_MODO=client` avvia il router locale, sempre su `127.0.0.1:PORT`.
`HOST` non può esporre il client alla rete. Il file `server_remoti.yaml`, nella stessa
cartella del `.env`, ha precedenza su `LOCALROUTER_SERVER` / `LOCALROUTER_CHIAVE`:

```yaml
'5060':
  indirizzo: https://5060.example.it
  chiave: CHIAVE_DEL_5060
'3060':
  indirizzo: https://3060.example.it
  chiave: CHIAVE_DEL_3060
```

Il file non va committato. L'installatore lo scrive atomicamente con permessi 0600.
I nomi sono univoci, con lettere ASCII, cifre, `_`, `-`, `.`; non possono contenere
barre né coincidere con segmenti API riservati (`v1`, `health`, `media`, ecc.). Usare
indirizzi HTTP(S) senza credenziali, query o frammenti. Le chiavi non vengono stampate
nelle operazioni client. L'ordine è significativo: il primo nodo gestisce servizi
senza selettore e ha precedenza sui modelli non prefissati condivisi.

```bash
python installa.py --client --nome 5060 --indirizzo https://5060.example.it
python installa.py --client --nome 3060 --indirizzo https://3060.example.it
python installa.py --client --elenco
python installa.py --client --rimuovi 3060
```

La chiave viene richiesta senza eco (oppure passarla con `--chiave`). Ogni aggiunta
verifica `/v1/models` con la chiave prima di scrivere; duplicati e server non validi
sono rifiutati. Riavviare il client per applicare il registro aggiornato. Se il file
manca, il vecchio `.env` resta valido: `/health.server_name` determina il nome del
singolo server. Se si aggiunge un nodo alla configurazione legacy, il vecchio server
viene conservato per primo. Un file errato interrompe l'avvio; `{}` lascia l'elenco
vuoto e non riattiva il vecchio server del `.env`.

Il catalogo aggregato ha cache di **30 s**, richieste parallele e timeout di **3 s** per
server. Gli id esposti sono `5060/<modello>` e `3060/<modello>`, con `owned_by` uguale al
nome del nodo; i cataloghi spenti o non validi vengono omessi. Modelli JSON e form
sono instradati separando sulla prima barra: `5060/lmstudio:qwen/qwen3-vl-8b` diventa
`lmstudio:qwen/qwen3-vl-8b` sul 5060. Nessun ripiego per un prefisso esplicito.
Modelli senza prefisso: primo catalogo che li contiene, header `X-LocalRouter-Server`
e log della scelta. Modello assente o server sconosciuto: 404; nodo scelto spento: 502;
prefissi discordanti: 400.

Per OCR, TTS senza modello, tokenizer, unload, context, dashboard, debug e metriche,
usare `/<nome>/<percorso>`. Esempi: `/5060/process`, `/3060/v1/models/unload-all`.
Senza selettore viene contattato solo il primo nodo: nessuna operazione collettiva
implicita. `GET /health` locale elenca `servers` con `{nome, indirizzo, raggiungibile,
modelli}` dalla cache; `/3060/health` inoltra la chiamata solo al 3060.

Le risposte di creazione `/v1/chat/jobs`, `/process` e `/media/process` riscrivono solo
il campo `job_id` come `<server>/<job_id>`. L'id intero va usato in status, result,
cancel ed eventi, ad esempio `/v1/chat/jobs/3060/chatjob-abc/events` oppure
`/media/result/3060/abc`. Il percorso completo viene accettato dal client, poi la
parte server viene tolta prima dell'inoltro. Risposte successive, file binari ed SSE
mantengono il contenuto originale del remoto. Vedere la sezione client del README.

### Client compatibile con LM Studio e Ollama

Il client espone `/api/v0/models`, `/api/v1/models`, `/api/tags` e `/api/ps`,
oltre al catalogo OpenAI `/v1/models`. Gli identificatori conservano il prefisso
server, per esempio `5060/lmstudio:autore/modello`, anche nella chat Ollama.
Le eventuali porte `LOCALROUTER_CLIENT_EXTRA_PORTS` espongono la stessa
configurazione multi-server e ascoltano soltanto su `127.0.0.1`.

Per leggere lo stato il client interroga `/v1/models?runtime=true` dei server.
Il server mantiene la cache del catalogo, ma legge nuovamente i modelli caricati:
`/api/v0/models` per LM Studio, `/running` per llama-swap e `/api/ps` per Ollama.
Per llama-swap conta soltanto lo stato `ready`, secondo il
[formato dell'API upstream](https://github.com/mostlygeek/llama-swap/blob/main/internal/server/api.go).
Il client conserva la cache dei cataloghi legacy senza stato, mentre quelli con
`loaded` vengono riletti per vedere subito caricamenti e scaricamenti.

`/api/tags` elenca tutti i modelli disponibili con il campo aggiuntivo `loaded`;
`/api/ps` include soltanto quelli certamente caricati. Se il backend non risponde,
lo stato resta sconosciuto (`loaded: null`, `state: unknown`), senza inventare
istanze caricate. Con tutti i server remoti irraggiungibili le API compatibili
restituiscono 502. `/api/v1/models/load` conferma soltanto un modello già caricato:
restituisce 409 se scarico o sconosciuto al runtime, 404 se assente dal catalogo;
il caricamento effettivo resta demandato alla prima inferenza.
