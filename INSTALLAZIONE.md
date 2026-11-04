# Installazione da zero — guida operativa

Checklist e problemi reali emersi durante il setup di un server GPU Windows (es. RTX 16 GB).  
Usare insieme a `README.md` e `CONFIGURAZIONE.md`.

---

## 0. Server o client

LocalRouter serve a **condividere una GPU**. Se GPU e programma stanno sulla
stessa macchina basta LM Studio o Ollama.

**Server** (la macchina con la GPU) — il resto di questa guida:

```bash
pip install -r requirements.txt
python installa.py --server          # genera la chiave se manca, la scrive nel .env e la stampa
python server.py
```

- La chiave va data a ogni client. Senza chiave il server rifiuta tutto,
  anche da 127.0.0.1; restano aperte solo `/health` e la pagina `/dashboard`
  (che poi chiede la chiave per leggere i dati).
- `python installa.py --rigenera-chiave` → chiave nuova; la vecchia smette di
  valere al riavvio del server.
- Ascolta su `127.0.0.1` (dietro il tunnel). Per servire la rete di casa:
  `HOST=0.0.0.0` nel `.env`.

**Client** (macchina senza GPU):

```bash
pip install -r requirements.txt
python installa.py --client          # chiede indirizzo e chiave, li prova (GET /v1/models = 200), li salva
python server.py                     # legge LOCALROUTER_MODO=client dal .env
```

- Espone su `127.0.0.1:8088` (mai sulla rete) la stessa API del server,
  **senza chiave**, come Ollama o LM Studio: i programmi lo trovano lì.
- Inoltra ogni route al server aggiungendo chiave e `User-Agent: Mozilla/5.0`
  (Cloudflare blocca quello di python), streaming compreso. Nessuna logica di
  modelli: è un tubo.

**Sinergo** (computer da collegare a Kairos — gateway locale + canale firmato):

```bash
python installa.py --sinergo
```

- Dipendenze voce/chatterbox, `.env` con chiave (generata se manca, **non** stampata),
  `LOCALROUTER_MODO=sinergo`, host di ascolto **solo** `127.0.0.1` (`SINERGO_BIND_HOST`).
- Avvia il gateway se `/health` su `127.0.0.1:<PORT>` non risponde; altrimenti riusa
  un LocalRouter gia' acceso. Poi avvia il collegato.
- **Una sola** voce Run Windows (`Sinergo` → `autostart.vbs sinergo` → `Sinergo.bat`).
- **Niente** cloudflared / tunnel. Chiave obbligatoria anche da loopback (come il server).
- Non installa llama-swap: come `--server`, i backend modelli restano a parte.

Dettaglio: `SINERGO.md` e `sinergo/README.md`.

---

## 1. Ordine consigliato

1. **Hardware e dischi** — SSD per modelli, swap e pagefile (vedi §2).
2. **LM Studio** — modelli scaricati, Local Server, parametri di caricamento (§3).
3. **Ollama** — solo modelli necessari, path su SSD (§4).
4. **Localrouter** — clone, venv, `.env`, test locale (§5).
5. **Cloudflare Tunnel** — servizio Windows, hostname (§6).
6. **Avvio automatico** — Task Scheduler Localrouter + LM Studio (§7).
7. **Whisper** (opzionale) — venv separato, modelli su SSD (§8).
8. **Collaudo** — `/health`, benchmark modelli, tunnel pubblico (§9).

---

## 2. Dischi e pagefile (critico per le prestazioni)

| Percorso | Cosa | Note |
|----------|------|------|
| `C:\` (SSD) | LM Studio modelli | Default `%USERPROFILE%\.lmstudio\models` |
| `C:\Model\ollama` | Modelli Ollama | Impostare `OLLAMA_MODELS` (non HDD tipo `E:\`) |
| `C:\Model\whisper` | Cache Whisper | `WHISPER_MODELS_DIR` in `.env` |
| Pagefile | Swap Windows | **Evitare HDD** se possibile |

### Problema riscontrato: pagefile su HDD (`F:\pagefile.sys`)

Con pagefile su disco meccanico, durante l'inferenza LLM Windows può spillare RAM su HDD → latenza enorme su **tutti** i modelli, non solo MoE.

- Script: `scripts\move-pagefile-to-c.ps1` (richiede Admin + **riavvio**).
- Se `C:\` ha pochi GB liberi, lo spostamento può fallire: servono almeno ~16–32 GB liberi per un pagefile ragionevole.
- In assenza di spazio su `C:\`, il pagefile su HDD è accettabile come compromesso, ma le prestazioni sotto carico RAM peggiorano.

### Python globale ingombrante

Su questa macchina `C:\Python311\Lib\site-packages` occupava **~16 GB** (PyTorch, ComfyUI, cartelle rotte `~orch` / `~-rch` da uninstall incompleti).  
Localrouter usa venv isolati (`.venv`, `.venv-whisper`) — **non cancellare** `C:\Python311` senza ricreare i venv.  
Recupero sicuro: eliminare solo cartelle `~*` in `site-packages` (~10 GB).

---

## 3. LM Studio

### Prerequisiti

- Local Server attivo su `http://127.0.0.1:1234`.
- Task login: `scripts\start-lmstudio-server.bat` (aggiornare il path `lms.exe` se utente diverso da quello del setup).
- In LM Studio: **Server → Start Server**; opzione `autoStartOnLaunch` in  
  `%USERPROFILE%\.lmstudio\.internal\http-server-config.json`.

### Modelli tipici (server 5060)

| ID | Tipo | Quant | Note |
|----|------|-------|------|
| `gemma-4-26b-a4b-it` | MoE VLM | IQ3_XXS ~13.7 GB | Modello principale |
| `google/gemma-3-12b` | VLM | Q4_K_M | Veloce, ctx 16384 |
| `qwen/qwen3-vl-8b` | VLM | Q4_K_M | Vision + tools |
| `qwen2.5-coder-14b-instruct` | LLM | Q6_K | Coding |
| `ornith-1.0-9b` | LLM | Q8_0 | Leggero |

### Problema critico: MoE lento (~3 tok/s invece di ~30+)

**Sintomo**: solo `gemma-4-26b-a4b-it` lentissimo; gli altri modelli sulla stessa GPU vanno bene.

**Causa**: default LM Studio **`Parallel = 4`** (4 slot KV cache). KV ≈ context × parallel: su MoE ~14 GB, `16384×2` o `8192×4` saturano i 16 GB → spill su RAM/pagefile.

**Fix corretto (via gateway, non a mano):**

```env
LMSTUDIO_CONTEXT_LENGTH=16384
LMSTUDIO_PARALLEL=1
LMSTUDIO_MODEL_OVERRIDES={"gemma-4-26b-a4b-it":{"context":16384,"gpu":"max","parallel":1},...}
```

Poi unload e lascia ricaricare Localrouter (`ensure_model_context` / warmup):

```powershell
# Solo unload — il gateway ricarica al prossimo chat o al restart con warmup
curl -X POST "http://127.0.0.1:8088/v1/models/unload?model_id=gemma-4-26b-a4b-it" -H "Authorization: Bearer <API_KEY>"
# oppure start-gateway.cmd
```

**Non** usare `lms load -c 8192 ...` a mano: scavalca gli override e il context sbagliato sopravvive ai riavvii. Dettaglio: **CONFIGURAZIONE.md** § *Context e modelli MoE*.

**Risultato atteso**: **~60–70 tok/s** in benchmark locale; VRAM dedicata ~15.7/16.3 GB stabile (senza Shared in crescita).

### Altre regole LM Studio

| Parametro | Valore consigliato (26B MoE, 16 GB) | Perché |
|-----------|-------------------------------------|--------|
| GPU Offload | `max` (tutti i layer) | MoE (`-a4b`) con layer su CPU = disastro |
| Context Length | `16384` (via gateway) | agent + tools sforano 8192; 16384×1 ≈ VRAM di 8192×2 |
| Parallel | `1` | vedi sopra; client sequenziali non servono slot multipli |
| Quantizzazione | verificare file caricato | IQ3_XXS ≠ IQ4_XS usato su altra macchina |

### API HTTP vs CLI `lms load`

L'endpoint HTTP `/api/v0/models/load` **non applica** in modo affidabile `contextLength` / `gpuOffload`.  
Localrouter usa sempre la CLI:

```text
lms load <model> -c <ctx> --gpu max --parallel 1 --identifier <id> -y
```

Dopo ogni modifica a context/gpu/parallel: **ricaricare il modello** e ritestare.

### Utilizzo GPU nel Task Manager

VRAM ~88% piena + utilizzo compute ~30–50% durante generazione = **normale** per LLM token-by-token.  
Non confondere con "GPU offload al 39%" nello slider LM Studio.

---

## 4. Ollama

```powershell
# Variabile utente/sistema (SSD)
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "C:\Model\ollama", "User")
```

Modelli minimi per questo setup: `glm-ocr:latest` (OCR).  
Evitare di tenere modelli LLM grossi in Ollama se già presenti in LM Studio (contesa VRAM).

Riavviare `ollama serve` dopo cambio path.

---

## 5. Localrouter — `.env` essenziale (5060)

```env
API_KEY=<chiave-lunga>
SERVER_NAME=5060
GPU_INFO=NVIDIA GeForce RTX 5060 Ti 16GB VRAM

LMSTUDIO_ENABLED=1
LMSTUDIO_HOST=http://127.0.0.1:1234
LMSTUDIO_CONTEXT_LENGTH=16384
LMSTUDIO_GPU_OFFLOAD_RATIO=max
LMSTUDIO_PARALLEL=1

LMSTUDIO_MODEL_OVERRIDES={"gemma-4-26b-a4b-it":{"context":16384,"gpu":"max","parallel":1},"google/gemma-3-12b":{"context":16384,"gpu":"max"},"ornith-1.0-9b":{"context":8192,"gpu":"max"},"qwen/qwen3-vl-8b":{"context":8192,"gpu":"max"},"qwen2.5-coder-14b-instruct":{"context":8192,"gpu":"max"}}

WHISPER_ENABLED=1
WHISPER_MODELS_DIR=C:\Model\whisper
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
```

### Problema: dashboard "API key mancante"

La dashboard **non legge** la chiave dal `.env`. Va incollata nel campo in alto (salvata in `localStorage`) oppure:

```text
http://127.0.0.1:8088/dashboard?api_key=<API_KEY>
```

### Problema: reload LM Studio fallito / context mismatch

Se `LMSTUDIO_CONTEXT_LENGTH` o gli override non coincidono con il modello già in VRAM (es. caricato da GUI con ctx diverso), Localrouter forza reload via `lms load`.  
Verificare che la CLI `lms` sia nel PATH dell'utente che esegue il server (task SYSTEM vs utente interattivo).

---

## 6. Cloudflare Tunnel

- Hostname esempio: `https://localrouter.example.com` → `http://127.0.0.1:8088`
- Riferimento: copia `cloudflared\config.yml.example` come `config.yml` e personalizza
- Installazione servizio: `cloudflared\install-service.ps1` (Admin)
- Test: `curl https://localrouter.example.com/health`

---

## 7. Avvio automatico Windows

| Componente | Task / servizio | Note |
|------------|-----------------|------|
| Localrouter | `LocalrouterServer` | `cloudflared\install-server-task.ps1` |
| cloudflared | Servizio Windows | `Automatic`, token Zero Trust |
| LM Studio API | `LMStudioServer` (login) | `scripts\start-lmstudio-server.bat` |

Dopo aggiornamento codice o `.env`: riavviare il task Localrouter (o il processo `server.py`).

---

## 8. Whisper (opzionale)

```powershell
.\scripts\setup_whisper.ps1
# Modelli in C:\Model\whisper (non D: HDD)
```

Su GPU 16 GB: `large-v3` + `WHISPER_AUTO_UNLOAD_LLMS=1` scarica gli LLM LM Studio prima di trascrivere.

---

## 9. Collaudo finale

### Health check

```powershell
curl http://127.0.0.1:8088/health
curl https://localrouter.example.com/health
```

Attesi: `lmstudio_ok: true`, `whisper_ok: true`, elenco modelli LM Studio.

### Benchmark velocità (dopo `parallel=1` su gemma)

```powershell
# Prompt ~150 parole, max_tokens 200 — atteso ~3–5 s totali, non 70–80 s
curl -X POST http://127.0.0.1:1234/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"gemma-4-26b-a4b-it","messages":[{"role":"user","content":"Racconta in circa 150 parole una storia breve."}],"max_tokens":200,"stream":false}'
```

### Throughput atteso (ordine di grandezza, server 5060)

| Modello | tok/s indicativi |
|---------|------------------|
| `qwen/qwen3-vl-8b` | ~47 |
| `google/gemma-3-12b` | ~37 |
| `qwen2.5-coder-14b-instruct` | ~25 |
| `gemma-4-26b-a4b-it` (parallel=1) | **~60–70** |
| `gemma-4-26b-a4b-it` (parallel=4, **bug**) | **~3** |

---

## 10. Troubleshooting rapido

| Sintomo | Causa probabile | Azione |
|---------|-----------------|--------|
| Solo MoE ~3 tok/s, altri OK | `Parallel=4` default | `lms load ... --parallel 1`; `.env` `LMSTUDIO_PARALLEL=1` |
| Tutto lento, disco HDD al 100% | Pagefile su HDD | `scripts\move-pagefile-to-c.ps1` + riavvio |
| Dashboard senza dati | API key non inserita | `?api_key=` nell'URL o incolla nel campo |
| `lmstudio_load_model_failed` | `lms` non trovato / LM Studio spento | Verifica PATH `~\.lmstudio\bin\lms.exe`, server :1234 |
| Context "giusto" ma ancora lento | Modello caricato da GUI con parallel=4 | Unload + reload con CLI o riavvio Localrouter |
| VRAM quasi piena, altro processo | Ollama/altro modello in GPU | `lms unload` / chiudi processi GPU |
| Tunnel OK, locale no | Porta 8088 occupata | `install-server-task.ps1` o kill processo vecchio |

---

## 11. Git e credenziali

Prima del clone su macchina nuova: verificare account GitHub corretto (`gh auth status` / Credential Manager).  
Logout account sbagliato evita push/pull con identità errata.

```powershell
git clone git@github.com:Tecnopozzi/Localrouter.git C:\Localrouter
cd C:\Localrouter
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# compilare .env come §5
```

---

*Ultimo aggiornamento: installazione server 5060, luglio 2026.*
