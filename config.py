"""
Configurazione LocalRouter — Gateway OpenAI-compat + OCR Server.

Unifica tutte le variabili del progetto 10 (Personal OpenRouter) con le
nuove variabili per il servizio OCR basato su glm-ocr via Ollama.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env_str(name, "")
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Autenticazione e rete
# ---------------------------------------------------------------------------

API_KEY: str = _env_str("API_KEY", "")
if 0 < len(API_KEY) < 16:
    raise RuntimeError("API_KEY troppo corta (minimo 16 caratteri): genera una chiave con `python installa.py --rigenera-chiave`")
MAX_BODY_BYTES: int = _env_int("MAX_BODY_BYTES", 160 * 1024 * 1024)
OLLAMA_HOST: str = _env_str("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _default_server_name() -> str:
    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = ""
    return f"LocalRouter-{host}" if host else "LocalRouter"


# Nome identificativo di questa istanza (utile con piu' server su macchine diverse).
# Vuoto = derivato dall'hostname (es. "LocalRouter-studio"). Mostrato in dashboard, /health e /llm/context.
SERVER_NAME: str = _env_str("SERVER_NAME", "") or _default_server_name()

# Descrizione della GPU disponibile su questo PC (testo libero, informativo).
# Mostrata nella dashboard e in /llm/context per orientare un LLM orchestratore.
GPU_INFO: str = _env_str("GPU_INFO", "")

# server = macchina con la GPU; client = tubo verso un server remoto (vedi cliente.py).
# Lo scrive installa.py.
MODO: str = _env_str("LOCALROUTER_MODO", "server").lower()
# Solo client: indirizzo del server remoto e la sua chiave.
SERVER_REMOTO: str = _env_str("LOCALROUTER_SERVER", "").rstrip("/")
CHIAVE_REMOTA: str = _env_str("LOCALROUTER_CHIAVE", "")

# 127.0.0.1 = solo questa macchina (dietro il tunnel); 0.0.0.0 = anche la rete di casa.
HOST: str = _env_str("HOST", "127.0.0.1")
# Host di ascolto del gateway in modo Sinergo: sempre loopback, mai rete/tunnel.
SINERGO_BIND_HOST: str = _env_str("SINERGO_BIND_HOST", "127.0.0.1")
# Opzionale: percorso del .env del LocalRouter gia' acceso (API_KEY/PORT per riuso).
# Se impostato, Sinergo legge chiave e porta da li' invece che dal proprio .env.
SINERGO_GATEWAY_ENV: str = _env_str("SINERGO_GATEWAY_ENV", "")
# Stessa porta del progetto 10 (8088): sostituisce direttamente il vecchio server.
PORT: int = _env_int("PORT", 8088)
# Solo modalita' client: porte locali aggiuntive su cui esporre la stessa app
# compatibile LM Studio/Ollama. Default vuoto: non occupa le porte native 1234/11434.
_client_extra_ports_raw = _env_list("LOCALROUTER_CLIENT_EXTRA_PORTS")
CLIENT_EXTRA_PORTS = []
for _raw_port in _client_extra_ports_raw:
    try:
        _port = int(_raw_port)
    except ValueError:
        continue
    if _port > 0 and _port != PORT and _port not in CLIENT_EXTRA_PORTS:
        CLIENT_EXTRA_PORTS.append(_port)

ALLOW_ORIGINS: list[str] = _env_list("ALLOW_ORIGINS", ["*"])
RATE_LIMIT_PER_MINUTE: int = _env_int("RATE_LIMIT_PER_MINUTE", 120)
OLLAMA_TIMEOUT_S: float = _env_float("OLLAMA_TIMEOUT_S", 600.0)

# Keep-alive Ollama per le chat (mantiene il modello in VRAM tra richieste, riduce i
# ricaricamenti da disco). Esempi: "30m", "1h", "-1" (per sempre). Vuoto = default Ollama (5m).
CHAT_OLLAMA_KEEP_ALIVE: str = _env_str("CHAT_OLLAMA_KEEP_ALIVE", "30m").strip()

# Root del repository git per `git pull` da POST /admin/pull-restart.
# Default: due directory sopra questa cartella (cartella genitore del clone).
_pull_restart_repo = _env_str("PULL_RESTART_REPO_ROOT", "")
GIT_REPO_ROOT: Path = (
    Path(_pull_restart_repo).resolve()
    if _pull_restart_repo
    else (BASE_DIR.parent.parent.resolve())
)

# Token dedicato per POST /admin/pull-restart (git pull + riavvio processo sulla PORT).
# Vuoto = endpoint non registrato. Usa un segreto lungo e distinto da API_KEY se possibile.
PULL_RESTART_HTTP_KEY: str = _env_str("PULL_RESTART_HTTP_KEY", "")
# Secondi di attesa iniziale nel worker prima del pull (consente di chiudere la risposta HTTP).
PULL_RESTART_PREDELAY_S: int = max(0, _env_int("PULL_RESTART_PREDELAY_S", 3))
# Tra due richieste ammesse (0 = nessun cooldown).
PULL_RESTART_COOLDOWN_S: int = max(0, _env_int("PULL_RESTART_COOLDOWN_S", 120))
# Minimo intervallo tra tentativi POST /admin/unload, anche falliti (0 = disabilitato).
ADMIN_UNLOAD_COOLDOWN_S: int = max(0, _env_int("ADMIN_UNLOAD_COOLDOWN_S", 60))

# ---------------------------------------------------------------------------
# LM Studio (opzionale)
# ---------------------------------------------------------------------------

LMSTUDIO_HOST: str = _env_str("LMSTUDIO_HOST", "http://127.0.0.1:1234").rstrip("/")
LMSTUDIO_ENABLED: bool = _env_str("LMSTUDIO_ENABLED", "1").lower() not in (
    "0", "false", "no", "",
)
# Proxy puro verso llama-swap (o altro OpenAI-compat): salta CLI `lms`, API /api/v0,
# ensure_model_context, TTL injection, revive via `lms server`. Il ciclo di vita
# dei modelli lo gestisce lo swap. 0 = comportamento LM Studio classico (via di ritorno).
LMSTUDIO_PROXY_PURE: bool = _env_str("LMSTUDIO_PROXY_PURE", "0").lower() not in (
    "0", "false", "no", "",
)
# Percorso esplicito a `lms.exe` (opzionale). Utile se il gateway non vede PATH utente.
LMSTUDIO_LMS_PATH: str = _env_str("LMSTUDIO_LMS_PATH", "")
# Retry su 502/503 (modello in caricamento): max tentativi e delay iniziale in secondi.
LMSTUDIO_RETRY_MAX: int = _env_int("LMSTUDIO_RETRY_MAX", 18)
LMSTUDIO_RETRY_DELAY_S: float = _env_float("LMSTUDIO_RETRY_DELAY_S", 5.0)
# Retry breve su errori transitori chat (post-generazione, connessione rifiutata, model busy).
LMSTUDIO_CHAT_RETRY_MAX: int = max(1, _env_int("LMSTUDIO_CHAT_RETRY_MAX", 3))
LMSTUDIO_CHAT_RETRY_DELAY_S: float = max(0.1, _env_float("LMSTUDIO_CHAT_RETRY_DELAY_S", 0.4))
# Tetto read chat verso llama-swap/LM Studio. Senza tetto (o con pool
# timeout = read) le connessioni restano appese e saturano il pool httpx.
LMSTUDIO_CHAT_TIMEOUT_S: float = max(30.0, _env_float("LMSTUDIO_CHAT_TIMEOUT_S", 180.0))
# Attesa max per uno slot del pool chat; oltre -> 503 pool_saturated (no hang).
LMSTUDIO_CHAT_POOL_TIMEOUT_S: float = max(0.5, _env_float("LMSTUDIO_CHAT_POOL_TIMEOUT_S", 3.0))
LMSTUDIO_CHAT_MAX_CONNECTIONS: int = max(1, _env_int("LMSTUDIO_CHAT_MAX_CONNECTIONS", 16))
# Idle upstream (secondi senza chunk SSE): chiude TCP verso llama-swap da sola.
# Copre kill -9 / docker stop quando is_disconnected non vede il peer (tunnel CF).
LMSTUDIO_CHAT_IDLE_S: float = max(15.0, _env_float("LMSTUDIO_CHAT_IDLE_S", 90.0))
# Time-to-first-byte max (cold-start modello). Usato come read timeout httpx.
LMSTUDIO_CHAT_TTFB_S: float = max(30.0, _env_float("LMSTUDIO_CHAT_TTFB_S", 150.0))
# Intervallo reaper background (disconnect/idle/max).
LMSTUDIO_CHAT_REAPER_INTERVAL_S: float = max(1.0, _env_float("LMSTUDIO_CHAT_REAPER_INTERVAL_S", 2.0))
# Warmup al boot: invia richiesta dummy per precaricare il modello in VRAM.
LMSTUDIO_WARMUP_ON_STARTUP: bool = _env_str("LMSTUDIO_WARMUP_ON_STARTUP", "1").lower() not in (
    "0", "false", "no", "",
)
# Modello da tenere caldo all'avvio (produzione). Vuoto = primo chat model della lista LMS.
LMSTUDIO_WARMUP_MODEL: str = _env_str("LMSTUDIO_WARMUP_MODEL", "gemma-4-26b-a4b-it")
# Watchdog: se LM Studio/local server non risponde, tenta `lms server start` + reload warmup.
LMSTUDIO_WATCHDOG_ENABLED: bool = _env_str("LMSTUDIO_WATCHDOG_ENABLED", "1").lower() not in (
    "0", "false", "no", "",
)
LMSTUDIO_WATCHDOG_INTERVAL_S: float = max(10.0, _env_float("LMSTUDIO_WATCHDOG_INTERVAL_S", 30.0))
LMSTUDIO_WATCHDOG_FAILS_BEFORE_RESTART: int = max(1, _env_int("LMSTUDIO_WATCHDOG_FAILS_BEFORE_RESTART", 3))
# Contesto (default globale) da configurare via API /api/v0/models/load al boot (0 = non forzare).
LMSTUDIO_CONTEXT_LENGTH: int = _env_int("LMSTUDIO_CONTEXT_LENGTH", 16384)
# GPU offload ratio (default globale) per LM Studio (es: "max", "0.8", "" = non forzare).
LMSTUDIO_GPU_OFFLOAD_RATIO: str = _env_str("LMSTUDIO_GPU_OFFLOAD_RATIO", "max")
# Slot paralleli KV cache (default LM Studio = 4). Con MoE + 16 GB VRAM, 4 slot saturano la
# VRAM e degradano drasticamente le prestazioni; usare 1 per modelli grandi/MoE.
LMSTUDIO_PARALLEL: int = max(1, _env_int("LMSTUDIO_PARALLEL", 1))
# TTL idle (secondi) imposto dal gateway su ogni chat LM Studio (0 = non inviare, default LM Studio).
# 86400 = 24h: il modello JIT resta caldo dall'ultimo uso; unload espliciti (/v1/models/unload*) ignorano il TTL.
LMSTUDIO_TTL_S: int = max(0, _env_int("LMSTUDIO_TTL_S", 86400))

# Override per-modello di context/gpu/parallel. JSON in env LMSTUDIO_MODEL_OVERRIDES.
# Necessario perché modelli diversi entrano in VRAM con config diverse: es. il 26B MoE
# richiede ctx 16384 + gpu max + parallel 1 (16384×1 ≈ stessa VRAM di 8192×2).
# Formato:
#   {"gemma-4-26b-a4b-it":{"context":16384,"gpu":"max","parallel":1},
#    "gemma-4-12b-it-uncensored":{"context":16384,"gpu":"max"}}
# Match sulla chiave: prima uguaglianza esatta (case-insensitive), poi sottostringa.


def _parse_model_overrides(raw: str) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            out[str(key).strip().lower()] = val
    return out


LMSTUDIO_MODEL_OVERRIDES: dict[str, dict[str, Any]] = _parse_model_overrides(
    _env_str("LMSTUDIO_MODEL_OVERRIDES", "")
)

# Capability dichiarate in /v1/models, per modello. JSON in LMSTUDIO_MODEL_CAPABILITIES.
# Senza questa mappa si aggiunge vision da --mmproj (se configurato) e si usa il nome
# per le altre capability (_infer_model_capabilities
# in server.py) e il nome mente: "qwen3-coder-30b" contiene "qwen3" e si prende un
# `thinking` che quel modello non ha. Chi consuma /v1/models decide il routing su questi
# valori, percio' vanno verificati sul campo con scripts/prova-capability-modelli.py.
# Formato: {"qwen/qwen3-coder-30b": ["chat","tools","coding"]}
# Match solo per uguaglianza esatta (case-insensitive): niente sottostringhe, o
# "gemma-4-12b-qat" erediterebbe le capability di "gemma-4-26b-a4b-it".


def _parse_model_capabilities(raw: str) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, val in data.items():
        if isinstance(val, list):
            out[str(key).strip().lower()] = [str(c).strip().lower() for c in val if str(c).strip()]
    return out


LMSTUDIO_MODEL_CAPABILITIES: dict[str, list[str]] = _parse_model_capabilities(
    _env_str("LMSTUDIO_MODEL_CAPABILITIES", "")
)

# Config YAML locale di llama-swap, opzionale; nessun percorso implicito.
LLAMASWAP_CONFIG_PATH: str = _env_str("LLAMASWAP_CONFIG_PATH", "")


def llamaswap_vision_models() -> set[str]:
    """Modelli con --mmproj nel cmd; config assente/illeggibile = nessuna deduzione."""
    if not LLAMASWAP_CONFIG_PATH:
        return set()
    try:
        data = yaml.safe_load(Path(LLAMASWAP_CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return set()
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return set()
    return {
        str(model_id).strip().lower()
        for model_id, entry in models.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("cmd"), str)
        and re.search(r"(?:^|\s)--mmproj(?:\s|=|$)", entry["cmd"])
    }


def lmstudio_model_capabilities(model_id: str) -> list[str] | None:
    """Capability verificate per il modello, o None se non ne sono state dichiarate."""
    target = str(model_id or "").strip().lower()
    if not target:
        return None
    caps = LMSTUDIO_MODEL_CAPABILITIES.get(target)
    return list(caps) if caps is not None else None


def lmstudio_model_config(model_id: str) -> tuple[int, Any, int]:
    """Ritorna (context_length, gpu_offload, parallel) per il modello indicato.

    Applica gli override per-modello da LMSTUDIO_MODEL_OVERRIDES; in assenza di
    match usa i default globali LMSTUDIO_CONTEXT_LENGTH / LMSTUDIO_GPU_OFFLOAD_RATIO /
    LMSTUDIO_PARALLEL.
    """
    context_length: int = LMSTUDIO_CONTEXT_LENGTH
    gpu_offload: Any = LMSTUDIO_GPU_OFFLOAD_RATIO
    parallel: int = LMSTUDIO_PARALLEL

    target = str(model_id or "").strip().lower()
    override: dict[str, Any] | None = LMSTUDIO_MODEL_OVERRIDES.get(target)
    if override is None and target:
        for key, val in LMSTUDIO_MODEL_OVERRIDES.items():
            if key and key in target:
                override = val
                break

    if isinstance(override, dict):
        if override.get("context") is not None:
            try:
                context_length = int(override["context"])
            except (TypeError, ValueError):
                pass
        if override.get("gpu") is not None:
            gpu_offload = override["gpu"]
        if override.get("parallel") is not None:
            try:
                parallel = max(1, int(override["parallel"]))
            except (TypeError, ValueError):
                pass

    return context_length, gpu_offload, parallel

# ---------------------------------------------------------------------------
# Gateway OpenAI-compat (ereditato dal progetto 10)
# ---------------------------------------------------------------------------

MODEL_ALLOWLIST: list[str] = [m.lower() for m in _env_list("MODEL_ALLOWLIST")]
MODELS_CACHE_TTL_S: float = _env_float("MODELS_CACHE_TTL_S", 300.0)
NON_STREAM_HEARTBEAT_INTERVAL_S: float = _env_float("NON_STREAM_HEARTBEAT_INTERVAL_S", 10.0)
CHAT_JOB_MAX_ACTIVE: int = max(1, _env_int("CHAT_JOB_MAX_ACTIVE", 16))
CHAT_JOB_MAX_RETAINED: int = max(CHAT_JOB_MAX_ACTIVE, _env_int("CHAT_JOB_MAX_RETAINED", 100))
MODELS_WARMUP_ON_STARTUP: bool = _env_str("MODELS_WARMUP_ON_STARTUP", "1").lower() not in (
    "0", "false", "no", "",
)

AUTO_NUM_CTX: bool = _env_str("AUTO_NUM_CTX", "1").lower() not in ("0", "false", "no", "")
DEFAULT_NUM_CTX: int = _env_int("DEFAULT_NUM_CTX", 8192)
MAX_NUM_CTX: int = _env_int("MAX_NUM_CTX", 32768)
NUM_CTX_CHARS_PER_TOKEN: float = _env_float("NUM_CTX_CHARS_PER_TOKEN", 3.2)
NUM_CTX_SAFETY_TOKENS: int = _env_int("NUM_CTX_SAFETY_TOKENS", 512)

# ---------------------------------------------------------------------------
# OCR Server (nuovo)
# ---------------------------------------------------------------------------

OCR_MODEL: str = _env_str("OCR_MODEL", "glm-ocr")
# Backend OCR: "lmstudio" | "ollama" | "" (auto: lmstudio se LMSTUDIO_ENABLED, altrimenti ollama).
# La presenza del modello viene verificata a runtime in ocr_pipeline /health.
OCR_BACKEND: str = _env_str("OCR_BACKEND", "").strip().lower()
PDF_DPI: int = _env_int("PDF_DPI", 150)
MAX_PAGES: int = _env_int("MAX_PAGES", 50)
JOB_TTL_S: int = _env_int("JOB_TTL_S", 3600)
OCR_TIMEOUT_S: float = _env_float("OCR_TIMEOUT_S", 300.0)
# Keep-alive Ollama per glm-ocr durante job multi-pagina (es. "30m"). Vuoto = default Ollama.
OCR_OLLAMA_KEEP_ALIVE: str = _env_str("OCR_OLLAMA_KEEP_ALIVE", "").strip()
# Lato lungo massimo immagine in pixel prima del ridimensionamento.
IMAGE_MAX_SIDE: int = _env_int("IMAGE_MAX_SIDE", 4000)

# Coda OCR: job eseguiti in parallelo (default 1 = serializza sulla GPU).
OCR_MAX_CONCURRENT_JOBS: int = max(1, _env_int("OCR_MAX_CONCURRENT_JOBS", 1))
# Max job in coda non ancora avviati; anche 0/negativi danno una coda finita.
OCR_QUEUE_MAX_SIZE: int = max(1, _env_int("OCR_QUEUE_MAX_SIZE", 4))
# Abilita POST /cancel/{id} e POST /cancel-all.
OCR_ALLOW_CANCEL: bool = _env_str("OCR_ALLOW_CANCEL", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Tempo massimo (s) per finire i worker in shutdown applicazione.
OCR_JOB_SHUTDOWN_GRACE_S: float = _env_float("OCR_JOB_SHUTDOWN_GRACE_S", 30.0)

# ---------------------------------------------------------------------------
# Media Enhancement (opzionale — SUPIR upscaling/restauro foto)
# ---------------------------------------------------------------------------

MEDIA_ENABLED: bool = _env_str("MEDIA_ENABLED", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)

_media_venv = _env_str("MEDIA_VENV_PYTHON", "")
MEDIA_VENV_PYTHON: Path = (
    Path(_media_venv).resolve()
    if _media_venv
    else (BASE_DIR / ".venv-media" / "Scripts" / "python.exe")
)

_media_models = _env_str("MEDIA_MODELS_DIR", "models/media")
MEDIA_MODELS_DIR: Path = (
    Path(_media_models).resolve()
    if Path(_media_models).is_absolute()
    else (BASE_DIR / _media_models)
)

_media_temp = _env_str("MEDIA_TEMP_DIR", "data/media_jobs")
MEDIA_TEMP_DIR: Path = (
    Path(_media_temp).resolve()
    if Path(_media_temp).is_absolute()
    else (BASE_DIR / _media_temp)
)

_supir_root = _env_str("MEDIA_SUPIR_ROOT", "vendor/SUPIR")
MEDIA_SUPIR_ROOT: Path = (
    Path(_supir_root).resolve()
    if Path(_supir_root).is_absolute()
    else (BASE_DIR / _supir_root)
)

_supir_yaml = _env_str("MEDIA_SUPIR_YAML", "options/SUPIR_v0_12gb.yaml")
MEDIA_SUPIR_YAML: Path = (
    Path(_supir_yaml).resolve()
    if Path(_supir_yaml).is_absolute()
    else (MEDIA_SUPIR_ROOT / _supir_yaml)
)

MEDIA_DEFAULT_UPSCALE: int = max(2, min(4, _env_int("MEDIA_DEFAULT_UPSCALE", 2)))
if MEDIA_DEFAULT_UPSCALE not in (2, 4):
    MEDIA_DEFAULT_UPSCALE = 2

MEDIA_DEFAULT_FIDELITY: float = max(0.0, min(1.0, _env_float("MEDIA_DEFAULT_FIDELITY", 0.85)))
MEDIA_MAX_CONCURRENT_JOBS: int = max(1, _env_int("MEDIA_MAX_CONCURRENT_JOBS", 1))
MEDIA_QUEUE_MAX_SIZE: int = max(1, _env_int("MEDIA_QUEUE_MAX_SIZE", 4))
MEDIA_JOB_TTL_S: int = _env_int("MEDIA_JOB_TTL_S", 3600)
MEDIA_ALLOW_CANCEL: bool = _env_str("MEDIA_ALLOW_CANCEL", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
MEDIA_JOB_SHUTDOWN_GRACE_S: float = _env_float("MEDIA_JOB_SHUTDOWN_GRACE_S", 60.0)
MEDIA_RESULT_FORMAT: str = _env_str("MEDIA_RESULT_FORMAT", "auto").lower()
MEDIA_INFERENCE_TIMEOUT_S: float = _env_float("MEDIA_INFERENCE_TIMEOUT_S", 1800.0)
MEDIA_MAX_INPUT_BYTES: int = max(1, _env_int("MEDIA_MAX_INPUT_BYTES", 25 * 1024 * 1024))
MEDIA_MAX_INPUT_SIDE: int = max(256, _env_int("MEDIA_MAX_INPUT_SIDE", 2048))
MEDIA_MAX_OUTPUT_SIDE: int = max(512, _env_int("MEDIA_MAX_OUTPUT_SIDE", 4096))
MEDIA_SUPIR_EDM_STEPS: int = max(10, _env_int("MEDIA_SUPIR_EDM_STEPS", 50))
MEDIA_SUPIR_NO_LLAVA: bool = _env_str("MEDIA_SUPIR_NO_LLAVA", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
MEDIA_SUPIR_USE_TILE_VAE: bool = _env_str("MEDIA_SUPIR_USE_TILE_VAE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
MEDIA_SUPIR_HALF_PARAMS: bool = _env_str("MEDIA_SUPIR_HALF_PARAMS", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)

# ---------------------------------------------------------------------------
# Whisper (trascrizione audio — opzionale, venv dedicato .venv-whisper)
# ---------------------------------------------------------------------------

WHISPER_ENABLED: bool = _env_str("WHISPER_ENABLED", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)

_whisper_venv = _env_str("WHISPER_VENV_PYTHON", "")
WHISPER_VENV_PYTHON: Path = (
    Path(_whisper_venv).resolve()
    if _whisper_venv
    else (BASE_DIR / ".venv-whisper" / "Scripts" / "python.exe")
)

_whisper_models = _env_str("WHISPER_MODELS_DIR", "models/whisper")
WHISPER_MODELS_DIR: Path = (
    Path(_whisper_models).resolve()
    if Path(_whisper_models).is_absolute()
    else (BASE_DIR / _whisper_models)
)

_whisper_temp = _env_str("WHISPER_TEMP_DIR", "data/audio_jobs")
WHISPER_TEMP_DIR: Path = (
    Path(_whisper_temp).resolve()
    if Path(_whisper_temp).is_absolute()
    else (BASE_DIR / _whisper_temp)
)

# Modello faster-whisper: tiny|base|small|medium|large-v3 (o path locale).
WHISPER_MODEL: str = _env_str("WHISPER_MODEL", "large-v3")
# Device inferenza: cpu | cuda | auto. Su GPU 3060 12GB: cuda + float16 (dopo unload LLM).
WHISPER_DEVICE: str = _env_str("WHISPER_DEVICE", "cuda").lower()
# compute_type: int8 (cpu) | int8_float16 | float16 (cuda) | float32.
WHISPER_COMPUTE_TYPE: str = _env_str("WHISPER_COMPUTE_TYPE", "float16")
# Lingua di default (ISO 639-1, es. "it"); vuoto = autodetect.
WHISPER_DEFAULT_LANGUAGE: str = _env_str("WHISPER_DEFAULT_LANGUAGE", "")
WHISPER_BEAM_SIZE: int = max(1, _env_int("WHISPER_BEAM_SIZE", 5))
WHISPER_VAD_FILTER: bool = _env_str("WHISPER_VAD_FILTER", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Prima di trascrivere su cuda, scarica modelli LM Studio dalla VRAM (3060 12GB).
WHISPER_AUTO_UNLOAD_LLMS: bool = _env_str("WHISPER_AUTO_UNLOAD_LLMS", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Timeout download/prefetch modello (large-v3 ~3 GB).
WHISPER_DOWNLOAD_TIMEOUT_S: float = _env_float("WHISPER_DOWNLOAD_TIMEOUT_S", 3600.0)
# Job di trascrizione in parallelo (default 1 = serializza; audio e' CPU/GPU-bound).
WHISPER_MAX_CONCURRENT_JOBS: int = max(1, _env_int("WHISPER_MAX_CONCURRENT_JOBS", 1))
# Timeout inferenza per file (secondi). Include load modello (~1-3s) + trascrizione.
WHISPER_TIMEOUT_S: float = _env_float("WHISPER_TIMEOUT_S", 1800.0)
# Dimensione massima upload audio (bytes). Default 100 MB.
WHISPER_MAX_INPUT_BYTES: int = max(1, _env_int("WHISPER_MAX_INPUT_BYTES", 100 * 1024 * 1024))

# ---------------------------------------------------------------------------
# Voce esterna (speaches OpenAI-compat) — se valorizzato, /v1/audio/* va li'
# ---------------------------------------------------------------------------

# Base URL speaches locale (es. http://127.0.0.1:8201). Vuoto = comportamento
# storico: whisper interno se WHISPER_ENABLED, altrimenti 404; /speech assente.
VOCE_URL: str = _env_str("VOCE_URL", "").rstrip("/")
# Nome modello STT in speaches per faster-whisper small (HF id).
VOCE_STT_MODEL_DEFAULT: str = "Systran/faster-whisper-small"
# TTS Kokoro ONNX + voce italiana; wav perche' sul 5060 non c'e' FFmpeg.
VOCE_TTS_MODEL_DEFAULT: str = "speaches-ai/Kokoro-82M-v1.0-ONNX-int8"  # id installato sul 5060 (27/09: senza -int8 → 404)
VOCE_TTS_VOICE_DEFAULT: str = "if_sara"
VOCE_TTS_FORMAT_DEFAULT: str = "wav"
VOCE_TIMEOUT_S: float = 180.0

# ---------------------------------------------------------------------------
# Decisori (Laya + Decision-4B) — un solo upstream, prefisso decider:
# ---------------------------------------------------------------------------
# Base URL del server FastAPI decisori (es. http://127.0.0.1:8098). Vuoto = off.
DECIDER_URL: str = _env_str("DECIDER_URL", "").rstrip("/")
# Decision-4B su CPU con stato lungo puo' superare i 30 s; coda seriale sul
# server decisori + tunnel CF ~100 s → default 180 (override con env).
DECIDER_TIMEOUT_S: float = _env_float("DECIDER_TIMEOUT_S", 180.0)
# Chiave inoltrata all'upstream (Bearer). Vuota = nessuna Authorization.
DECIDER_API_KEY: str = _env_str("DECIDER_API_KEY", "")
# Id esposti in GET /v1/models (prefisso decider: aggiunto dal gateway).
DECIDER_MODELS: list[str] = _env_list("DECIDER_MODELS", ["laya", "decision-4b"])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Rotazione: oltre questa soglia (MB) il log viene rinominato con la data e compresso; si tengono gli ultimi N.
REQUESTS_LOG_MAX_MB: int = _env_int("REQUESTS_LOG_MAX_MB", 200)
REQUESTS_LOG_KEEP: int = _env_int("REQUESTS_LOG_KEEP", 10)
REQUESTS_LOG_PATH_RAW: str = _env_str("REQUESTS_LOG_PATH", "logs/requests.jsonl")
REQUESTS_LOG_PATH: Path | None
if REQUESTS_LOG_PATH_RAW:
    path_candidate = Path(REQUESTS_LOG_PATH_RAW)
    if not path_candidate.is_absolute():
        path_candidate = BASE_DIR / path_candidate
    path_candidate.parent.mkdir(parents=True, exist_ok=True)
    REQUESTS_LOG_PATH = path_candidate
else:
    REQUESTS_LOG_PATH = None


def is_model_allowed(model_name: str) -> bool:
    """True se il modello puo' essere esposto via /v1/models."""

    if not MODEL_ALLOWLIST:
        return True
    lowered = str(model_name or "").strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in MODEL_ALLOWLIST)
