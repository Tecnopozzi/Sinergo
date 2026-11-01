# Architettura — Localrouter, Gateway OpenAI-compat + OCR Server

---

## Struttura file

```
C:\Localrouter\
│
├── server.py              ← Entry point FastAPI, tutti gli endpoint
├── pull_restart_runner.py ← Accoda worker PowerShell: git pull + kill PORT + avvio server
├── POST /admin/pull-restart     ← Stesso flusso da esploratore (root repo da ..\..)
├── config.py              ← Carica .env, espone costanti tipizzate
├── auth.py                ← Middleware Bearer token / X-API-Key + token pull-restart
├── schemas.py             ← Modelli Pydantic request/response
├── ollama_client.py       ← Client httpx verso Ollama (API native: /api/tags, /api/chat)
├── lmstudio_client.py     ← Client httpx verso llama-swap / LM Studio (API OpenAI-compat: /v1/*)
│                            in proxy puro inoltra i campi extra intatti
│                            (thinking_budget_tokens, chat_template_kwargs)
│
├── ocr_pipeline.py        ← Orchestrazione OCR: PDF→immagini→glm-ocr→blocks
├── ocr_scheduler.py       ← Coda asyncio + worker pool (OCR_MAX_CONCURRENT_JOBS)
├── pdf_utils.py           ← Rendering PDF→PNG con pypdfium2
├── image_utils.py         ← Normalizzazione immagini (PNG, resize)
├── job_store.py           ← Job store in-memory (asyncio.Lock)
│
├── media/                 ← Modulo Media SUPIR (opzionale, MEDIA_ENABLED=1)
│   └── ...                  worker subprocess in .venv-media (GPU)
├── transcribe/            ← Modulo Whisper (opzionale, WHISPER_ENABLED=1)
│   ├── whisper_worker.py    ← Eseguito in .venv-whisper: faster-whisper → JSON
│   ├── whisper_pipeline.py  ← Orchestrazione subprocess (async, semaphore)
│   └── whisper_check.py     ← Runtime check per /health (whisper_ok)
│
├── requirements.txt
├── requirements-media.txt
├── requirements-whisper.txt
├── setup_media.bat / setup_whisper.bat
├── scripts/setup_whisper.ps1 / verify_whisper.ps1
├── .env.example
├── .gitignore
├── run.ps1 / run.bat
├── start-production.cmd / start-gateway.cmd  ← avvio catena completa / solo gateway
├── Install-Autostart-AsUser.bat              ← autostart senza PowerShell (ATD-safe)
├── (note antivirus locali, non pubblicate)    ← eccezioni antivirus e ripristini
├── PRESENTAZIONE-LOCALROUTER.md              ← descrizione del servizio per chi non e' tecnico
├── scripts/
│   ├── pull_restart_10b.ps1        ← git pull main + stop listener PORT + start server.py
│   ├── allinea-modelli-llamaswap.py ← LM Studio → C:\llamaswap\config.yaml
│   ├── valida-config-llamaswap.py   ← controlla il config.yaml prima dell'avvio
│   ├── prova-capability-modelli.py  ← verifica tools/thinking/vision sul campo
│   └── ...                          ← diagnosi, misure, watchdog (vedi § Script)
│
│  [fuori dal repo, ma parte della catena]
│  C:\llamaswap\config.yaml          ← catalogo modelli servito alla rete
│  C:\llamaswap\logs\llama-swap.log  ← swap e crash del backend
│
├── cloudflared/
│   ├── config.yml             ← Configurazione tunnel reale (non in git)
│   ├── config.yml.example     ← Template
│   ├── install-server-task.ps1  ← Registra Windows Task Scheduler
│   ├── install-service.ps1      ← Installa cloudflared come servizio Windows
│   ├── install-service-manual.ps1
│   ├── fix-service.ps1
│   └── diag-service.ps1
│
├── logs/
│   └── requests.jsonl         ← Audit log richieste JSONL (non in git)
│
├── README.md
├── CONFIGURAZIONE.md
└── ARCHITETTURA.md            ← questo file
```

---

## Moduli — responsabilità

### `server.py`
Entry point principale. Contiene:
- Creazione app FastAPI con `lifespan`: avvio **OcrScheduler** (`start()`), warmup modelli opzionale; su shutdown `sched.shutdown()` + `close_client()` (Ollama) + `lmstudio_client.close_client()` (se abilitato).
- Middleware: logging richieste, CORS, rate limiting
- Tutti gli endpoint:
  - `GET /` — info servizio
  - `GET /health` — stato Ollama + glm-ocr
  - `GET /dashboard` — pagina HTML di monitoraggio (host, ultime inferenze, grafico); `GET /dashboard/data` fornisce il JSON
  - `GET /llm/context` — contesto operativo per un LLM orchestratore (`?format=json|text`)
  - `GET /v1/models` — lista modelli unificata Ollama + LM Studio (con cache TTL); modelli LM Studio hanno prefisso `lmstudio:` nell'`id`
  - `POST /v1/chat/completions` — routing automatico: se `model` inizia con `lmstudio:` → LM Studio, altrimenti → Ollama (streaming, tool calls, immagini)
  - `POST /process` — crea job **`queued`**, `submit_nowait()` nella coda OCR
  - `GET /status/{job_id}` — stato job (cancel mappato come `error` per 09)
  - `GET /result/{job_id}` — risultato job (202 se non ancora pronto)
  - `POST /cancel/{job_id}` — cancella queued / richiede stop running
  - `GET /jobs` — lista sintetica job
  - `POST /cancel-all` — mass cancel
  - Opzionale — se `PULL_RESTART_HTTP_KEY` non è vuoto: `POST /admin/pull-restart` (202) — accoda `scripts/pull_restart_10b.ps1` con pre-ritardo; autenticazione solo con token dedicato (`X-Pull-Restart-Token` o `Authorization: Bearer`), non con `API_KEY`
- Exception handler globale per `OllamaError` e `LMStudioError` → JSON consistente
- `POST /v1/kv/prefisso` e `POST /v1/kv/slot` — rotte autenticate per la
  fotografia KV. llama-swap espone il passthrough nativo
  `/upstream/<modello>/<path>`: seleziona/avvia il modello, rimuove quel prefisso
  e inoltra al suo llama-server. Il gateway lo usa per `/apply-template`,
  `/completion` e `/slots/0`, che non fanno parte dell'API OpenAI comune.

**Audit log richieste**:
- Middleware HTTP che apre un record all'inizio della richiesta e lo chiude a fine risposta.
- Ogni record JSONL contiene almeno:
  - `request_id`, `method`, `path`, `query`
  - `started_at`, `finished_at`, `duration_ms`, `status_code`
  - `client.ip`
  - `client.application.{name,id,version,resolved_name,user_agent}`
  - `model` se presente
  - `request_payload`
  - `request_items` (array con i dati principali della richiesta: messaggi chat, campi form, ecc.)
  - `response_payload` sintetico
- Identificazione applicativo:
  - preferita tramite header opzionali `X-Client-Name`, `X-Client-Id`, `X-Client-Version`
  - fallback automatico a `User-Agent` / IP per compatibilità con client legacy
- Consultazione remota via `GET /debug/requests` (protetto da `API_KEY` se attiva)

**Dashboard e contesto per LLM** (costruiti sull'audit log):
- `GET /dashboard` restituisce una pagina HTML statica (`_DASHBOARD_HTML`, Bootstrap + Chart.js via CDN); `GET /dashboard/data` (protetto) aggrega host+modelli (`_dashboard_hosts`), ultime inferenze (`_dashboard_recent`) e serie temporali per modello su finestre ora/24h/settimana (`_dashboard_series`) leggendo `logs/requests.jsonl`.
- `GET /llm/context` (protetto) produce, tramite `_llm_context_payload`, un catalogo modelli con capability euristiche (`_infer_model_capabilities`), stato `loaded` (LM Studio), uso 24h/7g (`_model_usage_counts`) e routing hint (`_llm_routing_hints`). Con `?format=text` viene reso in markdown (`_render_llm_context_md`) pronto per un system prompt.

**Routing `/v1/chat/completions`**:
```
model inizia con "lmstudio:"?
  SÌ → strip prefisso → lmstudio_client.chat() / chat_stream()
        body in formato OpenAI puro (temperature/seed top-level, non in options{})
        risposta già OpenAI → return diretto
  NO  → _build_ollama_body() → ollama_client.chat() / chat_stream()
        body convertito in formato Ollama (options{}, /api/chat)
        risposta Ollama → conversione → formato OpenAI
```

**`_get_models_cached()`** — costruisce la lista unificata:
1. `list_tags()` da Ollama → filtra per `MODEL_ALLOWLIST` → entry con `owned_by="ollama"`
2. Se `LMSTUDIO_ENABLED`: `lmstudio_client.list_models()` → aggiunge `lmstudio:` come prefisso → entry con `owned_by="lmstudio"`
3. Se un backend non è raggiungibile → warning + graceful skip (l'altro backend funziona comunque)

### `config.py`
- Carica `.env` via `python-dotenv`
- Espone tutte le costanti con tipo corretto (`str`, `int`, `float`, `bool`, `list[str]`, `Path`)
- Helper interni: `_env_str`, `_env_int`, `_env_float`, `_env_list`
- Funzione `is_model_allowed(name)` per filtro `MODEL_ALLOWLIST`
- Variabili LM Studio: `LMSTUDIO_ENABLED`, `LMSTUDIO_HOST`

### `auth.py`
- Dipendenza FastAPI `verify_api_key(request, credentials)`
- Se `API_KEY` è vuota → no auth per nessun endpoint
- Se `API_KEY` è valorizzata:
  - `/health` → sempre pubblico (esentato esplicitamente)
  - Tutti gli altri → richiede `Authorization: Bearer <key>` oppure `X-API-Key: <key>`
- Ritorna `403` (not `401`) per non far trapelare l'esistenza di auth a scanner

### `schemas.py`
Pydantic v2 models:
- **Gateway**: `ChatMessage`, `ChatCompletionRequest`, `ModelEntry`, `ModelsResponse`
- **OCR**: `OcrJobResponse`, `OcrStatusResponse`, `OcrProgress`, `OcrBlock`, `OcrSource`, `OcrResultResponse`, `OcrCancelResponse`, `OcrJobsListResponse`, `OcrCancelAllResponse`

### `ollama_client.py`
Client `httpx.AsyncClient` verso `OLLAMA_HOST` (API native Ollama):
- `list_tags()` → `GET /api/tags`
- `show_model(name)` → `GET /api/show`
- `chat(payload, stream)` → `POST /api/chat` — ritorna risposta completa o async generator SSE
- `ocr_call(image_b64, prompt)` → `POST /api/chat` con immagine base64 — per glm-ocr
- `OllamaError` — eccezione custom per errori upstream

### `lmstudio_client.py`
Client `httpx.AsyncClient` verso `LMSTUDIO_HOST` (API OpenAI-compat nativa di LM Studio):
- `list_models()` → `GET /v1/models` — ritorna lista `data[]` in formato OpenAI
- `chat(body)` → `POST /v1/chat/completions` (stream=False) — risposta già in formato OpenAI
- `chat_stream(body)` → `POST /v1/chat/completions` (stream=True) — async generator di dict chunk OpenAI
- `LMStudioError` — eccezione custom analoga a `OllamaError`

**Differenza chiave rispetto a Ollama**: LM Studio parla direttamente OpenAI-compat, quindi non serve nessuna conversione di formato. Il body della request viene passato quasi invariato e la risposta viene ritornata direttamente al client senza trasformazioni.

### `ocr_scheduler.py`
- **`OcrScheduler`**: proprio `asyncio.Queue` dimensionata da `OCR_QUEUE_MAX_SIZE` (0 illimitato). All'`start()`, crea **`OCR_MAX_CONCURRENT_JOBS`** coroutine **`_worker_loop`** (`asyncio.create_task`).
- **`submit_nowait(OcrWorkItem)`**: enqueue; solleva `asyncio.QueueFull` se backlog pieno (gestito dal route come 503 dopo rollback `store.remove`).
- **`_worker_loop`**: estrae dalla coda; `None` = sentinella di shutdown per worker. `_run_item` applica orchestrazione errore OK.
- **`shutdown()`**: inserisce N sentinelle, attende `OCR_JOB_SHUTDOWN_GRACE_S`, cancella worker ancora vivi.

### `job_store.py`
`JobStore` — dizionario in-memory con `asyncio.Lock`:
- Stati **`queued`, `running`, `done`, `error`, `cancelled`**. Timestamp wall (`created_ts`, `updated_ts`, `started_ts`, `finished_ts`).
- **`request_cancel`** / **`cancel_all`**. Flag **`cancel_requested`** mentre `running`.
- **`maybe_cancel_requested`**: letta dalla pipeline fra le pagine; `True` ⇒ solleva **`OcrCancelled`**.
- **Compatibilità 09**: `get_status()` mappa **`cancelled` → pubblico `error` + `cancelled_by_user`**; campo opzionale **`internal_status`**.

### `ocr_pipeline.py`
`run_ocr(..., cancel_check=..., on_total_pages=..., on_page_complete=...)`:
1. Verifica MIME / PDF vs immagine (`render_pdf_to_pngs`, `coerce_to_png`).
2. `on_total_pages(n)` dopo il preprocessing (pagina/conta).
3. Per ogni pagina: **`await _check_cancel(cancel_check)`** prima della chiamata; poi `ocr_call()`; poi `on_page_complete(i, total)`.
4. Eccezione **`OcrCancelled`**: il worker marca il job **`cancelled`**.

Separazione: **`ocr_scheduler`** (coda/worker), **`job_store`** (stato/cancel), **`run_ocr`** (pipeline file).

### `pdf_utils.py`
`render_pdf_to_pngs(pdf_bytes, dpi, max_pages)` — rendering PDF→PNG via `pypdfium2`.

### `image_utils.py`
`coerce_to_png(...)` — normalizzazione immagine con `Pillow`.

### `transcribe/` (Whisper, opzionale)
Modulo trascrizione audio isolato in un venv Python 3.10/3.11/3.12 (`.venv-whisper`), separato dal server (3.14, non compatibile con faster-whisper).
- **`whisper_pipeline.py`** — `run_transcription(raw_bytes, filename, options)`: salva l'audio in `data/audio_jobs/<job>/`, serializza le richieste con un `asyncio.Semaphore(WHISPER_MAX_CONCURRENT_JOBS)`, lancia `whisper_worker.py` come subprocess nel venv dedicato con timeout, parsa il JSON su stdout, pulisce la temp dir.
- **`whisper_worker.py`** — interprete separato: `WhisperModel(...).transcribe()` (faster-whisper), stampa una riga JSON `{text, language, duration, segments, ...}`. Non importa nulla dal server.
- **`whisper_check.py`** — `check_whisper_runtime()` per `/health`: verifica venv, worker e cache modello (leggero, nessun import di faster-whisper nel processo principale).

Endpoint in `server.py`: `/v1/audio/transcriptions` e `/v1/audio/translations` (OpenAI-compat) + `/audio/transcribe` (nativo). Sincroni. Formati: `json`/`text`/`srt`/`vtt`/`verbose_json`.

---

## Flusso richiesta OCR

```
Client (09B)
    │
    ├─ POST /process  (multipart: file + mode + skip_cache)
    │       │
    │       ├── auth.verify_api_key()
    │       ├── job_store.create(job_id)   stato queued
    │       ├── OcrScheduler.submit_nowait(OcrWorkItem)   ──▶ asyncio.Queue
    │       └── return {"job_id": "..."}

    Scheduler (worker i):
    │       ├── await queue.get()
    │       ├── should_skip_worker_start? (queued→cancelled)
    │       ├── set_running(...)
    │       └── await run_ocr(..., cancel_check, on_page_*)
    │
    ├─ GET /status/{job_id}   [polling ogni N secondi]
    │       └── return {"status": "running", "progress": {...}}
    │
    └─ GET /result/{job_id}
            ├── se non done → 202 + {"status": "running"}
            └── se done     → 200 + OcrResultResponse completo

                 (in background, run_ocr esegue):
                 file_bytes
                     │
                     ├─ PDF? → render_pdf_to_pngs() [pypdfium2]
                     │              └── [PNG_p1, PNG_p2, ...]
                     └─ img? → coerce_to_png() [Pillow]
                                    └── PNG_p1

                 Per ogni PNG:
                     └─ ollama_client.ocr_call(image_b64)
                             └─ POST http://127.0.0.1:11434/api/chat
                                  model: glm-ocr
                                  images: [<base64>]
                             └─ parse risposta → OcrBlock[]
                             └─ job_store.set_page_progress()

                 job_store.set_done(blocks)
```

## Flusso richiesta chat (OpenAI-compat)

```
Client (09B o qualsiasi OpenAI-compat)
    │
    └─ POST /v1/chat/completions
            │
            ├── auth.verify_api_key()
            ├── valida ChatCompletionRequest (Pydantic)
            ├── is_model_allowed(model)?
            ├── calcola num_ctx se AUTO_NUM_CTX
            ├── converti immagini multipart → base64
            │
            ├─ model con prefisso "lmstudio:"?
            │       └─ lmstudio_client.chat() → llama-swap :8093
            │               (in proxy puro: nessun load/unload lato gateway;
            │                thinking_budget_tokens e chat_template_kwargs
            │                passano intatti fino a llama-server)
            │
            ├─ stream=True?
            │       └─ ollama_client.chat(payload, stream=True)
            │               └─ StreamingResponse SSE
            │                   chunk: {"choices":[{"delta":{"content":"..."}}]}
            │                   ...
            │                   data: [DONE]
            │
            └─ stream=False?
                    └─ ollama_client.chat(payload, stream=False)
                            └─ ChatCompletion JSON completo
                               (con heartbeat spazi ogni NON_STREAM_HEARTBEAT_INTERVAL_S)
```

---

## Dipendenze Python

| Pacchetto | Versione | Uso |
|---|---|---|
| `fastapi` | ≥0.111 | Framework web |
| `uvicorn[standard]` | ≥0.29 | Server ASGI |
| `httpx` | ≥0.27 | Client async verso Ollama |
| `pydantic` | ≥2.6 | Validazione request/response |
| `python-dotenv` | ≥1.0 | Caricamento .env |
| `python-multipart` | ≥0.0.9 | Upload file multipart |
| `pillow` | ≥10.0 | Manipolazione immagini |
| `pypdfium2` | ≥4.x | Rendering PDF→PNG |

---

## Deployment su Windows

```
Windows Boot → login utente interattivo   (MAI come SYSTEM: la CLI lms e i modelli
    │                                       vivono nel profilo dell'utente)
    ├─ Task "LMStudioServer" (AtLogon, Interactive)
    │       ├─ C:\llamaswap\start-llamaswap.cmd
    │       │       └─ llama-swap.exe --watch-config  su :8093
    │       │               └─ llama-server (UN modello per volta, es. 16 GB VRAM)
    │       │                  catalogo in C:\llamaswap\config.yaml
    │       │                  log in C:\llamaswap\logs\llama-swap.log
    │       ├─ lms.exe server start          (LM Studio, via di ritorno su :1234)
    │       └─ C:\Localrouter\start-gateway.cmd
    │               └─ .venv\python.exe server.py
    │                       └─ uvicorn → FastAPI su :8088
    │                               ├─ Ollama (servizio separato) su :11434
    │                               │       ├─ glm-ocr  (OCR)
    │                               │       └─ altri modelli Ollama
    │                               └─ llama-swap :8093  [proxy puro]
    │                                      modelli esposti con prefisso "lmstudio:"
    │
    ├─ HKCU\...\Run\Localrouter → start-production.cmd   [rete di sicurezza
    │                             se il task non parte]
    │
    └─ cloudflared (servizio Windows)
            │
            └─ Tunnel (UUID dal pannello Cloudflare)
                    ├─ localrouter.example.com → http://127.0.0.1:8088
                    └─ (altri hostname verso altri servizi, se servono)
```

> **Niente `.lnk` in Startup e niente `powershell -ExecutionPolicy Bypass`**: alcuni
> antivirus mettono in quarantena i primi e classificano i secondi come dropper.

### Script di gestione

| Script | Dove eseguire | Cosa fa |
|---|---|---|
| `run.ps1` | Utente normale | Crea .venv, installa deps, avvia server in foreground |
| `Install-Autostart-AsUser.bat` | Utente normale | Installa l'autostart (task `LMStudioServer` + chiave HKCU Run). Solo `.bat`: niente PowerShell, che ATD blocca |
| `start-production.cmd` | Utente normale | Avvia la catena completa: llama-swap, LM Studio, gateway |
| `start-gateway.cmd` | Utente normale | Avvia il solo gateway (preferisce `server.py`, ripiega su `run_server.py`) |
| `RestartAsUser-Admin.bat` | Admin | Riavvia il gateway come utente interattivo dopo un blocco |
| `cloudflared\install-localrouter-user-task.ps1` | Admin | Registra il task di avvio come **utente interattivo** |
| `cloudflared\install-server-task.ps1` | — | **Deprecato**: registrava il gateway come SYSTEM, e `lms load` falliva |
| `cloudflared\install-service.ps1` | Admin | Installa/aggiorna servizio cloudflared (tunnel) |
| `cloudflared\diag-service.ps1` | Admin | Diagnostica stato servizio cloudflared |
| `cloudflared\fix-service.ps1` | Admin | Tenta riparazione servizio cloudflared |

### Script di diagnosi e manutenzione (`scripts\`)

| Script | Cosa fa |
|---|---|
| `allinea-modelli-llamaswap.py` | Confronta i modelli installati in LM Studio (`lms ls --json`) con quelli in `C:\llamaswap\config.yaml` e aggiunge le voci mancanti. Da eseguire dopo ogni modello nuovo installato |
| `valida-config-llamaswap.py` | Verifica il `config.yaml` prima dell'avvio: YAML valido, GGUF esistenti, context dichiarati |
| `riavvia-llamaswap-con-cautela.py` | Aspetta una finestra di traffico basso, poi riavvia llama-swap e riscalda il modello di produzione |
| `prova-capability-modelli.py` | Prova sul campo `tools`, `thinking` e `vision` di ogni modello — le capability si dichiarano solo dopo questa prova |
| `prova-spegnimento-pensiero.py` | Confronta i modi di spegnere il ragionamento (`thinking_budget_tokens` vs `chat_template_kwargs`) |
| `verifica-chat-template-kwargs.py` | Verifica che il gateway inoltri `chat_template_kwargs` fino al motore |
| `collaudo-nove-modelli.py` | Interroga tutti i modelli esposti e riporta chi risponde, con che capability |
| `diagnosi-richieste-gateway.py` | Analizza `logs/requests.jsonl`: chi chiama, durate, concorrenza, swap di modello |
| `misura-swap-e-vram.py` | Misura quanto costa un cambio di modello e quanta VRAM resta libera |
| `elenco-swap-recenti.py` | Elenco rapido degli ultimi cambi di modello |
| `watchdog-lms.ps1` / `.cmd` | Sorveglia il backend e lo rianima se smette di rispondere |
| `move-pagefile-to-c.ps1` | Sposta il pagefile su SSD (Admin + riavvio): su HDD l'inferenza collassa |

---

## Sicurezza

- **API_KEY** via header Bearer o X-API-Key (no query string)
- **`/health` sempre pubblico** — necessario per health check di 09B senza auth
- **Rate limiting in-memory** per token/IP (non persistente tra riavvii)
- **CORS** configurabile con `ALLOW_ORIGINS`
- **Tunnel Cloudflare** — il server ascolta solo su `127.0.0.1`, non esposto direttamente su internet
- **Log richieste** in `logs/requests.jsonl` (path configurabile, escluso da git)
- **Compatibilità retroattiva**: i client vecchi non devono aggiungere nulla; i nuovi possono auto-identificarsi via header opzionali
- **Job store in-memory** — nessuna persistenza su disco per i risultati OCR

---

## Relazione con gli altri progetti

```
09B_chat_rag_manuale  (cloud)
    ├─ vision.py         → OCR_SERVER_URL  → /process, /status, /result  (sempre Ollama/glm-ocr)
    ├─ providers.py      → LOCALROUTER_API_BASE → /v1/models, /v1/chat/completions
    │       Per usare LM Studio da 09B: model = "localrouter:lmstudio:<nome-modello>"
    │       Il prefisso "lmstudio:" viene riconosciuto da 10b e routato a LM Studio.
    │       Nessuna modifica a providers.py necessaria.
    └─ compito_workflow.py → usa entrambi

LocalRouter  (PC di casa) ← questo progetto
    ├─ sostituisce: 10_personal_openrouter (solo gateway, niente OCR)
    ├─ backend Ollama  → modelli senza prefisso (es. qwen3:14b)
    └─ backend LM Studio → modelli con prefisso "lmstudio:" (es. lmstudio:llama-3-8b-instruct)

09_Meccan-IA  (cloud, versione precedente di 09B)
    └─ aveva OCR_SERVER_URL che puntava a server separato → ora tutto su 10b
```
