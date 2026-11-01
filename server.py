"""
LocalRouter — Gateway OpenAI-compat + OCR Server unificato.

Endpoint ereditati dal progetto 10 (Personal OpenRouter):
  GET  /              info
  GET  /health        salute Ollama + glm-ocr
  GET  /v1/models     lista modelli (filtrati da MODEL_ALLOWLIST)
  POST /v1/chat/completions  chat completion (streaming + tools)

Endpoint nuovi OCR (compatibili con 09B_chat_rag_manuale):
  POST /process       invia file (PDF/immagine) -> restituisce job_id
  GET  /status/{job_id}  stato del job OCR
  GET  /result/{job_id}  risultato JSON con blocks

Endpoint Media Enhancement (opzionale, MEDIA_ENABLED=1):
  POST /media/process       upload immagine -> job_id (SUPIR-v0F x2)
  GET  /media/status/{job_id}
  GET  /media/result/{job_id}   PNG o JSON base64

Endpoint Whisper (trascrizione audio, opzionale, WHISPER_ENABLED=1):
  POST /v1/audio/transcriptions   OpenAI-compat (file -> testo)
  POST /v1/audio/translations     OpenAI-compat (audio -> inglese)
  POST /audio/transcribe          nativo (verbose_json con segmenti+timing)

Endpoint Voce (se VOCE_URL punta a speaches locale):
  POST /v1/audio/transcriptions   inoltro multipart a VOCE_URL
  POST /v1/audio/translations     inoltro multipart a VOCE_URL
  POST /v1/audio/speech           TTS OpenAI-compat (JSON -> audio)

Endpoint Decisori (se DECIDER_URL punta al server FastAPI locale):
  POST /v1/decide                 inoltro a DECIDER_URL/v1/decide (Laya, Decision-4B)
  GET  /v1/models                 espone anche id decider:laya, decider:decision-4b

Opzionale (se PULL_RESTART_HTTP_KEY in .env):
  POST /admin/pull-restart  git pull main + riavvio server (token dedicato, 202 Accepted)

Cloudflare Tunnel: questo server e' pensato per girare su :8088 (stesso del
progetto 10) con tunnel che punta a questa porta.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import gzip
import json
import logging
import math
import os
import platform
import re
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import config
from auth import ROTTE_APERTE, chiave_valida, require_api_key, require_health_rate_limit, require_pull_restart_token, troppi_fallimenti
from job_store import get_store
from ocr_pipeline import ALLOWED_MIMES
from ocr_scheduler import OcrWorkItem, get_scheduler
from ollama_client import (
    OllamaError,
    chat,
    chat_stream,
    close_client,
    list_tags,
    show_model,
)
import lmstudio_client
from lmstudio_client import LMStudioError
from pull_restart_runner import schedule_pull_restart
from schemas import (
    ChatCompletionRequest,
    ModelEntry,
    ModelsResponse,
    KvPrefissoRequest,
    KvSlotRequest,
    ChatJobAcceptedResponse,
    ChatJobResultResponse,
    ChatJobStatusResponse,
    OcrBlock,
    OcrCancelAllResponse,
    OcrCancelResponse,
    OcrJobResponse,
    OcrJobsListResponse,
    OcrProgress,
    OcrResultResponse,
    OcrSource,
    OcrStatusResponse,
    PullRestartAcceptedResponse,
    MediaCancelAllResponse,
    MediaCancelResponse,
    MediaJobResponse,
    MediaJobsListResponse,
    MediaResultJsonResponse,
    MediaStatusResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ocr_server")

_PULL_RESTART_LAST_MONO: float = 0.0
_PULL_RESTART_CD_LOCK = asyncio.Lock()
_ADMIN_UNLOAD_LAST_MONO: float | None = None
_SERVER_STARTED_AT_MONO: float = time.monotonic()

try:  # opzionale: metriche piu' ricche se disponibile nell'ambiente
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None


# ---------------------------------------------------------------------------
# In-memory chat async jobs
# ---------------------------------------------------------------------------

_CHAT_JOBS: dict[str, dict[str, Any]] = {}
_CHAT_JOBS_LOCK = asyncio.Lock()
_CHAT_JOB_EVENTS: dict[str, asyncio.Queue[dict[str, Any]]] = {}


def _now_ts() -> int:
    return int(time.time())


def _run_cmd(args: list[str], timeout_s: float = 1.2) -> tuple[bool, str]:
    try:
        out = subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=timeout_s, text=True)
        return True, out.strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _linux_memory_snapshot() -> Optional[dict[str, Any]]:
    if not sys.platform.startswith("linux"):
        return None
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                parts = rest.strip().split()
                if not parts:
                    continue
                meminfo[key] = int(parts[0]) * 1024  # kB -> bytes
        total = int(meminfo.get("MemTotal", 0))
        avail = int(meminfo.get("MemAvailable", 0))
        if total <= 0 or avail < 0:
            return None
        used = max(0, total - avail)
        used_pct = round((used / total) * 100.0, 2) if total else None
        return {
            "total_bytes": total,
            "available_bytes": avail,
            "used_bytes": used,
            "used_pct": used_pct,
            "source": "procfs",
        }
    except Exception:  # noqa: BLE001
        return None


def _system_memory_snapshot() -> dict[str, Any]:
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return {
                "total_bytes": int(vm.total),
                "available_bytes": int(vm.available),
                "used_bytes": int(vm.used),
                "used_pct": round(float(vm.percent), 2),
                "source": "psutil",
            }
        except Exception:  # noqa: BLE001
            pass
    linux = _linux_memory_snapshot()
    if linux is not None:
        return linux
    return {
        "total_bytes": None,
        "available_bytes": None,
        "used_bytes": None,
        "used_pct": None,
        "source": "unavailable",
    }


def _cpu_snapshot() -> dict[str, Any]:
    logical = os.cpu_count()
    loadavg = None
    if hasattr(os, "getloadavg"):
        try:
            la = os.getloadavg()
            loadavg = [round(float(la[0]), 2), round(float(la[1]), 2), round(float(la[2]), 2)]
        except Exception:  # noqa: BLE001
            loadavg = None
    cpu_pct = None
    if psutil is not None:
        try:
            cpu_pct = round(float(psutil.cpu_percent(interval=0.0)), 2)
        except Exception:  # noqa: BLE001
            cpu_pct = None
    return {
        "logical_cores": logical,
        "loadavg_1_5_15": loadavg,
        "cpu_percent": cpu_pct,
    }


def _gpu_snapshot() -> dict[str, Any]:
    query = (
        "--query-gpu=timestamp,name,utilization.gpu,utilization.memory,"
        "memory.used,memory.total,temperature.gpu,power.draw"
    )
    ok, out = _run_cmd(["nvidia-smi", query, "--format=csv,noheader,nounits"], timeout_s=1.0)
    if not ok:
        return {
            "available": False,
            "error": out,
            "gpus": [],
        }
    gpus: list[dict[str, Any]] = []
    for line in [ln.strip() for ln in out.splitlines() if ln.strip()]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        try:
            mem_used = int(float(parts[4]))
            mem_total = int(float(parts[5]))
            mem_pct = round((mem_used / mem_total) * 100.0, 2) if mem_total > 0 else None
            gpus.append(
                {
                    "timestamp": parts[0],
                    "name": parts[1],
                    "util_gpu_pct": float(parts[2]),
                    "util_mem_pct": float(parts[3]),
                    "vram_used_mb": mem_used,
                    "vram_total_mb": mem_total,
                    "vram_used_pct": mem_pct,
                    "temperature_c": float(parts[6]),
                    "power_w": float(parts[7]),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return {
        "available": len(gpus) > 0,
        "gpus": gpus,
        "error": None if gpus else "parse_failed_or_no_devices",
    }


def _process_snapshot() -> dict[str, Any]:
    uptime_s = round(time.monotonic() - _SERVER_STARTED_AT_MONO, 3)
    base: dict[str, Any] = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "python": sys.executable,
        "uptime_s": uptime_s,
        "cpu_percent": None,
        "rss_bytes": None,
        "vms_bytes": None,
        "threads": None,
    }
    if psutil is None:
        base["source"] = "stdlib"
        return base
    try:
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        base.update(
            {
                "cpu_percent": round(float(proc.cpu_percent(interval=0.0)), 2),
                "rss_bytes": int(mem.rss),
                "vms_bytes": int(mem.vms),
                "threads": int(proc.num_threads()),
                "source": "psutil",
            }
        )
    except Exception:  # noqa: BLE001
        base["source"] = "psutil_error"
    return base


async def _chat_job_emit(job_id: str, event: str, data: dict[str, Any]) -> None:
    q = _CHAT_JOB_EVENTS.get(job_id)
    if q is None:
        return
    await q.put({"event": event, "data": data, "ts": _now_ts()})


async def _chat_job_update(job_id: str, **fields: Any) -> dict[str, Any] | None:
    async with _CHAT_JOBS_LOCK:
        job = _CHAT_JOBS.get(job_id)
        if not job:
            return None
        job.update(fields)
        job["updated_at"] = _now_ts()
        return dict(job)


async def _run_chat_nonstream(req: ChatCompletionRequest, ip: str) -> tuple[dict[str, Any], Optional[str]]:
    # LM Studio branch
    if _is_lmstudio_model(req.model):
        real_model = _strip_lmstudio_prefix(req.model)
        if lmstudio_client.is_lmstudio_runtime_unsupported(real_model):
            raise HTTPException(
                status_code=501,
                detail=lmstudio_client.lmstudio_unsupported_detail(real_model),
            )
        _rifiuta_grammar_su_lmstudio_nativo(req)
        lms_body: dict[str, Any] = {
            "model": real_model,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": False,
        }
        if req.temperature is not None:
            lms_body["temperature"] = float(req.temperature)
        if req.max_tokens is not None:
            lms_body["max_tokens"] = int(req.max_tokens)
        if req.top_p is not None:
            lms_body["top_p"] = float(req.top_p)
        if req.stop is not None:
            lms_body["stop"] = req.stop
        if req.tools:
            lms_body["tools"] = req.tools
        if req.response_format:
            lms_body["response_format"] = req.response_format
        if req.seed is not None:
            lms_body["seed"] = int(req.seed)
        if req.chat_template_kwargs is not None:
            lms_body["chat_template_kwargs"] = req.chat_template_kwargs
        _apply_thinking_budget(lms_body, req)
        _apply_parametri_llama(lms_body, req)

        logger.info(
            "chat_async_lmstudio | ip=%s | model=%s | msgs=%d | thinking_budget=%s",
            ip, real_model, len(lms_body["messages"]),
            lms_body.get("thinking_budget_tokens"),
        )
        if not await lmstudio_client.ensure_model_context(real_model):
            err = lmstudio_client.get_last_load_error() or "unknown"
            raise HTTPException(
                status_code=502,
                detail=f"LM Studio reload failed for model {real_model}: {err}",
            )
        retry_meta: list[dict[str, Any]] = []
        try:
            data = await _lmstudio_chat_nonstream_call(lms_body, req.model, retry_meta)
        except LMStudioError:
            raise
        req_id = str(data.get("id") or "") or None
        return data, req_id

    # Ollama branch
    body = _build_ollama_body(req)
    logger.info(
        "chat_async_ollama | ip=%s | model=%s | msgs=%d | tools=%d",
        ip, req.model, len(body.get("messages", [])), len(body.get("tools") or []),
    )
    data = await chat(body)
    completion = _build_chat_completion(data, req.model)
    req_id = str(completion.get("id") or "") or None
    return completion, req_id


async def _chat_job_worker(job_id: str, req: ChatCompletionRequest, ip: str) -> None:
    await _chat_job_update(job_id, status="running", started_at=_now_ts())
    await _chat_job_emit(job_id, "running", {"job_id": job_id})
    try:
        result, req_id = await _run_chat_nonstream(req, ip)
        content = ""
        try:
            choices = result.get("choices") or []
            content = str(((choices[0].get("message") or {}).get("content") or "")) if choices else ""
        except Exception:
            content = ""
        await _chat_job_update(
            job_id,
            status="completed",
            finished_at=_now_ts(),
            result=result,
            request_id=req_id,
            partial_text_len=len(content),
            error=None,
        )
        await _chat_job_emit(job_id, "completed", {"job_id": job_id, "request_id": req_id, "partial_text_len": len(content)})
    except asyncio.CancelledError:
        await _chat_job_update(job_id, status="cancelled", finished_at=_now_ts(), error="cancelled")
        await _chat_job_emit(job_id, "cancelled", {"job_id": job_id})
        raise
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        await _chat_job_update(
            job_id,
            status="failed",
            finished_at=_now_ts(),
            error=err,
        )
        await _chat_job_emit(job_id, "failed", {"job_id": job_id, "error": err})


class _InMemoryLogHandler(logging.Handler):
    """Ring buffer degli ultimi 500 log record — accessibile via /debug/logs."""

    MAX_RECORDS = 500

    def __init__(self) -> None:
        super().__init__()
        self._records: collections.deque[dict[str, str]] = collections.deque(maxlen=self.MAX_RECORDS)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._records.append({
                "ts": self.formatTime(record),
                "level": record.levelname,
                "msg": record.getMessage(),
            })
        except Exception:  # noqa: BLE001
            pass

    def get_records(self, n: int = 100) -> list[dict[str, str]]:
        records = list(self._records)
        return records[-n:] if n < len(records) else records


_mem_handler = _InMemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter("%(asctime)s"))
logger.addHandler(_mem_handler)


# ---------------------------------------------------------------------------
# Cache modelli (dal progetto 10)
# ---------------------------------------------------------------------------

_MODELS_CACHE: dict[str, Any] = {
    "at": 0.0,
    "data": [],
    "lmstudio_ok": False,
    "lmstudio_error": None,
    "ollama_ok": False,
}
_MODELS_BUILD_LOCK = asyncio.Lock()
# TTL corto dopo fallimento LM Studio: ritenta il riaggancio senza martellare.
_MODELS_CACHE_FAIL_TTL_S = 15.0


async def _warmup_models_cache() -> None:
    try:
        started = time.monotonic()
        data = await _get_models_cached()
        logger.info(
            "models_warmup_ok | models=%d | elapsed_ms=%d",
            len(data),
            int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.warning("models_warmup_failed | err=%s", exc)


async def _warmup_lmstudio() -> None:
    """Precaricare in VRAM il modello di produzione (o il primo chat model LMS)."""
    try:
        if not await lmstudio_client.ensure_server_running():
            logger.warning("lmstudio_warmup_skip | local server unreachable")
            return
        preferred = (config.LMSTUDIO_WARMUP_MODEL or "").strip()
        model_id = preferred
        if not model_id:
            models = await lmstudio_client.list_models()
            chat_models = [
                m for m in models
                if "embed" not in str(m.get("id") or "").lower()
            ]
            if not chat_models:
                logger.info("lmstudio_warmup_skip | nessun modello chat trovato")
                return
            model_id = str(chat_models[0].get("id") or "")
        if model_id:
            # In proxy puro (llama-swap) non c'e' CLI lms: basta una chat dummy.
            if not config.LMSTUDIO_PROXY_PURE:
                await lmstudio_client.load_model_with_context(model_id)
            await lmstudio_client.warmup(model_id)
    except Exception as exc:
        logger.warning("lmstudio_warmup_skip | err=%s", exc)


async def _lmstudio_watchdog_loop() -> None:
    """Se LM Studio smette di rispondere, riavvia il local server e ricarica il modello produzione."""
    fails = 0
    interval = float(config.LMSTUDIO_WATCHDOG_INTERVAL_S)
    threshold = int(config.LMSTUDIO_WATCHDOG_FAILS_BEFORE_RESTART)
    logger.info(
        "lmstudio_watchdog_start | interval=%.0fs | fails_before_restart=%d | warmup=%s",
        interval, threshold, config.LMSTUDIO_WARMUP_MODEL,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            if await lmstudio_client.probe_server():
                if fails:
                    logger.info("lmstudio_watchdog_recovered | previous_fails=%d", fails)
                fails = 0
                continue
            fails += 1
            logger.warning(
                "lmstudio_watchdog_probe_fail | fails=%d/%d",
                fails, threshold,
            )
            if fails < threshold:
                continue
            logger.error("lmstudio_watchdog_revive | starting revive_runtime")
            ok = await lmstudio_client.revive_runtime(load_warmup_model=True)
            fails = 0 if ok else fails
            logger.info("lmstudio_watchdog_revive_done | ok=%s", ok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("lmstudio_watchdog_error | %s", exc)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    sched = get_scheduler()
    sched.start()
    media_sched = None
    watchdog_task: asyncio.Task | None = None
    if config.MEDIA_ENABLED:
        from media.media_scheduler import get_media_scheduler

        media_sched = get_media_scheduler()
        media_sched.start()
    if config.PULL_RESTART_HTTP_KEY:
        git_mark = config.GIT_REPO_ROOT / ".git"
        if not git_mark.exists():
            logger.warning(
                "pull_restart_misconfigured | GIT_REPO_ROOT=%s (nessun .git)",
                config.GIT_REPO_ROOT,
            )
    if config.MODELS_WARMUP_ON_STARTUP:
        asyncio.create_task(_warmup_models_cache())
    if config.LMSTUDIO_ENABLED and config.LMSTUDIO_WARMUP_ON_STARTUP:
        asyncio.create_task(_warmup_lmstudio())
    if config.LMSTUDIO_ENABLED and config.LMSTUDIO_WATCHDOG_ENABLED:
        watchdog_task = asyncio.create_task(_lmstudio_watchdog_loop())
    if config.LMSTUDIO_ENABLED:
        lmstudio_client.start_chat_reaper()
    try:
        yield
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
        await lmstudio_client.stop_chat_reaper()
        await sched.shutdown()
        if media_sched is not None:
            await media_sched.shutdown()
        await close_client()
        if config.LMSTUDIO_ENABLED:
            await lmstudio_client.close_client()


app = FastAPI(
    title="LocalRouter",
    version="1.0.0",
    description=(
        "Gateway OpenAI-compatible verso Ollama locale (eredita il progetto 10) "
        "piu' server OCR compatibile con 09B_chat_rag_manuale."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOW_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class LimiteCorpo:
    """ASGI: 413 se il corpo supera `massimo` byte (Content-Length o flusso reale)."""

    def __init__(self, app, massimo: int) -> None:
        self.app = app
        self.massimo = massimo

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        dichiarato = dict(scope["headers"]).get(b"content-length", b"")
        if dichiarato.isdigit() and int(dichiarato) > self.massimo:
            return await JSONResponse({"detail": "Body too large"}, status_code=413)(scope, receive, send)
        letti = 0

        async def contato():
            nonlocal letti
            msg = await receive()
            if msg["type"] == "http.request":
                letti += len(msg.get("body", b""))
                if letti > self.massimo:
                    return {"type": "http.disconnect"}
            return msg

        await self.app(scope, contato, send)


app.add_middleware(LimiteCorpo, massimo=config.MAX_BODY_BYTES)


@app.middleware("http")
async def chiave_ovunque(request: Request, call_next):
    """La chiave vale per ogni route, anche da 127.0.0.1, salvo ROTTE_APERTE."""
    if request.method == "OPTIONS" or request.url.path in ROTTE_APERTE or chiave_valida(request):
        return await call_next(request)
    return JSONResponse(
        status_code=429 if troppi_fallimenti() else 401,
        content={"detail": "Invalid or missing API key"},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def audit_logging_middleware(request: Request, call_next):
    _audit_init(request)
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        _audit_finalize(request, getattr(response, "status_code", None))


@app.exception_handler(OllamaError)
async def _ollama_error_handler(_request: Request, exc: OllamaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc), "type": "upstream_error"}},
    )


def _lmstudio_error_audit(exc: LMStudioError) -> dict[str, Any]:
    return {
        "message": str(exc),
        "gateway_status": exc.status_code,
        "upstream_status": exc.upstream_status,
        "error_kind": exc.error_kind or "upstream",
        "detail": (exc.detail or str(exc))[:500],
    }


@app.exception_handler(LMStudioError)
async def _lmstudio_error_handler(request: Request, exc: LMStudioError) -> JSONResponse:
    _audit_update(
        request,
        lmstudio_retries=getattr(request.state, "lmstudio_retries", []),
        lmstudio_error=_lmstudio_error_audit(exc),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": str(exc),
                "type": exc.error_kind or "upstream_error",
            }
        },
    )


_TAIL_BLOCK = 1 << 20
_rotazione_lock = threading.Lock()


def _comprimi_e_pota(path: Path) -> None:
    """Comprime i log ruotati (requests-<data>.jsonl -> .gz) e tiene gli ultimi N. Ogni errore = si riprova alla prossima rotazione."""
    for src in sorted(path.parent.glob(f"{path.stem}-*{path.suffix}")):
        gz = src.with_name(src.name + ".gz")
        tmp = src.with_name(src.name + ".gz.tmp")
        try:
            with src.open("rb") as fi, gzip.open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.replace(tmp, gz)
            src.unlink()
        except OSError as exc:
            logger.warning("requests_log_compress_failed | file=%s err=%s", src.name, exc)
    for vecchio in sorted(path.parent.glob(f"{path.stem}-*{path.suffix}.gz"))[: -max(config.REQUESTS_LOG_KEEP, 1)]:
        try:
            vecchio.unlink()
        except OSError as exc:
            logger.warning("requests_log_prune_failed | file=%s err=%s", vecchio.name, exc)


def _ruota_log(path: Path) -> None:
    """Oltre la soglia rinomina il log con la data e comprime in un thread. Rinomina fallita (Windows: file aperto da
    un altro processo) = si scrive ancora sul file corrente e si riprova alla scrittura dopo: nessuna riga persa."""
    try:
        if path.stat().st_size < config.REQUESTS_LOG_MAX_MB * 1024 * 1024:
            return
    except OSError:
        return
    with _rotazione_lock:
        dest = path.with_name(f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        try:
            if path.stat().st_size < config.REQUESTS_LOG_MAX_MB * 1024 * 1024 or dest.exists():
                return
            path.rename(dest)
        except OSError as exc:
            logger.warning("requests_log_rotate_failed | err=%s", exc)
            return
    threading.Thread(target=_comprimi_e_pota, args=(path,), daemon=True).start()


def _log_request(entry: dict[str, Any]) -> None:
    if config.REQUESTS_LOG_PATH is None:
        return
    try:
        _ruota_log(config.REQUESTS_LOG_PATH)
        with config.REQUESTS_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("requests_log_failed | err=%s", exc)


def _iso_utc(ts: float | None = None) -> str:
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_json(value: Any, max_chars: int = 4000) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"
    if isinstance(value, dict):
        return {str(k): _safe_json(v, max_chars=max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v, max_chars=max_chars) for v in value]
    return _safe_json(str(value), max_chars=max_chars)


def _request_headers_for_audit(request: Request) -> dict[str, Any]:
    wanted = (
        "user-agent",
        "x-forwarded-for",
        "x-real-ip",
        "x-client-name",
        "x-client-id",
        "x-client-version",
    )
    out: dict[str, Any] = {}
    for name in wanted:
        value = request.headers.get(name)
        if value:
            out[name] = value
    return out


def _client_identity_for_audit(request: Request) -> dict[str, Any]:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    forwarded_ip = forwarded.split(",")[0].strip() if forwarded else None
    client = request.client
    direct_ip = client.host if client else None
    app_name = request.headers.get("x-client-name") or None
    app_id = request.headers.get("x-client-id") or None
    app_version = request.headers.get("x-client-version") or None
    user_agent = request.headers.get("user-agent") or None
    resolved_name = app_name or user_agent or "legacy-client"
    return {
        "ip": forwarded_ip or direct_ip or "unknown",
        "forwarded_for": forwarded or None,
        "direct_ip": direct_ip,
        "application": {
            "name": app_name,
            "id": app_id,
            "version": app_version,
            "user_agent": user_agent,
            "resolved_name": resolved_name,
            "declared": bool(app_name or app_id or app_version),
        },
    }


def _audit_init(request: Request) -> None:
    request.state.audit_entry = {
        "request_id": f"req-{uuid.uuid4().hex}",
        "method": request.method,
        "path": request.url.path,
        "query": {k: ("***" if k.lower() in ("api_key", "key", "token") else v) for k, v in request.query_params.items()},
        "started_at": _iso_utc(),
        "client": _client_identity_for_audit(request),
        "headers": _request_headers_for_audit(request),
        "request_items": [],
    }
    request.state.audit_started_mono = time.monotonic()


def _audit_update(request: Request, **fields: Any) -> None:
    entry = getattr(request.state, "audit_entry", None)
    if not isinstance(entry, dict):
        return
    for key, value in fields.items():
        if key == "request_items" and value is not None:
            entry[key] = _safe_json(value)
        elif key in {"client", "headers", "request_payload", "response_payload", "route"} and isinstance(value, dict):
            current = entry.setdefault(key, {})
            if isinstance(current, dict):
                current.update(_safe_json(value))
            else:
                entry[key] = _safe_json(value)
        else:
            entry[key] = _safe_json(value)


def _audit_finalize(request: Request, status_code: int | None) -> None:
    entry = getattr(request.state, "audit_entry", None)
    if not isinstance(entry, dict):
        return
    started_mono = getattr(request.state, "audit_started_mono", None)
    duration_ms = None
    if isinstance(started_mono, (int, float)):
        duration_ms = int((time.monotonic() - started_mono) * 1000)
    entry["finished_at"] = _iso_utc()
    entry["duration_ms"] = duration_ms
    entry["status_code"] = status_code
    route = entry.get("route") if isinstance(entry.get("route"), dict) else {}
    if entry.get("path") == "/v1/chat/completions" and route.get("backend") == "lmstudio":
        if "lmstudio_retries" not in entry:
            entry["lmstudio_retries"] = _safe_json(
                getattr(request.state, "lmstudio_retries", [])
            )
    _log_request(entry)


def _chat_request_items(req: ChatCompletionRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for idx, msg in enumerate(req.messages):
        items.append({
            "type": "message",
            "index": idx,
            "value": msg.model_dump(exclude_none=True),
        })
    if req.tools:
        items.append({"type": "tools", "value": req.tools})
    if req.response_format:
        items.append({"type": "response_format", "value": req.response_format})
    return items


def _form_request_items(fields: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "field", "name": key, "value": value} for key, value in fields.items()]


def _tail_jsonl(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    # Solo la coda del file, a blocchi dalla fine: memoria proporzionale a `limit` righe, non alla dimensione del log.
    # errors="replace": una riga tagliata a meta' carattere (scrittura in corso) dava UnicodeDecodeError -> HTTP 500.
    blocchi: list[bytes] = []
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        newline = 0
        while pos > 0 and newline <= limit:  # limit+1 a capo = limit righe intere (la prima potrebbe essere tagliata)
            n = min(_TAIL_BLOCK, pos)
            pos -= n
            f.seek(pos)
            blocchi.append(f.read(n))
            newline += blocchi[-1].count(b"\n")
    lines = b"".join(reversed(blocchi)).decode("utf-8", errors="replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]  # prima riga probabilmente tagliata a meta'
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out[-limit:]


# ---------------------------------------------------------------------------
# Dashboard di monitoraggio (host, ultime inferenze, grafico multilinea)
# ---------------------------------------------------------------------------

# Path considerati "inferenze" ai fini del grafico e del log richieste.
_INFERENCE_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/chat/jobs",
    "/v1/audio/",
    "/audio/transcribe",
    "/process",
    "/media/process",
)


def _parse_iso_to_epoch(value: Any) -> float | None:
    """Converte un timestamp ISO-8601 (con timezone) in epoch secondi."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_inference_record(record: dict[str, Any]) -> bool:
    if record.get("model"):
        return True
    path = str(record.get("path") or "")
    return any(path.startswith(prefix) for prefix in _INFERENCE_PATH_PREFIXES)


async def _dashboard_hosts() -> list[dict[str, Any]]:
    """Elenco host (Ollama, llama-swap / LM Studio) con i modelli installati e lo stato."""
    hosts: list[dict[str, Any]] = []

    ollama_entry: dict[str, Any] = {
        "name": "Ollama",
        "kind": "ollama",
        "base_url": config.OLLAMA_HOST,
        "reachable": False,
        "error": None,
        "models": [],
    }
    try:
        tags = await list_tags()
        for t in tags:
            name = str(t.get("name") or t.get("model") or "").strip()
            if not name:
                continue
            ollama_entry["models"].append({
                "id": name,
                "size": t.get("size"),
                "modified_at": t.get("modified_at"),
                "loaded": None,
            })
        ollama_entry["reachable"] = True
    except Exception as exc:  # noqa: BLE001
        ollama_entry["error"] = str(exc)
    ollama_entry["models"].sort(key=lambda m: str(m["id"]).lower())
    hosts.append(ollama_entry)

    if config.LMSTUDIO_ENABLED:
        # In produzione PROXY_PURE punta a llama-swap (:8093): stesso log unificato
        # delle inferenze, ma il nome host in dashboard deve dire la verita'.
        swap_mode = bool(config.LMSTUDIO_PROXY_PURE)
        lm_entry: dict[str, Any] = {
            "name": "llama-swap" if swap_mode else "LM Studio",
            "kind": "llama-swap" if swap_mode else "lmstudio",
            "base_url": config.LMSTUDIO_HOST,
            "reachable": False,
            "error": None,
            "models": [],
        }
        try:
            lm_models = await lmstudio_client.list_models()
            try:
                loaded_ctx = await lmstudio_client.get_loaded_models_context()
            except Exception:  # noqa: BLE001
                loaded_ctx = {}
            for m in lm_models:
                rid = str(m.get("id") or "").strip()
                if not rid:
                    continue
                lm_entry["models"].append({
                    "id": rid,
                    "size": m.get("size"),
                    "loaded": rid in loaded_ctx,
                    "context_length": loaded_ctx.get(rid),
                })
            lm_entry["reachable"] = True
        except Exception as exc:  # noqa: BLE001
            lm_entry["error"] = str(exc)
        lm_entry["models"].sort(key=lambda m: str(m["id"]).lower())
        hosts.append(lm_entry)

    return hosts


def _dashboard_backend_label(backend: Any) -> str | None:
    """Etichetta backend per la tabella: in proxy puro lmstudio = llama-swap."""
    if backend is None:
        return None
    text = str(backend)
    if config.LMSTUDIO_PROXY_PURE and text == "lmstudio":
        return "llama-swap"
    return text


def _dashboard_recent(records: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    """Ultime inferenze (piu' recenti in testa): chi, a quale modello/backend.

    Unifica Ollama e llama-swap/LM Studio nello stesso elenco (stesso audit log).
    """
    out: list[dict[str, Any]] = []
    for r in records:
        if not _is_inference_record(r):
            continue
        client = r.get("client") or {}
        app = client.get("application") or {}
        route = r.get("route") or {}
        raw_backend = route.get("backend") or route.get("kind")
        out.append({
            "request_id": r.get("request_id"),
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "duration_ms": r.get("duration_ms"),
            "status_code": r.get("status_code"),
            "path": r.get("path"),
            "model": r.get("model") or "—",
            "backend": _dashboard_backend_label(raw_backend),
            "client_name": app.get("resolved_name") or "legacy-client",
            "client_ip": client.get("ip"),
            "declared": bool(app.get("declared")),
        })
    return out[-limit:][::-1]


_DASHBOARD_WINDOWS: dict[str, dict[str, int]] = {
    # chiave: (span_s, bucket_s)
    "hour": {"span_s": 3600, "bucket_s": 300},          # 12 punti da 5 min
    "day": {"span_s": 86400, "bucket_s": 3600},         # 24 punti da 1 ora
    "week": {"span_s": 604800, "bucket_s": 86400},      # 7 punti da 1 giorno
}
_DASHBOARD_MAX_SERIES = 8


def _dashboard_series(records: list[dict[str, Any]], now_epoch: float) -> dict[str, Any]:
    """Serie temporali per il grafico multilinea (richieste per modello).

    Ritorna, per ogni finestra (hour/day/week): labels (epoch inizio bucket),
    e datasets {model: [conteggi]}. Limita ai top-N modelli piu' richiesti.
    """
    parsed: list[tuple[float, str]] = []
    for r in records:
        if not _is_inference_record(r):
            continue
        model = str(r.get("model") or "").strip()
        if not model:
            continue
        ts = _parse_iso_to_epoch(r.get("started_at"))
        if ts is None:
            continue
        parsed.append((ts, model))

    out: dict[str, Any] = {}
    for wkey, cfg in _DASHBOARD_WINDOWS.items():
        span_s = cfg["span_s"]
        bucket_s = cfg["bucket_s"]
        n_buckets = span_s // bucket_s
        end = int(now_epoch // bucket_s) * bucket_s + bucket_s
        start = end - n_buckets * bucket_s
        labels = [int(start + i * bucket_s) for i in range(n_buckets)]

        model_counts: dict[str, int] = {}
        per_model: dict[str, list[int]] = {}
        for ts, model in parsed:
            if ts < start or ts >= end:
                continue
            idx = int((ts - start) // bucket_s)
            if idx < 0 or idx >= n_buckets:
                continue
            series = per_model.setdefault(model, [0] * n_buckets)
            series[idx] += 1
            model_counts[model] = model_counts.get(model, 0) + 1

        top_models = sorted(model_counts, key=lambda m: model_counts[m], reverse=True)[:_DASHBOARD_MAX_SERIES]
        datasets = [{"model": m, "data": per_model[m], "total": model_counts[m]} for m in top_models]
        out[wkey] = {
            "labels": labels,
            "bucket_s": bucket_s,
            "datasets": datasets,
            "total_requests": sum(model_counts.values()),
        }
    return out


# ---------------------------------------------------------------------------
# Contesto per LLM orchestratore (machine-readable / prompt-ready)
# ---------------------------------------------------------------------------


def _infer_model_capabilities(model_id: str) -> list[str]:
    """Capability euristiche dal nome del modello (best-effort)."""
    mid = str(model_id or "").lower()
    if "embed" in mid:
        return ["embeddings"]
    caps: list[str] = ["chat", "tools"]
    if any(k in mid for k in ("coder", "-code", "code-", "codestral")):
        caps.append("coding")
    if any(k in mid for k in ("vl", "vision", "llava", "gemma-3", "gemma3", "qwen3-vl")):
        caps.append("vision")
    if "ocr" in mid:
        caps.append("ocr")
        if "vision" not in caps:
            caps.append("vision")
    if any(k in mid for k in ("qwen3", "gpt-oss", "reason", "think", "-r1", "deepseek-r1")):
        caps.append("thinking")
    seen: set[str] = set()
    out: list[str] = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _model_usage_counts(records: list[dict[str, Any]], now_epoch: float) -> dict[str, dict[str, int]]:
    """Conteggio chiamate per modello nelle ultime 24h e 7 giorni."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        model = str(r.get("model") or "").strip()
        if not model:
            continue
        ts = _parse_iso_to_epoch(r.get("started_at"))
        if ts is None:
            continue
        age = now_epoch - ts
        entry = out.setdefault(model, {"last_24h": 0, "last_7d": 0})
        if age <= 604800:
            entry["last_7d"] += 1
            if age <= 86400:
                entry["last_24h"] += 1
    return out


def _llm_endpoints_catalog() -> list[dict[str, str]]:
    """Endpoint operativi con indicazione di quando usarli."""
    eps: list[dict[str, str]] = [
        {
            "method": "POST",
            "path": "/v1/chat/completions",
            "use": "Chat/completion OpenAI-compatible (streaming e tools). Usa il campo 'model' con un id della lista models.",
        },
        {
            "method": "POST",
            "path": "/v1/chat/jobs",
            "use": "Come sopra ma asincrono (stream=false): ritorna job_id, poi GET /v1/chat/jobs/{id}/result. Utile su tunnel per risposte lunghe.",
        },
        {
            "method": "GET",
            "path": "/v1/models",
            "use": "Lista id modello richiamabili (stessa 'model' da passare in chat).",
        },
    ]
    eps.append({
        "method": "POST",
        "path": "/process",
        "use": "OCR di PDF/immagini (motore glm-ocr): NON usare la chat per l'OCR, usa questo. Ritorna job_id, poi GET /result/{id}.",
    })
    if config.VOCE_URL or config.WHISPER_ENABLED:
        eps.append({
            "method": "POST",
            "path": "/v1/audio/transcriptions",
            "use": (
                "Trascrizione audio (OpenAI-compat via speaches se VOCE_URL, "
                "altrimenti Whisper interno). Per audio->testo, non usare la chat."
            ),
        })
    if config.VOCE_URL:
        eps.append({
            "method": "POST",
            "path": "/v1/audio/speech",
            "use": "Sintesi vocale (OpenAI-compat via speaches). JSON model/input/voice -> audio.",
        })
    if config.MEDIA_ENABLED:
        eps.append({
            "method": "POST",
            "path": "/media/process",
            "use": "Restauro/upscaling immagini (SUPIR). Ritorna job_id, poi GET /media/result/{id}.",
        })
    if config.DECIDER_URL:
        eps.append({
            "method": "POST",
            "path": "/v1/decide",
            "use": (
                "Decisione tipizzata (non genera testo): state/opzioni/istruzioni. "
                "Modelli id decider:laya e decider:decision-4b. Non usare la chat."
            ),
        })
    return eps


def _llm_routing_hints(models: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Suggerimenti di routing task->modello, basati sui modelli realmente presenti."""
    ids = [str(m.get("id") or "") for m in models]
    low = [i.lower() for i in ids]

    def pick(pred) -> str | None:
        for original, lowered in zip(ids, low):
            if pred(lowered):
                return original
        return None

    hints: list[dict[str, str]] = []
    coder = pick(lambda x: "coder" in x or "code" in x)
    if coder:
        hints.append({"task": "coding / generazione-refactor codice", "model": coder})
    vision = pick(lambda x: any(k in x for k in ("vl", "vision", "llava")))
    if vision:
        hints.append({"task": "immagini / vision (descrizione, Q&A visivo)", "model": vision})
    general = pick(
        lambda x: "embed" not in x and "coder" not in x and "code" not in x and "ocr" not in x
    )
    if general:
        hints.append({"task": "chat generica / italiano", "model": general})
    embed = pick(lambda x: "embed" in x)
    if embed:
        hints.append({"task": "embeddings (retrieval/RAG)", "model": embed})
    hints.append({"task": "OCR documenti (PDF/immagini)", "model": "→ usa POST /process (glm-ocr), non la chat"})
    return hints


async def _llm_context_payload(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now_epoch = time.time()
    if records is None:
        records = (
            _tail_jsonl(config.REQUESTS_LOG_PATH, limit=20000)
            if config.REQUESTS_LOG_PATH is not None
            else []
        )

    catalog = await _get_models_cached()
    loaded_ctx: dict[str, Any] = {}
    if config.LMSTUDIO_ENABLED:
        try:
            loaded_ctx = await lmstudio_client.get_loaded_models_context()
        except Exception:  # noqa: BLE001
            loaded_ctx = {}
    usage = _model_usage_counts(records, now_epoch)

    models: list[dict[str, Any]] = []
    for m in catalog:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        backend = str(m.get("owned_by") or "")
        loaded: bool | None = None
        ctx_len = m.get("context_length")
        if backend == "lmstudio":
            bare = _strip_lmstudio_prefix(mid)
            if bare in loaded_ctx:
                loaded = True
                ctx_len = loaded_ctx.get(bare) or ctx_len
            else:
                loaded = False
        models.append({
            "id": mid,
            "backend": backend,
            "loaded": loaded,
            "context_length": ctx_len,
            "size_bytes": m.get("size"),
            "capabilities": _infer_model_capabilities(mid),
            "usage": usage.get(mid, {"last_24h": 0, "last_7d": 0}),
        })
    models.sort(
        key=lambda x: (
            0 if x["loaded"] else 1,
            -int(x["usage"]["last_7d"]),
            str(x["id"]).lower(),
        )
    )

    return {
        "server": "LocalRouter — gateway OpenAI-compatible (Ollama + LM Studio) con OCR/Whisper/Media",
        "server_name": config.SERVER_NAME,
        "generated_at": _iso_utc(now_epoch),
        "auth": {
            "required": bool(config.API_KEY),
            "header": "Authorization: Bearer <API_KEY>  (in alternativa X-API-Key: <API_KEY>)",
        },
        "identify_yourself": {
            "why": "Facoltativo ma consigliato: consente al server di tracciare chi chiama nel log/monitor.",
            "headers": {
                "X-Client-Name": "nome applicativo (es. 'router-agent')",
                "X-Client-Id": "id univoco opzionale",
                "X-Client-Version": "versione opzionale",
            },
        },
        "hardware": {
            "gpu": config.GPU_INFO or "non specificata (imposta GPU_INFO in .env)",
            "note": (
                "In VRAM sta un solo LLM di grandi dimensioni per volta. "
                "Su LM Studio caricare un modello scarica gli altri (unload+reload => latenza extra al primo token). "
                "Ollama usa keep-alive e puo' scaricare dopo inattivita'. "
                "Preferisci un modello con loaded=true per risposte rapide ed evita cambi di modello frequenti."
            ),
        },
        "call_convention": {
            "chat_endpoint": "/v1/chat/completions",
            "model_field": "Passa esattamente l'id di 'models[].id'.",
            "lmstudio_prefix": "Gli id LM Studio iniziano con 'lmstudio:'; passali interi (prefisso incluso).",
            "streaming": "Supportato (stream=true, SSE). Per risposte lunghe via tunnel preferisci /v1/chat/jobs.",
            "tools": "capabilities include 'tools' in via euristica; se il modello non li supporta il server ritenta automaticamente senza tools.",
        },
        "routing_hints": _llm_routing_hints(models),
        "endpoints": _llm_endpoints_catalog(),
        "models": models,
    }


def _fmt_bytes_compact(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f}{units[i]}" if i > 0 and v < 10 else f"{int(v)}{units[i]}"


def _render_llm_context_md(payload: dict[str, Any]) -> str:
    """Rende il payload come markdown compatto, pronto per un system prompt."""
    lines: list[str] = []
    lines.append(f"# {payload['server']}")
    lines.append(f"**Istanza server:** `{payload.get('server_name', '')}`")
    lines.append(f"_Aggiornato: {payload['generated_at']}_")
    lines.append("")

    auth = payload["auth"]
    lines.append("## Autenticazione")
    lines.append(f"- API key {'OBBLIGATORIA' if auth['required'] else 'non richiesta (accesso aperto)'}.")
    lines.append(f"- Header: `{auth['header']}`")
    ident = payload["identify_yourself"]
    lines.append(f"- Identificati (consigliato): {', '.join(f'`{k}`' for k in ident['headers'])}.")
    lines.append("")

    hw = payload["hardware"]
    lines.append("## Hardware / vincoli")
    lines.append(f"- GPU: {hw['gpu']}.")
    lines.append(f"- {hw['note']}")
    lines.append("")

    conv = payload["call_convention"]
    lines.append("## Come chiamare")
    lines.append(f"- Chat: `POST {conv['chat_endpoint']}` (formato OpenAI).")
    lines.append(f"- Campo `model`: {conv['model_field']} {conv['lmstudio_prefix']}")
    lines.append(f"- Streaming: {conv['streaming']}")
    lines.append(f"- Tools: {conv['tools']}")
    lines.append("")

    lines.append("## Routing consigliato (task → modello)")
    for h in payload["routing_hints"]:
        lines.append(f"- **{h['task']}**: `{h['model']}`")
    lines.append("")

    lines.append("## Endpoint")
    for e in payload["endpoints"]:
        lines.append(f"- `{e['method']} {e['path']}` — {e['use']}")
    lines.append("")

    lines.append("## Modelli disponibili (loaded prima, poi per uso 7g)")
    lines.append("| id | backend | loaded | ctx | size | capabilities | 24h | 7g |")
    lines.append("|----|---------|--------|-----|------|--------------|-----|----|")
    for m in payload["models"]:
        loaded = "sì" if m["loaded"] else ("no" if m["loaded"] is False else "—")
        ctx = m["context_length"] if m["context_length"] else "—"
        size = _fmt_bytes_compact(m["size_bytes"]) or "—"
        caps = ",".join(m["capabilities"])
        u = m["usage"]
        lines.append(
            f"| `{m['id']}` | {m['backend']} | {loaded} | {ctx} | {size} | {caps} | "
            f"{u['last_24h']} | {u['last_7d']} |"
        )
    lines.append("")
    return "\n".join(lines)


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="it" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LocalRouter</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background:#0d1117; }
  .card { border:1px solid #21262d; }
  .mono { font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.85rem; }
  .model-list { max-height:220px; overflow:auto; }
  .badge-loaded { background:#238636; }
  .table-sm td, .table-sm th { padding:.35rem .5rem; }
  #chartWrap { position:relative; height:360px; }
  .status-2 { color:#3fb950; } .status-4 { color:#d29922; } .status-5 { color:#f85149; }
  .dot { display:inline-block; width:.55rem; height:.55rem; border-radius:50%; margin-right:.35rem; }
  .dot-ok { background:#3fb950; } .dot-ko { background:#f85149; }
</style>
</head>
<body>
<nav class="navbar navbar-expand bg-body-tertiary border-bottom mb-3">
  <div class="container-fluid gap-2 flex-wrap">
    <span class="navbar-brand mb-0 h1">LocalRouter</span>
    <span class="badge text-bg-info align-self-center" id="serverName">—</span>
    <div class="d-flex align-items-center gap-2 ms-auto flex-wrap">
      <input id="apiKey" type="password" class="form-control form-control-sm" style="width:220px"
             placeholder="API key (Bearer)" autocomplete="off">
      <div class="form-check form-switch m-0 text-nowrap">
        <input class="form-check-input" type="checkbox" id="autoRefresh" checked>
        <label class="form-check-label small" for="autoRefresh">Auto 30s</label>
      </div>
      <button id="refreshBtn" class="btn btn-sm btn-primary">Aggiorna</button>
    </div>
  </div>
</nav>

<div class="container-fluid">
  <div id="alertBox"></div>
  <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">
    <div class="text-secondary small" id="generatedAt">—</div>
    <div><span class="badge text-bg-secondary" id="gpuInfo">GPU: —</span></div>
    <div class="text-secondary small" id="totals">—</div>
  </div>

  <div class="row g-3 mb-3" id="hosts"></div>

  <div class="card mb-3">
    <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
      <span>Richieste per modello <span class="text-secondary small fw-normal">(Ollama + llama-swap)</span></span>
      <div class="btn-group btn-group-sm" role="group" id="windowBtns">
        <button type="button" class="btn btn-outline-light" data-win="hour">Ultima ora</button>
        <button type="button" class="btn btn-outline-light active" data-win="day">24 ore</button>
        <button type="button" class="btn btn-outline-light" data-win="week">Settimana</button>
      </div>
    </div>
    <div class="card-body"><div id="chartWrap"><canvas id="chart"></canvas></div></div>
  </div>

  <div class="card mb-4">
    <div class="card-header">Ultime inferenze <span class="text-secondary small fw-normal">(Ollama + llama-swap, unificate)</span></div>
    <div class="table-responsive">
      <table class="table table-sm table-hover align-middle mb-0">
        <thead><tr>
          <th>Ora</th><th>Client</th><th>IP</th><th>Modello</th>
          <th>Backend</th><th>Path</th><th class="text-end">Durata</th><th class="text-end">Status</th>
        </tr></thead>
        <tbody id="recentBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script>
(function () {
  const KEY = "dash_api_key";
  const $ = (id) => document.getElementById(id);
  const palette = ["#58a6ff","#3fb950","#d29922","#f85149","#bc8cff","#39c5cf","#ff7b72","#e3b341","#7ee787","#ffa657"];
  let colorMap = {}, chart = null, currentWin = "day", lastData = null;

  const keyInput = $("apiKey");
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");
  keyInput.value = localStorage.getItem(KEY) || "";
  keyInput.addEventListener("change", () => localStorage.setItem(KEY, keyInput.value.trim()));
  if (!keyInput.value) {
    alertMsg("Incolla la API key nel campo in alto a destra (Bearer token del file .env), poi clicca Aggiorna.", "warning");
  }

  function alertMsg(msg, kind) {
    $("alertBox").innerHTML = msg
      ? '<div class="alert alert-' + (kind||"warning") + ' py-2">' + msg + '</div>' : "";
  }
  function colorFor(model) {
    if (!colorMap[model]) {
      colorMap[model] = palette[Object.keys(colorMap).length % palette.length];
    }
    return colorMap[model];
  }
  function fmtBytes(n) {
    if (!n && n !== 0) return "";
    const u = ["B","KB","MB","GB","TB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
    return v.toFixed(v < 10 && i > 0 ? 1 : 0) + " " + u[i];
  }
  function fmtDur(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return ms + " ms";
    return (ms/1000).toFixed(ms < 10000 ? 2 : 1) + " s";
  }
  function fmtClock(epochSec, bucketS) {
    const d = new Date(epochSec * 1000);
    const p = (x) => String(x).padStart(2, "0");
    if (bucketS >= 86400) return p(d.getDate()) + "/" + p(d.getMonth()+1);
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function statusClass(code) {
    if (!code) return "";
    return "status-" + String(code)[0];
  }

  function renderHosts(hosts) {
    $("hosts").innerHTML = (hosts || []).map((h) => {
      const dot = h.reachable ? '<span class="dot dot-ok"></span>' : '<span class="dot dot-ko"></span>';
      const rows = (h.models || []).map((m) => {
        const loaded = m.loaded ? ' <span class="badge badge-loaded">caricato</span>' : "";
        const size = m.size ? ' <span class="text-secondary">' + fmtBytes(m.size) + '</span>' : "";
        const ctx = m.context_length ? ' <span class="text-secondary">ctx ' + m.context_length + '</span>' : "";
        return '<div class="mono">' + esc(m.id) + loaded + size + ctx + '</div>';
      }).join("");
      const err = h.error ? '<div class="text-danger small">' + esc(h.error) + '</div>' : "";
      return (
        '<div class="col-12 col-lg-6"><div class="card h-100"><div class="card-body">' +
        '<div class="d-flex justify-content-between"><h5 class="card-title mb-1">' + dot + esc(h.name) +
        '</h5><span class="badge text-bg-secondary">' + (h.models||[]).length + ' modelli</span></div>' +
        '<div class="text-secondary small mono mb-2">' + esc(h.base_url) + '</div>' + err +
        '<div class="model-list">' + (rows || '<div class="text-secondary small">Nessun modello</div>') + '</div>' +
        '</div></div></div>'
      );
    }).join("");
  }

  function renderTable(recent) {
    $("recentBody").innerHTML = (recent || []).map((r) => {
      const t = r.started_at ? new Date(r.started_at).toLocaleString("it-IT") : "—";
      const decl = r.declared ? "" : ' <span class="badge text-bg-dark" title="client non dichiarato">legacy</span>';
      return '<tr>' +
        '<td class="text-nowrap small">' + t + '</td>' +
        '<td class="small">' + esc(r.client_name||"—") + decl + '</td>' +
        '<td class="mono small">' + esc(r.client_ip||"—") + '</td>' +
        '<td class="mono small">' + esc(r.model||"—") + '</td>' +
        '<td class="small">' + esc(r.backend||"—") + '</td>' +
        '<td class="mono small">' + esc(r.path||"—") + '</td>' +
        '<td class="text-end small">' + fmtDur(r.duration_ms) + '</td>' +
        '<td class="text-end small ' + statusClass(r.status_code) + '">' + esc(r.status_code||"—") + '</td>' +
        '</tr>';
    }).join("") || '<tr><td colspan="8" class="text-center text-secondary py-3">Nessuna inferenza registrata</td></tr>';
  }

  function renderChart(win) {
    if (!lastData) return;
    const s = (lastData.series || {})[win];
    if (!s) return;
    const labels = s.labels.map((e) => fmtClock(e, s.bucket_s));
    const datasets = (s.datasets || []).map((d) => ({
      label: d.model, data: d.data, borderColor: colorFor(d.model),
      backgroundColor: colorFor(d.model), tension: 0.25, borderWidth: 2,
      pointRadius: 2, pointHoverRadius: 4,
    }));
    const cfg = {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        scales: {
          x: { grid: { color: "#21262d" }, ticks: { color: "#8b949e" } },
          y: { beginAtZero: true, grid: { color: "#21262d" }, ticks: { color: "#8b949e", precision: 0 } },
        },
        plugins: { legend: { labels: { color: "#c9d1d9", boxWidth: 12 } } },
      },
    };
    if (chart) { chart.data = cfg.data; chart.update(); }
    else { chart = new Chart($("chart").getContext("2d"), cfg); }
  }

  async function load() {
    const key = keyInput.value.trim();
    const headers = key ? { "Authorization": "Bearer " + key } : {};
    $("refreshBtn").disabled = true;
    try {
      const res = await fetch("/dashboard/data?recent=80", { headers });
      if (res.status === 401) { alertMsg("API key mancante o non valida.", "danger"); return; }
      if (!res.ok) { alertMsg("Errore server: " + res.status, "danger"); return; }
      alertMsg("");
      lastData = await res.json();
      $("serverName").textContent = lastData.server_name || "LocalRouter";
      document.title = "LocalRouter · " + (lastData.server_name || "");
      $("generatedAt").textContent = "Aggiornato: " + new Date(lastData.generated_at).toLocaleString("it-IT");
      $("gpuInfo").textContent = "GPU: " + (lastData.gpu || "non specificata");
      const s = (lastData.series || {})[currentWin] || {};
      $("totals").textContent = (s.total_requests || 0) + " richieste nella finestra";
      renderHosts(lastData.hosts);
      renderTable(lastData.recent);
      renderChart(currentWin);
      if (!lastData.audit_enabled) alertMsg("Audit log disabilitato: grafico e tabella vuoti.", "warning");
    } catch (e) {
      alertMsg("Errore rete: " + e, "danger");
    } finally {
      $("refreshBtn").disabled = false;
    }
  }

  document.querySelectorAll("#windowBtns button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#windowBtns button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentWin = btn.dataset.win;
      const s = (lastData && lastData.series || {})[currentWin] || {};
      $("totals").textContent = (s.total_requests || 0) + " richieste nella finestra";
      renderChart(currentWin);
    });
  });

  $("refreshBtn").addEventListener("click", load);
  let timer = null;
  function schedule() {
    if (timer) clearInterval(timer);
    if ($("autoRefresh").checked) timer = setInterval(load, 30000);
  }
  $("autoRefresh").addEventListener("change", schedule);
  schedule();
  load();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers modelli (dal progetto 10)
# ---------------------------------------------------------------------------


_OLLAMA_CAP_ALIASES = {
    "completion": "chat",
    "chat": "chat",
    "tools": "tools",
    "tool": "tools",
    "vision": "vision",
    "thinking": "thinking",
    "embedding": "embeddings",
    "embeddings": "embeddings",
}


def _capabilities_from_show(info: dict[str, Any]) -> list[str]:
    caps: set[str] = set()

    # Ollama recente espone direttamente il campo `capabilities` in /api/show
    # (es. ["completion", "tools", "vision"]): e' la fonte piu' affidabile.
    for raw in info.get("capabilities") or []:
        mapped = _OLLAMA_CAP_ALIASES.get(str(raw).strip().lower())
        if mapped:
            caps.add(mapped)

    # Fallback/augmento euristico su template e families (Ollama vecchi o campo assente).
    details = info.get("details") or {}
    families = {str(x).lower() for x in (details.get("families") or [])}
    if "clip" in families or "llava" in families:
        caps.add("vision")
    template = str(info.get("template") or "").lower()
    if "tool" in template or "<|tool_call|>" in template:
        caps.add("tools")
    if any(tok in template for tok in ("<think>", "reasoning", "thought")):
        caps.add("thinking")

    # "chat" sempre presente tranne per modelli solo-embedding.
    if caps != {"embeddings"}:
        caps.add("chat")
    return sorted(caps)


def _context_length_from_show(info: dict[str, Any]) -> int | None:
    model_info = info.get("model_info") or {}
    if isinstance(model_info, dict):
        best = 0
        for key, val in model_info.items():
            if not isinstance(key, str) or not key.endswith(".context_length"):
                continue
            try:
                candidate = int(val)
            except (TypeError, ValueError):
                continue
            if candidate > best:
                best = candidate
        if best > 0:
            return best
    for key in ("context_length", "num_ctx"):
        val = info.get(key)
        if isinstance(val, int) and val > 0:
            return val
    params = info.get("parameters") or ""
    if isinstance(params, str):
        for line in params.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0] == "num_ctx":
                try:
                    return int(parts[1])
                except ValueError:
                    continue
    return None


async def _describe_model(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or item.get("model") or "").strip()
    if not name:
        return None
    if not config.is_model_allowed(name):
        return None
    info = await show_model(name)
    capabilities = _capabilities_from_show(info)
    context_length = _context_length_from_show(info)
    details = info.get("details") or {}
    architecture = {
        "modality": "text->text" if "vision" not in capabilities else "text+image->text",
        "tokenizer": details.get("family") or details.get("families") or "unknown",
        "instruct_type": details.get("format"),
    }
    return {
        "id": name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "ollama",
        "context_length": context_length,
        "architecture": architecture,
        "capabilities": capabilities,
        "size": item.get("size"),
        "modified_at": item.get("modified_at"),
    }


def _model_supports_tools(model_id: str) -> bool:
    """True se il modello supporta i tools.

    Regola prudente: si strippa SOLO quando le capabilities sono note e non
    includono "tools". Se il modello non e' in cache o le capabilities sono
    assenti/None (sconosciute), si assume True e non si stripano mai i tools
    alla cieca (il fallback reattivo su errore "does not support tools" resta).
    """
    target = str(model_id or "").strip().lower()
    if not target:
        return True
    for item in _MODELS_CACHE.get("data") or []:
        if str(item.get("id") or "").strip().lower() == target:
            caps = item.get("capabilities")
            if not caps:  # None o lista vuota -> capabilities sconosciute
                return True
            return "tools" in [str(c).lower() for c in caps]
    return True


def _model_supports_thinking(model_id: str) -> bool:
    """True solo se le capabilities in cache includono esplicitamente 'thinking'.

    Se il modello non e' in cache o le capabilities sono assenti, restituisce False:
    non inviamo `think` ad Ollama (evita 400 su modelli non-thinking).
    """
    target = str(model_id or "").strip().lower()
    if not target:
        return False
    for item in _MODELS_CACHE.get("data") or []:
        if str(item.get("id") or "").strip().lower() == target:
            caps = item.get("capabilities")
            if not caps:
                return False
            return "thinking" in [str(c).lower() for c in caps]
    return False


def _client_requested_reasoning(req: ChatCompletionRequest) -> bool:
    """True se il client ha chiesto esplicitamente il reasoning/thinking."""
    extra = req.model_extra or {}
    if extra.get("reasoning") is not None:
        return True
    if extra.get("reasoning_effort") is not None:
        return True
    if req.thinking_budget_tokens is not None or req.budget_pensiero is not None or req.reasoning_budget is not None:
        return True
    if extra.get("thinking_budget_tokens") is not None or extra.get("budget_pensiero") is not None:
        return True
    ctk = req.chat_template_kwargs or {}
    if ctk.get("enable_thinking") is True:
        return True
    return False


def _extract_thinking_budget(req: ChatCompletionRequest) -> int | None:
    """Estrae il budget di ragionamento per-richiesta.

    Ordine: thinking_budget_tokens (nome llama.cpp) → budget_pensiero (alias Kairos)
    → reasoning_budget → stessi campi in model_extra. Ritorna None se assente.
    """
    for candidate in (
        req.thinking_budget_tokens,
        req.budget_pensiero,
        req.reasoning_budget,
    ):
        if candidate is not None:
            try:
                return max(0, int(candidate))
            except (TypeError, ValueError):
                pass
    extra = req.model_extra or {}
    for key in ("thinking_budget_tokens", "budget_pensiero", "reasoning_budget"):
        if extra.get(key) is not None:
            try:
                return max(0, int(extra[key]))
            except (TypeError, ValueError):
                pass
    return None


def _apply_thinking_budget(lms_body: dict[str, Any], req: ChatCompletionRequest) -> None:
    """Inoltra il budget al primo livello del JSON (dove llama-server lo legge)."""
    budget = _extract_thinking_budget(req)
    if budget is not None:
        lms_body["thinking_budget_tokens"] = budget


#: Parametri di llama-server fuori dallo standard OpenAI, inoltrati se il client li manda.
#: `cache_prompt: false` serve ai banchi (04/10): a temperatura 0 la cache del prompt
#: cambia gli arrotondamenti e la stessa richiesta dava risposte diverse fra due corse.
#: `grammar` (GBNF) e `json_schema` top-level: nativi di llama-server; senza questi nomi
#: restavano in model_extra e LocalRouter li buttava (09/10, TRE_BRACCI_BIS).
_PARAMETRI_LLAMA = (
    "cache_prompt",
    "top_k",
    "min_p",
    "repeat_penalty",
    "id_slot",
    "grammar",
    "json_schema",
    # 12/10: decisori sul modello caricato (stile Jev) leggono le probabilita' dei token.
    "logprobs",
    "top_logprobs",
)

_MSG_GRAMMAR_LMSTUDIO = (
    "grammar/json_schema top-level non supportati da LM Studio nativo "
    "(solo llama-swap/llama-server con LMSTUDIO_PROXY_PURE=1). "
    "Usa response_format.json_schema, oppure punta a un backend llama-server."
)


def _apply_parametri_llama(lms_body: dict[str, Any], req: ChatCompletionRequest) -> None:
    extra = req.model_extra or {}
    for nome in _PARAMETRI_LLAMA:
        if nome in extra:
            lms_body[nome] = extra[nome]


def _rifiuta_grammar_su_lmstudio_nativo(req: ChatCompletionRequest) -> None:
    """LM Studio vero non gestisce GBNF/json_schema top-level: 400 chiaro, non 400 opaco upstream."""
    if config.LMSTUDIO_PROXY_PURE:
        return
    extra = req.model_extra or {}
    if "grammar" in extra or "json_schema" in extra:
        raise HTTPException(status_code=400, detail=_MSG_GRAMMAR_LMSTUDIO)


def _rifiuta_url_remoti(req: ChatCompletionRequest) -> None:
    """SSRF: llama-server scarica gli URL http(s) dei media; qui passano solo i data: URI."""
    for msg in req.messages:
        if not isinstance(msg.content, list):
            continue
        for parte in msg.content:
            for chiave in ("image_url", "audio_url", "video_url", "input_audio"):
                v = parte.get(chiave)
                url = v.get("url", v.get("data", "")) if isinstance(v, dict) else v
                if isinstance(url, str) and url and not url.startswith("data:") and "://" in url[:12]:
                    raise HTTPException(status_code=400, detail="URL remoti nei media non ammessi: usa un data: URI")


_LMSTUDIO_PREFIX = "lmstudio:"


def _is_lmstudio_model(model_id: str) -> bool:
    return str(model_id or "").lower().startswith(_LMSTUDIO_PREFIX)


def _strip_lmstudio_prefix(model_id: str) -> str:
    return model_id[len(_LMSTUDIO_PREFIX):]


_NO_TOOLS_ERROR_MARKERS = (
    "does not support tools",
    "no support for tools",
    "jinja template",       # LM Studio community models with broken tool chat template
    "undefinedvalue",       # same error: "Cannot call something that is not a function: got UndefinedValue"
)


def _is_no_tools_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(marker in msg for marker in _NO_TOOLS_ERROR_MARKERS)


def _mark_model_no_tools(model_id: str) -> None:
    target = str(model_id or "").strip().lower()
    if not target:
        return
    for item in _MODELS_CACHE.get("data") or []:
        if str(item.get("id") or "").strip().lower() == target:
            caps = [c for c in (item.get("capabilities") or []) if str(c).lower() != "tools"]
            item["capabilities"] = caps
            return


def _lmstudio_audit_retries(request: Request) -> list[dict[str, Any]]:
    """Lista condivisa di retry LM Studio per la richiesta HTTP corrente."""
    meta = getattr(request.state, "lmstudio_retries", None)
    if meta is None:
        meta = []
        request.state.lmstudio_retries = meta
    return meta


async def _lmstudio_chat_nonstream_call(
    lms_body: dict[str, Any],
    model_id: str,
    retry_meta: list[dict[str, Any]],
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Chat non-stream LM Studio con retry transitorio e fallback senza tools."""
    try:
        return await lmstudio_client.chat(lms_body, retry_meta=retry_meta, request=request)
    except LMStudioError as exc:
        if _is_no_tools_error(exc) and lms_body.get("tools"):
            logger.warning("lmstudio_retry_no_tools | model=%s | err=%s", model_id, exc)
            retry_body = {k: v for k, v in lms_body.items() if k not in ("tools", "tool_choice")}
            _mark_model_no_tools(model_id)
            return await lmstudio_client.chat(retry_body, retry_meta=retry_meta, request=request)
        raise


async def _await_unless_client_gone(request: Request, aw: Any) -> Any:
    """Esegue la coroutine chat; se il client HTTP chiude, cancella l'upstream.

    Senza questo, kill del banco (client/docker) lascia le POST verso llama-swap
    aperte per minuti: pool saturo (503) e poi 429→502 sullo swap.
    """
    task = asyncio.create_task(aw)
    try:
        while True:
            if task.done():
                return await task
            if await request.is_disconnected():
                logger.warning("chat_client_disconnected | cancelling upstream")
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise LMStudioError(
                    "client disconnected; upstream cancelled",
                    status_code=499,
                    error_kind="client_disconnected",
                )
            await asyncio.wait({task}, timeout=0.5)
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise


async def _lmstudio_chat_stream_call(
    lms_body: dict[str, Any],
    model_id: str,
    retry_meta: list[dict[str, Any]],
    *,
    request: Request | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Chat stream LM Studio con retry transitorio e fallback senza tools."""
    try:
        async for chunk in lmstudio_client.chat_stream(
            lms_body, retry_meta=retry_meta, request=request
        ):
            yield chunk
    except LMStudioError as exc:
        if _is_no_tools_error(exc) and lms_body.get("tools"):
            logger.warning("lmstudio_retry_no_tools_stream | model=%s | err=%s", model_id, exc)
            retry_body = {k: v for k, v in lms_body.items() if k not in ("tools", "tool_choice")}
            _mark_model_no_tools(model_id)
            async for chunk in lmstudio_client.chat_stream(
                retry_body, retry_meta=retry_meta, request=request
            ):
                yield chunk
            return
        raise


async def _get_models_cached() -> list[dict[str, Any]]:
    now = time.monotonic()
    ttl = (
        config.MODELS_CACHE_TTL_S
        if (_MODELS_CACHE.get("lmstudio_ok") or not config.LMSTUDIO_ENABLED)
        else _MODELS_CACHE_FAIL_TTL_S
    )
    # Nota: data=[] e' falsy — non usare `if data` come guardia TTL, altrimenti
    # dopo un fallimento LM Studio si ricostruisce a ogni richiesta (timeout 10s).
    if _MODELS_CACHE["at"] and (now - _MODELS_CACHE["at"]) < ttl:
        return list(_MODELS_CACHE["data"])
    async with _MODELS_BUILD_LOCK:
        now = time.monotonic()
        ttl = (
            config.MODELS_CACHE_TTL_S
            if (_MODELS_CACHE.get("lmstudio_ok") or not config.LMSTUDIO_ENABLED)
            else _MODELS_CACHE_FAIL_TTL_S
        )
        if _MODELS_CACHE["at"] and (now - _MODELS_CACHE["at"]) < ttl:
            return list(_MODELS_CACHE["data"])

        all_models: list[dict[str, Any]] = []
        ollama_ok = False
        lmstudio_ok = False
        lmstudio_error: str | None = None

        # Ollama: descrizione ricca (capabilities + context_length da /api/show),
        # in parallelo. Necessario perche' _model_supports_tools legge le capabilities
        # dalla cache: senza, i tools verrebbero strippati su ogni richiesta Ollama.
        try:
            tags = await list_tags()
            ollama_ok = True
            described = await asyncio.gather(
                *[_describe_model(t) for t in tags],
                return_exceptions=True,
            )
            for res in described:
                if isinstance(res, Exception):
                    logger.warning("describe_model_failed | err=%s", res)
                    continue
                if res:
                    all_models.append(res)
        except OllamaError as exc:
            logger.warning("models_cache_ollama_error | err=%s", exc)

        # LM Studio / llama-swap
        if config.LMSTUDIO_ENABLED:
            try:
                lms_models = await lmstudio_client.list_models()
                loaded_ctx = await lmstudio_client.get_loaded_models_context()
                vision_models = config.llamaswap_vision_models()
                for m in lms_models:
                    raw_id = str(m.get("id") or "")
                    if not raw_id:
                        continue
                    # Esclude architetture non caricabili da LM Studio (es. DiffusionGemma).
                    if lmstudio_client.is_lmstudio_runtime_unsupported(raw_id):
                        logger.info(
                            "models_cache_skip_unsupported_lms | model=%s",
                            raw_id,
                        )
                        continue
                    prefixed_id = f"{_LMSTUDIO_PREFIX}{raw_id}"
                    if config.MODEL_ALLOWLIST and not config.is_model_allowed(raw_id):
                        continue
                    capabilities = config.lmstudio_model_capabilities(raw_id)
                    if capabilities is None:
                        capabilities = _infer_model_capabilities(raw_id)
                        if raw_id.strip().lower() in vision_models and "vision" not in capabilities:
                            capabilities.append("vision")
                    all_models.append({
                        "id": prefixed_id,
                        "object": "model",
                        "created": int(m.get("created") or 0),
                        "owned_by": "lmstudio",
                        "context_length": loaded_ctx.get(raw_id),
                        "capabilities": capabilities,
                    })
                lmstudio_ok = True
            except LMStudioError as exc:
                lmstudio_error = str(exc)
                logger.warning("models_cache_lmstudio_error | err=%s", exc)

        # Decisori: un solo upstream (DECIDER_URL), prefisso decider: come lmstudio:
        if config.DECIDER_URL:
            for bare in config.DECIDER_MODELS:
                name = str(bare or "").strip()
                if not name:
                    continue
                all_models.append({
                    "id": f"decider:{name}",
                    "object": "model",
                    "created": 0,
                    "owned_by": "decider",
                    "context_length": None,
                    "capabilities": ["decide"],
                })

        _MODELS_CACHE["data"] = all_models
        _MODELS_CACHE["at"] = now
        _MODELS_CACHE["ollama_ok"] = ollama_ok
        _MODELS_CACHE["lmstudio_ok"] = lmstudio_ok if config.LMSTUDIO_ENABLED else True
        _MODELS_CACHE["lmstudio_error"] = lmstudio_error
        return list(all_models)


# ---------------------------------------------------------------------------
# Helpers chat completion (dal progetto 10)
# ---------------------------------------------------------------------------


def _normalize_tool_calls_for_ollama(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        new_call = dict(call)
        fn = dict(new_call.get("function") or {})
        args = fn.get("arguments")
        if isinstance(args, str):
            parsed: Any = None
            try:
                parsed = json.loads(args) if args.strip() else {}
            except (json.JSONDecodeError, ValueError):
                logger.warning("tool_call_args_malformed | name=%s | raw=%s", fn.get("name"), args[:200])
                parsed = {"_raw": args}
            if not isinstance(parsed, (dict, list)):
                parsed = {"_value": parsed}
            fn["arguments"] = parsed
        elif args is None:
            fn["arguments"] = {}
        new_call["function"] = fn
        out.append(new_call)
    return out


def _messages_openai_to_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            images: list[str] = []
            for piece in content:
                if not isinstance(piece, dict):
                    continue
                ptype = piece.get("type")
                if ptype in ("text", None) and piece.get("text"):
                    parts.append(str(piece["text"]))
                elif ptype == "image_url":
                    url = ""
                    image_url = piece.get("image_url")
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url") or "")
                    elif isinstance(image_url, str):
                        url = image_url
                    if url.startswith("data:") and "," in url:
                        images.append(url.split(",", 1)[1])
                    elif url:
                        images.append(url)
            normalized: dict[str, Any] = {"role": role, "content": "\n".join(parts).strip()}
            if images:
                normalized["images"] = images
        else:
            normalized = {"role": role, "content": str(content or "")}
        if role == "tool":
            normalized["tool_call_id"] = msg.get("tool_call_id")
        if msg.get("tool_calls"):
            normalized["tool_calls"] = _normalize_tool_calls_for_ollama(msg["tool_calls"])
        if msg.get("name"):
            normalized["name"] = msg["name"]
        out.append(normalized)
    return out


def _options_from_request(req: ChatCompletionRequest) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if req.temperature is not None:
        options["temperature"] = float(req.temperature)
    if req.top_p is not None:
        options["top_p"] = float(req.top_p)
    if req.max_tokens is not None:
        options["num_predict"] = int(req.max_tokens)
    if req.stop is not None:
        options["stop"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    if req.presence_penalty is not None:
        options["presence_penalty"] = float(req.presence_penalty)
    if req.frequency_penalty is not None:
        options["frequency_penalty"] = float(req.frequency_penalty)
    if req.seed is not None:
        options["seed"] = int(req.seed)
    return options


_NUM_CTX_BUCKETS: tuple[int, ...] = (4096, 8192, 16384, 32768, 65536, 131072)


def _model_max_context(model_id: str) -> int | None:
    target = str(model_id or "").strip().lower()
    if not target:
        return None
    for item in _MODELS_CACHE.get("data") or []:
        if str(item.get("id") or "").strip().lower() == target:
            ctx = item.get("context_length") or item.get("max_context_length")
            try:
                return int(ctx) if ctx else None
            except (TypeError, ValueError):
                return None
    return None


def _estimate_prompt_tokens(messages: list[dict[str, Any]], tools: Any) -> int:
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("text"):
                    total_chars += len(str(piece["text"]))
        for call in msg.get("tool_calls") or []:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                try:
                    total_chars += len(json.dumps(args, ensure_ascii=False))
                except (TypeError, ValueError):
                    total_chars += len(str(args))
            elif args is not None:
                total_chars += len(str(args))
    if tools:
        try:
            total_chars += len(json.dumps(tools, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    chars_per_token = max(1.0, float(config.NUM_CTX_CHARS_PER_TOKEN or 3.2))
    return int(total_chars / chars_per_token) + 1


def _pick_num_ctx(required_tokens: int, model_id: str) -> int:
    lo = max(1, int(config.DEFAULT_NUM_CTX or 8192))
    hi_cfg = int(config.MAX_NUM_CTX or 0)
    model_cap = _model_max_context(model_id) or 0
    caps = [c for c in (hi_cfg, model_cap) if c > 0]
    hi = min(caps) if caps else max(lo, required_tokens)
    desired = max(lo, required_tokens)
    desired = min(desired, hi) if hi > 0 else desired
    for bucket in _NUM_CTX_BUCKETS:
        if bucket >= desired:
            return min(bucket, hi) if hi > 0 else bucket
    return hi if hi > 0 else desired


def _build_ollama_body(req: ChatCompletionRequest) -> dict[str, Any]:
    raw_messages = [m.model_dump(exclude_none=True) for m in req.messages]
    messages = _messages_openai_to_ollama(raw_messages)
    body: dict[str, Any] = {"model": req.model, "messages": messages}
    options = _options_from_request(req)
    if req.tools:
        body["tools"] = req.tools
    if config.AUTO_NUM_CTX and "num_ctx" not in options:
        prompt_tokens = _estimate_prompt_tokens(messages, body.get("tools"))
        completion_tokens = int(options.get("num_predict") or 1024)
        safety = int(config.NUM_CTX_SAFETY_TOKENS or 512)
        required = prompt_tokens + completion_tokens + safety
        chosen = _pick_num_ctx(required, req.model)
        options["num_ctx"] = chosen
        logger.info(
            "auto_num_ctx | model=%s | prompt_tok=%d | required=%d | num_ctx=%d",
            req.model, prompt_tokens, required, chosen,
        )
    if options:
        body["options"] = options
    if req.response_format and req.response_format.get("type") == "json_object":
        body["format"] = "json"
    if config.CHAT_OLLAMA_KEEP_ALIVE:
        body["keep_alive"] = config.CHAT_OLLAMA_KEEP_ALIVE

    # Ollama >= 0.9: `think` disabilita il blocco di ragionamento interno.
    # Solo su modelli thinking-capable e solo se il client non ha chiesto reasoning.
    extra = req.model_extra or {}
    if "think" in extra:
        body["think"] = extra["think"]
    elif _model_supports_thinking(req.model) and not _client_requested_reasoning(req):
        body["think"] = False
        logger.info("think_false | model=%s", req.model)

    return body


def _ollama_message_to_openai(message: dict[str, Any]) -> dict[str, Any]:
    content = str(message.get("content") or message.get("thinking") or "")
    out: dict[str, Any] = {
        "role": message.get("role") or "assistant",
        "content": content,
    }
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        normalized_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                args_str = json.dumps(args, ensure_ascii=False)
            else:
                args_str = str(args or "{}")
            normalized_calls.append({
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": fn.get("name") or "", "arguments": args_str},
            })
        out["tool_calls"] = normalized_calls
    return out


def _build_chat_completion(ollama_response: dict[str, Any], request_model: str) -> dict[str, Any]:
    message = ollama_response.get("message") or {}
    finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
    prompt_tokens = int(ollama_response.get("prompt_eval_count") or 0)
    completion_tokens = int(ollama_response.get("eval_count") or 0)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{"index": 0, "message": _ollama_message_to_openai(message), "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Endpoint comuni
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> dict[str, str]:
    out: dict[str, str] = {
        "service": "LocalRouter",
        "server_name": config.SERVER_NAME,
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/dashboard",
        "dashboard_data": "/dashboard/data",
        "llm_context": "/llm/context (?format=json|text)",
        "debug_requests": "/debug/requests",
        "metrics_system": "/metrics/system",
        "metrics_process": "/metrics/process",
        "models": "/v1/models",
        "chat": "/v1/chat/completions",
        "chat_jobs_submit": "POST /v1/chat/jobs",
        "chat_jobs_status": "GET /v1/chat/jobs/{job_id}",
        "chat_jobs_result": "GET /v1/chat/jobs/{job_id}/result",
        "chat_jobs_events": "GET /v1/chat/jobs/{job_id}/events",
        "ocr_process": "/process",
        "ocr_cancel": "/cancel/{job_id}",
        "ocr_jobs": "/jobs",
        "ocr_cancel_all": "/cancel-all",
    }
    if config.PULL_RESTART_HTTP_KEY:
        out["admin_pull_restart"] = "POST /admin/pull-restart (token dedicato, vedi README)"
    if config.MEDIA_ENABLED:
        out["media_process"] = "/media/process"
        out["media_status"] = "/media/status/{job_id}"
        out["media_result"] = "/media/result/{job_id}"
        out["media_cancel"] = "/media/cancel/{job_id}"
        out["media_jobs"] = "/media/jobs"
        out["media_cancel_all"] = "/media/cancel-all"
    if config.VOCE_URL:
        out["audio_transcriptions"] = "POST /v1/audio/transcriptions (OpenAI-compat via VOCE_URL)"
        out["audio_translations"] = "POST /v1/audio/translations (OpenAI-compat via VOCE_URL)"
        out["audio_speech"] = "POST /v1/audio/speech (TTS OpenAI-compat via VOCE_URL)"
    if config.DECIDER_URL:
        out["decide"] = "POST /v1/decide (proxy a DECIDER_URL; modelli decider:*)"
    if config.WHISPER_ENABLED:
        if not config.VOCE_URL:
            out["audio_transcriptions"] = "POST /v1/audio/transcriptions (OpenAI-compat)"
            out["audio_translations"] = "POST /v1/audio/translations (OpenAI-compat)"
        out["audio_transcribe"] = "POST /audio/transcribe (nativo, verbose_json)"
        out["whisper_unload_llms"] = "POST /whisper/unload-llms (libera VRAM LM Studio)"
        out["whisper_download_model"] = "POST /whisper/download-model (prefetch pesi)"
    return out


@app.get("/health")
async def health(request: Request, _limite: None = Depends(require_health_rate_limit)) -> dict[str, Any]:
    """Health check unificato.

    - Controlla Ollama raggiungibilita'.
    - Verifica che glm-ocr sia installato.
    - Restituisce status: ok | degraded (09B interpreta ok/healthy/up -> ready).
    """

    ollama_ok = False
    tags: list[dict[str, Any]] = []
    try:
        tags = await list_tags()
        ollama_ok = True
    except OllamaError as exc:
        logger.warning("health_ollama_unreachable | err=%s", exc)

    installed_models = [str(t.get("name") or t.get("model") or "") for t in tags]
    ocr_model_name = config.OCR_MODEL.lower()
    model_present_ollama = any(ocr_model_name in m.lower() for m in installed_models)

    lmstudio_ok = False
    lmstudio_models: list[str] = []
    if config.LMSTUDIO_ENABLED:
        try:
            lm_list = await lmstudio_client.list_models()
            lmstudio_models = [
                str(m.get("id") or "")
                for m in lm_list
                if m.get("id")
                and not lmstudio_client.is_lmstudio_runtime_unsupported(str(m.get("id") or ""))
            ]
            lmstudio_ok = True
        except Exception as exc:
            logger.warning("health_lmstudio_unreachable | %s", exc)

    model_present_lmstudio = any(
        ocr_model_name in m.lower() or m.lower() in ocr_model_name
        for m in lmstudio_models
    )
    model_present = model_present_ollama or model_present_lmstudio

    media_health: dict[str, Any] = {}
    if config.MEDIA_ENABLED:
        from media.models_check import check_media_runtime

        media_health = check_media_runtime()

    whisper_health: dict[str, Any] = {}
    if config.WHISPER_ENABLED:
        from transcribe.whisper_check import check_whisper_runtime

        whisper_health = check_whisper_runtime()

    if model_present and (ollama_ok or lmstudio_ok):
        status = "ok"
    elif ollama_ok or lmstudio_ok:
        status = "degraded"
    else:
        status = "down"

    if not chiave_valida(request):
        return {"status": status}
    return {
        "status": status,
        "server_name": config.SERVER_NAME,
        "ollama_host": config.OLLAMA_HOST,
        "ollama_ok": ollama_ok,
        "ocr_model": config.OCR_MODEL,
        "ocr_model_present": model_present,
        "ocr_backend": (config.OCR_BACKEND or "auto"),
        "models_installed": len(tags),
        "models_cached": len(_MODELS_CACHE["data"]),
        "lmstudio_enabled": config.LMSTUDIO_ENABLED,
        "lmstudio_ok": lmstudio_ok,
        "lmstudio_host": config.LMSTUDIO_HOST,
        "lmstudio_proxy_pure": config.LMSTUDIO_PROXY_PURE,
        "lmstudio_models": lmstudio_models,
        "decider_url": config.DECIDER_URL or None,
        "decider_models": (
            [f"decider:{m}" for m in config.DECIDER_MODELS] if config.DECIDER_URL else []
        ),
        **media_health,
        **whisper_health,
    }


@app.get("/metrics/system")
async def metrics_system(_ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Snapshot veloce stato host: CPU, RAM, GPU/VRAM.

    - Se `nvidia-smi` non e' disponibile, la sezione GPU resta presente con `available=false`.
    - Se `psutil` non e' installato, CPU% e alcune metriche possono essere `null`.
    """
    return {
        "status": "ok",
        "ts": _now_ts(),
        "host": {
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "hostname": platform.node(),
        },
        "cpu": _cpu_snapshot(),
        "memory": _system_memory_snapshot(),
        "gpu": _gpu_snapshot(),
    }


@app.get("/metrics/process")
async def metrics_process(_ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Metriche del processo FastAPI corrente + GPU snapshot."""
    return {
        "status": "ok",
        "ts": _now_ts(),
        "process": _process_snapshot(),
        "gpu": _gpu_snapshot(),
    }


@app.post("/admin/unload")
async def admin_unload(ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Scarica i modelli via llama-swap con un cooldown globale al processo."""
    global _ADMIN_UNLOAD_LAST_MONO
    now = time.monotonic()
    cd = config.ADMIN_UNLOAD_COOLDOWN_S
    # Nessun await tra controllo e prenotazione: anche richieste concorrenti
    # vedono subito il tentativo, senza attendere la risposta di llama-swap.
    if _ADMIN_UNLOAD_LAST_MONO is not None and now - _ADMIN_UNLOAD_LAST_MONO < cd:
        wait = max(1, math.ceil(cd - (now - _ADMIN_UNLOAD_LAST_MONO)))
        logger.info("admin_unload_cooldown | ip=%s | retry_after=%s", ip, wait)
        raise HTTPException(429, f"Cooldown attivo: riprova tra {wait}s", headers={"Retry-After": str(wait)})
    _ADMIN_UNLOAD_LAST_MONO = now
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(config.LMSTUDIO_HOST.rstrip("/") + "/unload")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("admin_unload_failed | ip=%s | err=%s", ip, exc)
        raise HTTPException(502, f"Impossibile scaricare i modelli tramite llama-swap: {exc}") from exc
    try:
        upstream = response.json()
    except ValueError:
        upstream = response.text
    logger.info("admin_unload_success | ip=%s | upstream_status=%s", ip, response.status_code)
    return {"status": "ok", "upstream": upstream}


if config.PULL_RESTART_HTTP_KEY:

    @app.post(
        "/admin/pull-restart",
        response_model=PullRestartAcceptedResponse,
        status_code=202,
        include_in_schema=False,
        summary="Git pull (main) e riavvio processo server",
        description=(
            "Accoda uno script che attende qualche secondo, esegue `git pull` sul repo, "
            "termina il processo in ascolto sulla PORT e riavvia `server.py`. "
            "Richiede token dedicato (non usa API_KEY). Stessi privilegi dell'utente che esegue uvicorn."
        ),
    )
    async def admin_pull_restart(
        ip: str = Depends(require_pull_restart_token),
    ) -> PullRestartAcceptedResponse:
        global _PULL_RESTART_LAST_MONO
        async with _PULL_RESTART_CD_LOCK:
            now = time.monotonic()
            cd = float(config.PULL_RESTART_COOLDOWN_S)
            if cd > 0 and (now - _PULL_RESTART_LAST_MONO) < cd:
                wait = max(1, int(cd - (now - _PULL_RESTART_LAST_MONO)))
                raise HTTPException(
                    status_code=429,
                    detail=f"Cooldown attivo: riprova tra {wait}s",
                )
            _PULL_RESTART_LAST_MONO = now
        try:
            schedule_pull_restart()
        except Exception as exc:
            logger.exception("admin_pull_restart_failed | ip=%s | err=%s", ip, exc)
            raise HTTPException(status_code=500, detail=f"Impossibile avviare il worker: {exc}") from exc
        logger.info("admin_pull_restart_accepted | ip=%s", ip)
        return PullRestartAcceptedResponse(
            message=(
                "Operazione accodata. Dopo pochi secondi: git pull, stop sulla porta "
                f"{config.PORT}, riavvio server. Log: logs/pull_restart_worker.log"
            ),
        )


# ---------------------------------------------------------------------------
# Endpoint debug (solo API key)
# ---------------------------------------------------------------------------


@app.get("/debug/logs")
async def debug_logs(
    n: int = 100,
    _ip: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Ultimi N log record in memoria (max 500). Per debug remoto."""
    return {"count": min(n, _mem_handler.MAX_RECORDS), "records": _mem_handler.get_records(min(n, 500))}


@app.get("/debug/requests")
async def debug_requests(
    n: int = 100,
    path: str | None = None,
    model: str | None = None,
    client_name: str | None = None,
    _ip: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Ultime richieste persistite su JSONL, con filtri opzionali."""
    if config.REQUESTS_LOG_PATH is None:
        raise HTTPException(status_code=404, detail="Audit log disabilitato")
    limit = max(1, min(n, 500))
    records = _tail_jsonl(config.REQUESTS_LOG_PATH, limit=limit * 5)
    if path:
        records = [r for r in records if str(r.get("path") or "") == path]
    if model:
        needle = model.lower()
        records = [r for r in records if needle in str(r.get("model") or "").lower()]
    if client_name:
        needle = client_name.lower()
        records = [
            r for r in records
            if needle in str((((r.get("client") or {}).get("application") or {}).get("resolved_name") or "")).lower()
        ]
    records = records[-limit:]
    return {"count": len(records), "path": str(config.REQUESTS_LOG_PATH), "records": records}


@app.post("/debug/probe")
async def debug_probe(
    request: Request,
    _ip: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Invia prompt test a LM Studio, ritorna elapsed_s e token count."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body deve essere un oggetto JSON")
    prompt: str = str(body.get("prompt") or "Rispondi con 'ok'.")
    model_override: str = str(body.get("model") or "")
    if not model_override:
        try:
            models = await lmstudio_client.list_models()
            model_override = _strip_lmstudio_prefix(models[0]["id"]) if models else "default"
        except Exception:
            model_override = "default"
    lms_body: dict[str, Any] = {
        "model": model_override,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        result = await lmstudio_client.chat(lms_body)
        elapsed = round(time.monotonic() - t0, 2)
        choices = result.get("choices") or [{}]
        content = ((choices[0].get("message") or {}).get("content") or "")[:300]
        usage = result.get("usage") or {}
        return {
            "ok": True,
            "elapsed_s": elapsed,
            "model": model_override,
            "content": content,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
    except Exception as exc:
        return {"ok": False, "elapsed_s": round(time.monotonic() - t0, 2), "error": str(exc)}


@app.get("/dashboard/data")
async def dashboard_data(
    recent: int = 60,
    _ip: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Dati JSON per la dashboard: host+modelli, ultime inferenze, serie temporali."""
    now_epoch = time.time()
    hosts = await _dashboard_hosts()

    records: list[dict[str, Any]] = []
    if config.REQUESTS_LOG_PATH is not None:
        records = _tail_jsonl(config.REQUESTS_LOG_PATH, limit=20000)

    recent_limit = max(1, min(recent, 500))
    return {
        "server_name": config.SERVER_NAME,
        "generated_at": _iso_utc(now_epoch),
        "now_epoch": int(now_epoch),
        "audit_enabled": config.REQUESTS_LOG_PATH is not None,
        "gpu": config.GPU_INFO or None,
        "hosts": hosts,
        "recent": _dashboard_recent(records, limit=recent_limit),
        "series": _dashboard_series(records, now_epoch),
    }


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    """Pagina di monitoraggio (host, ultime inferenze, grafico multilinea).

    La pagina e' pubblica ma carica i dati da /dashboard/data (protetto da
    API key). La chiave viene inserita una volta e salvata in localStorage.
    """
    return HTMLResponse(content=_DASHBOARD_HTML)


@app.get("/llm/context")
async def llm_context(
    request: Request,
    format: str = "json",
    _ip: str = Depends(require_api_key),
):
    """Contesto operativo di 10b pensato per un LLM che orchestra le chiamate.

    - format=json (default): payload strutturato (host/modelli/capabilities/uso/routing).
    - format=text|md: markdown compatto pronto da iniettare in un system prompt.
    """
    payload = await _llm_context_payload()
    fmt = str(format or "json").strip().lower()
    if fmt in ("text", "md", "markdown"):
        text = _render_llm_context_md(payload)
        _audit_update(request, response_payload={"format": "markdown", "models": len(payload["models"])})
        return Response(content=text, media_type="text/markdown; charset=utf-8")
    _audit_update(request, response_payload={"format": "json", "models": len(payload["models"])})
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Endpoint gateway OpenAI-compat (dal progetto 10)
# ---------------------------------------------------------------------------


async def _models_with_runtime(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggiunge lo stato corrente senza memorizzarlo nella cache del catalogo."""
    import ollama_client

    async def leggi(client, percorso, campo):
        try:
            r = await client.get(percorso, timeout=10.0)
            r.raise_for_status()
            items = r.json()[campo]
            if not isinstance(items, list):
                raise ValueError("Elenco runtime non valido")
            return items
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    async def stato_lmstudio():
        swap = config.LMSTUDIO_PROXY_PURE
        items = await leggi(lmstudio_client.get_meta_client(), "/running" if swap else "/api/v0/models", "running" if swap else "data")
        if items is None:
            return None
        return {str(m.get("model") if swap else m.get("id")): m for m in items
                if isinstance(m, dict) and m.get("state") == ("ready" if swap else "loaded")}

    async def stato_ollama():
        items = await leggi(ollama_client.get_client(), "/api/ps", "models")
        return None if items is None else {str(m.get("name") or m.get("model")): m for m in items if isinstance(m, dict)}

    async def assente():
        return None

    lm, ollama = await asyncio.gather(
        stato_lmstudio() if any(m["id"].startswith(_LMSTUDIO_PREFIX) for m in data) else assente(),
        stato_ollama() if any(not m["id"].startswith((_LMSTUDIO_PREFIX, "decider:")) for m in data) else assente(),
    )
    out = []
    for model in data:
        mid = model["id"]
        runtime = lm if mid.startswith(_LMSTUDIO_PREFIX) else (None if mid.startswith("decider:") else ollama)
        bare = mid.removeprefix(_LMSTUDIO_PREFIX)
        loaded = None if runtime is None else bare in runtime
        item = {**model, "loaded": loaded, "state": "unknown" if loaded is None else ("loaded" if loaded else "not-loaded")}
        if loaded:
            details = runtime[bare]
            item["context_length"] = details.get("loaded_context_length") or details.get("context_length") or model.get("context_length")
        out.append(item)
    return out


@app.get("/v1/models")
async def list_models(request: Request, _ip: str = Depends(require_api_key), runtime: bool = False):
    data = await _get_models_cached()
    lmstudio_ok = bool(_MODELS_CACHE.get("lmstudio_ok"))
    # Guardia: con LM Studio abilitato ma irraggiungibile NON restituire mai
    # HTTP 200 + data:[] (sembra sano a chi guarda solo lo status code).
    if config.LMSTUDIO_ENABLED and not lmstudio_ok:
        payload = {
            "error": {
                "message": (
                    "LocalRouter degraded: LM Studio/llama-swap unreachable "
                    f"({config.LMSTUDIO_HOST})"
                ),
                "type": "degraded",
                "code": "lmstudio_unreachable",
                "lmstudio_ok": False,
                "lmstudio_host": config.LMSTUDIO_HOST,
                "detail": _MODELS_CACHE.get("lmstudio_error"),
            },
            "object": "list",
            "data": [],
        }
        _audit_update(
            request,
            response_payload={
                "count": 0,
                "models": [],
                "degraded": True,
                "lmstudio_ok": False,
                "error": payload["error"],
            },
        )
        return JSONResponse(status_code=503, content=payload)

    _audit_update(request, response_payload={"count": len(data), "models": [m["id"] for m in data]})
    if runtime:
        return {"object": "list", "data": await _models_with_runtime(data)}
    return ModelsResponse(data=[ModelEntry(**m) for m in data])


@app.post("/v1/models/refresh")
async def refresh_models(request: Request, _ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Invalida la cache modelli e forza un reload immediato da Ollama + LM Studio."""
    _MODELS_CACHE["at"] = 0.0
    _MODELS_CACHE["data"] = []
    _MODELS_CACHE["lmstudio_ok"] = False
    _MODELS_CACHE["lmstudio_error"] = None
    data = await _get_models_cached()
    lmstudio_ok = bool(_MODELS_CACHE.get("lmstudio_ok"))
    if config.LMSTUDIO_ENABLED and not lmstudio_ok:
        payload = {
            "ok": False,
            "degraded": True,
            "count": 0,
            "models": [],
            "lmstudio_ok": False,
            "error": {
                "message": (
                    "LocalRouter degraded: LM Studio/llama-swap unreachable "
                    f"({config.LMSTUDIO_HOST})"
                ),
                "type": "degraded",
                "code": "lmstudio_unreachable",
                "detail": _MODELS_CACHE.get("lmstudio_error"),
            },
        }
        _audit_update(request, response_payload=payload)
        return JSONResponse(status_code=503, content=payload)
    response = {
        "ok": True,
        "count": len(data),
        "models": [m["id"] for m in data],
        "lmstudio_ok": lmstudio_ok,
    }
    _audit_update(request, response_payload=response)
    return response


@app.post("/v1/models/unload")
async def unload_model_route(request: Request, model_id: str, _ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Scarica un modello LM Studio (libera VRAM/RAM). model_id come query param."""
    name = model_id.split("lmstudio:", 1)[-1]
    await lmstudio_client.unload_model(name)
    response = {"unloaded": name}
    _audit_update(
        request,
        model=f"lmstudio:{name}",
        request_payload={"model_id": model_id},
        request_items=_form_request_items({"model_id": model_id}),
        response_payload=response,
    )
    return response


@app.post("/v1/models/unload-all")
async def unload_all_route(request: Request, _ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Scarica TUTTI i modelli LM Studio caricati (libera VRAM/RAM). Best-effort."""
    try:
        loaded = await lmstudio_client.get_loaded_models_context()
    except Exception:
        loaded = {}
    out: list[str] = []
    for name in list(loaded.keys()):
        await lmstudio_client.unload_model(name)
        out.append(name)
    response = {"unloaded": out, "count": len(out)}
    _audit_update(request, response_payload=response)
    return response


@app.post("/v1/kv/prefisso")
async def kv_prefisso(req: KvPrefissoRequest, _ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Scalda llama-server fino alla fine esatta di ``fino_a``."""
    return await lmstudio_client.scalda_prefisso(
        req.model,
        [message.model_dump(exclude_none=True) for message in req.messages],
        req.tools,
        req.fino_a,
    )


@app.post("/v1/kv/slot")
async def kv_slot(req: KvSlotRequest, _ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Salva o ripristina lo slot 0 del llama-server scelto da llama-swap."""
    return await lmstudio_client.fotografia_slot(req.model, req.azione, req.nome)


@app.post("/v1/chat/jobs", response_model=ChatJobAcceptedResponse, status_code=202)
async def chat_jobs_submit(
    request: Request,
    ip: str = Depends(require_api_key),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    try:
        req = ChatCompletionRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request: {exc}")
    _rifiuta_url_remoti(req)
    _audit_update(
        request,
        route={"kind": "chat_job_submit", "backend": "lmstudio" if _is_lmstudio_model(req.model) else "ollama"},
        model=req.model,
        request_payload=_safe_json(payload),
        request_items=_chat_request_items(req),
    )
    if req.stream:
        raise HTTPException(status_code=400, detail="chat jobs async accettano solo stream=false")

    job_id = f"chatjob-{uuid.uuid4().hex}"
    now = _now_ts()
    base = {
        "job_id": job_id,
        "status": "queued",
        "model": req.model,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "request_id": None,
        "partial_text_len": 0,
    }
    async with _CHAT_JOBS_LOCK:
        # Prenota sotto lo stesso lock dell'inserimento: nessuna corsa fra submit.
        attivi = sum(j["status"] in {"queued", "running"} for j in _CHAT_JOBS.values())
        if attivi >= config.CHAT_JOB_MAX_ACTIVE:
            raise HTTPException(status_code=503, detail="Troppi job chat attivi")
        # Conserva solo lo storico piu' recente, senza espellere job attivi.
        for vecchio in list(_CHAT_JOBS):
            if len(_CHAT_JOBS) < config.CHAT_JOB_MAX_RETAINED:
                break
            if _CHAT_JOBS[vecchio]["status"] not in {"queued", "running"}:
                del _CHAT_JOBS[vecchio]
                _CHAT_JOB_EVENTS.pop(vecchio, None)
        _CHAT_JOBS[job_id] = base
        _CHAT_JOB_EVENTS[job_id] = asyncio.Queue(maxsize=200)
    await _chat_job_emit(job_id, "queued", {"job_id": job_id, "model": req.model})
    asyncio.create_task(_chat_job_worker(job_id, req, ip))
    response = ChatJobAcceptedResponse(job_id=job_id, status="queued")
    _audit_update(request, response_payload=response.model_dump())
    return response


@app.get("/v1/chat/jobs/{job_id}", response_model=ChatJobStatusResponse)
async def chat_jobs_status(
    job_id: str,
    _ip: str = Depends(require_api_key),
):
    async with _CHAT_JOBS_LOCK:
        job = dict(_CHAT_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")
    return ChatJobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        model=job["model"],
        created_at=int(job["created_at"]),
        updated_at=int(job["updated_at"]),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        error=job.get("error"),
        partial_text_len=int(job.get("partial_text_len") or 0),
        request_id=job.get("request_id"),
    )


@app.get("/v1/chat/jobs/{job_id}/result")
async def chat_jobs_result(
    job_id: str,
    _ip: str = Depends(require_api_key),
):
    async with _CHAT_JOBS_LOCK:
        job = dict(_CHAT_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")
    status = str(job.get("status") or "")
    if status in {"queued", "running"}:
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": status},
        )
    if status == "failed":
        return JSONResponse(
            status_code=502,
            content={"job_id": job_id, "status": "failed", "error": job.get("error")},
        )
    if status == "cancelled":
        return JSONResponse(
            status_code=499,
            content={"job_id": job_id, "status": "cancelled", "error": job.get("error") or "cancelled"},
        )
    return ChatJobResultResponse(job_id=job_id, status="completed", result=job.get("result") or {})


@app.get("/v1/chat/jobs/{job_id}/events")
async def chat_jobs_events(
    job_id: str,
    _ip: str = Depends(require_api_key),
):
    async with _CHAT_JOBS_LOCK:
        exists = job_id in _CHAT_JOBS
    if not exists:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")

    async def event_source() -> AsyncIterator[bytes]:
        q = _CHAT_JOB_EVENTS.get(job_id)
        if q is None:
            yield b"event: failed\ndata: {\"error\":\"events queue missing\"}\n\n"
            return
        while True:
            # La coda e' condivisa: un altro lettore puo' aver consumato l'evento
            # finale. Lo stato persistito chiude anche stream riaperti o espulsi.
            async with _CHAT_JOBS_LOCK:
                job = dict(_CHAT_JOBS.get(job_id) or {})
            stato = job.get("status") if job else "failed"
            if stato in {"completed", "failed", "cancelled"}:
                data = {"job_id": job_id, "request_id": job.get("request_id"),
                        "partial_text_len": job.get("partial_text_len", 0)}
                if stato != "completed":
                    data["error"] = job.get("error") or ("job expired" if not job else stato)
                yield f"event: {stato}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
                return
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15.0)
                payload = json.dumps(evt.get("data") or {}, ensure_ascii=False)
                event_name = str(evt.get("event") or "progress")
                yield f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
                if event_name in {"completed", "failed", "cancelled"}:
                    return
            except asyncio.TimeoutError:
                # heartbeat keepalive for tunnel/proxy
                yield b": heartbeat\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _lmstudio_response(request: Request, source: AsyncIterator[bytes], media_type: str) -> Response:
    """Aspetta il primo corpo prima di impegnare lo status HTTP."""
    try:
        first = await anext(source, b"")
    except LMStudioError as exc:
        return await _lmstudio_error_handler(request, exc)

    async def body() -> AsyncIterator[bytes]:
        try:
            yield first
            async for piece in source:
                yield piece
        finally:
            await source.aclose()

    return StreamingResponse(
        body(), media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    ip: str = Depends(require_api_key),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    try:
        req = ChatCompletionRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request: {exc}")
    _rifiuta_url_remoti(req)
    backend = "lmstudio" if _is_lmstudio_model(req.model) else "ollama"
    _audit_update(
        request,
        route={"kind": "chat_completion", "backend": backend},
        model=req.model,
        request_payload=_safe_json(payload),
        request_items=_chat_request_items(req),
    )

    # ── LM Studio branch ──────────────────────────────────────────────────
    if _is_lmstudio_model(req.model):
        real_model = _strip_lmstudio_prefix(req.model)
        if lmstudio_client.is_lmstudio_runtime_unsupported(real_model):
            detail = lmstudio_client.lmstudio_unsupported_detail(real_model)
            _audit_update(
                request,
                lmstudio_error={
                    "error_kind": "runtime_unsupported_architecture",
                    "detail": detail,
                    "model": real_model,
                },
            )
            raise HTTPException(status_code=501, detail=detail)
        _rifiuta_grammar_su_lmstudio_nativo(req)
        lms_body: dict[str, Any] = {
            "model": real_model,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": req.stream,
        }
        if req.temperature is not None:
            lms_body["temperature"] = float(req.temperature)
        if req.max_tokens is not None:
            lms_body["max_tokens"] = int(req.max_tokens)
        if req.top_p is not None:
            lms_body["top_p"] = float(req.top_p)
        if req.stop is not None:
            lms_body["stop"] = req.stop
        if req.tools:
            lms_body["tools"] = req.tools
        if req.response_format:
            lms_body["response_format"] = req.response_format
        if req.seed is not None:
            lms_body["seed"] = int(req.seed)
        if req.chat_template_kwargs is not None:
            lms_body["chat_template_kwargs"] = req.chat_template_kwargs
        _apply_thinking_budget(lms_body, req)
        _apply_parametri_llama(lms_body, req)

        logger.info(
            "chat_lmstudio | ip=%s | model=%s | stream=%s | msgs=%d | thinking_budget=%s",
            ip, real_model, bool(req.stream), len(lms_body["messages"]),
            lms_body.get("thinking_budget_tokens"),
        )
        retry_meta = _lmstudio_audit_retries(request)
        if not await lmstudio_client.ensure_model_context(real_model):
            err = lmstudio_client.get_last_load_error() or "unknown"
            _audit_update(
                request,
                lmstudio_retries=retry_meta,
                lmstudio_reload_failed=True,
                lmstudio_error={
                    "error_kind": "reload_failed",
                    "detail": err,
                    "model": real_model,
                },
            )
            raise HTTPException(
                status_code=502,
                detail=f"LM Studio reload failed for model {real_model}: {err}",
            )

        heartbeat_interval = float(config.NON_STREAM_HEARTBEAT_INTERVAL_S or 10.0)
        if heartbeat_interval <= 0:
            heartbeat_interval = 10.0

        if req.stream:
            async def lms_event_source() -> AsyncIterator[bytes]:
                completion_chunks = 0
                aiter = None
                pending = None
                sent = False
                try:
                    aiter = _lmstudio_chat_stream_call(
                        lms_body, req.model, retry_meta, request=request
                    ).__aiter__()
                    while True:
                        if await request.is_disconnected():
                            logger.warning("chat_client_disconnected_stream | stopping")
                            break
                        try:
                            if pending is None:
                                pending = asyncio.create_task(aiter.__anext__())
                            chunk = await asyncio.wait_for(
                                asyncio.shield(pending),
                                timeout=heartbeat_interval,
                            )
                            pending = None
                            completion_chunks += 1
                            sent = True
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                        except asyncio.TimeoutError:
                            sent = True
                            yield b": keepalive\n\n"
                        except StopAsyncIteration:
                            break
                    yield b"data: [DONE]\n\n"
                except LMStudioError as exc:
                    _audit_update(request, lmstudio_error=_lmstudio_error_audit(exc))
                    if not sent:
                        raise
                    err = {"error": {"message": str(exc), "type": exc.error_kind or "upstream_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    if pending is not None:
                        pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pending
                    if aiter is not None:
                        with contextlib.suppress(Exception):
                            await aiter.aclose()
                    _audit_update(
                        request,
                        response_payload={"stream": True, "chunks": completion_chunks, "backend": "lmstudio"},
                        lmstudio_retries=retry_meta,
                    )
            return await _lmstudio_response(
                request, lms_event_source(), "text/event-stream",
            )
        else:
            # Heartbeat whitespace: senza scrivere, is_disconnected non vede kill/tunnel.
            # Se il write fallisce o disconnect=true, cancelliamo l'upstream verso swap.
            async def lms_non_stream_with_heartbeat() -> AsyncIterator[bytes]:
                task = asyncio.create_task(
                    _lmstudio_chat_nonstream_call(
                        lms_body, req.model, retry_meta, request=request
                    )
                )
                last_hb = time.monotonic()
                sent = False
                try:
                    while not task.done():
                        if await request.is_disconnected():
                            logger.warning("chat_client_disconnected_lms_hb | cancelling")
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await task
                            return
                        now = time.monotonic()
                        if now - last_hb >= heartbeat_interval:
                            last_hb = now
                            try:
                                sent = True
                                yield b" "
                            except Exception:
                                logger.warning("chat_client_write_failed_lms_hb | cancelling")
                                task.cancel()
                                with contextlib.suppress(asyncio.CancelledError, Exception):
                                    await task
                                return
                        await asyncio.wait({task}, timeout=min(0.5, heartbeat_interval))
                    lms_data = await task
                    _audit_update(
                        request,
                        response_payload=_safe_json(lms_data),
                        lmstudio_retries=retry_meta,
                    )
                    yield json.dumps(lms_data, ensure_ascii=False).encode("utf-8")
                except LMStudioError as exc:
                    _audit_update(
                        request,
                        lmstudio_retries=retry_meta,
                        lmstudio_error=_lmstudio_error_audit(exc),
                    )
                    if not sent:
                        raise
                    yield json.dumps(
                        {"error": {"message": str(exc), "type": exc.error_kind or "upstream_error"}},
                        ensure_ascii=False,
                    ).encode("utf-8")
                except asyncio.CancelledError:
                    _audit_update(
                        request,
                        lmstudio_retries=retry_meta,
                        lmstudio_error={
                            "message": "chat cancelled",
                            "error_kind": "cancelled",
                        },
                    )
                    yield json.dumps(
                        {"error": {"message": "chat cancelled", "type": "cancelled"}},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    return
                except Exception as exc:  # noqa: BLE001
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                    # Se il task interno e' stato cancellato dal reaper, await alza CancelledError
                    # gia' gestito sopra; qui restano errori generici.
                    logger.exception("lms_non_stream_hb_failed | %s", exc)
                    yield json.dumps(
                        {"error": {"message": str(exc), "type": "server_error"}},
                        ensure_ascii=False,
                    ).encode("utf-8")
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

            return await _lmstudio_response(
                request, lms_non_stream_with_heartbeat(), "application/json",
            )
    # ── fine branch LM Studio ─────────────────────────────────────────────

    body = _build_ollama_body(req)
    started = time.monotonic()

    if body.get("tools") and not _model_supports_tools(req.model):
        logger.info("strip_tools_proactive | model=%s | tools=%d", req.model, len(body["tools"]))
        body.pop("tools", None)
    elif body.get("tools"):
        logger.info("tools_forwarded_ollama | model=%s | tools=%d", req.model, len(body["tools"]))

    msg_count = len(body.get("messages", []))
    logger.info(
        "chat_in | ip=%s | model=%s | stream=%s | msgs=%d | tools=%d",
        ip, req.model, bool(req.stream), msg_count, len(body.get("tools") or []),
    )

    if req.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        async def event_source() -> AsyncIterator[bytes]:
            first_chunk = True
            last_usage = {"prompt_eval_count": 0, "eval_count": 0}
            try:
                async for frame in chat_stream(body):
                    last_usage["prompt_eval_count"] = int(frame.get("prompt_eval_count") or last_usage["prompt_eval_count"])
                    last_usage["eval_count"] = int(frame.get("eval_count") or last_usage["eval_count"])
                    message = frame.get("message") or {}
                    delta: dict[str, Any] = {}
                    if first_chunk:
                        delta["role"] = message.get("role") or "assistant"
                        first_chunk = False
                    text_piece = str(message.get("content") or message.get("thinking") or "")
                    if text_piece:
                        delta["content"] = text_piece
                    if message.get("tool_calls"):
                        openai_msg = _ollama_message_to_openai(message)
                        delta["tool_calls"] = openai_msg.get("tool_calls")
                    if not frame.get("done"):
                        chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": req.model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                    else:
                        finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
                        final_chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": req.model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                            "usage": {
                                "prompt_tokens": last_usage["prompt_eval_count"],
                                "completion_tokens": last_usage["eval_count"],
                                "total_tokens": last_usage["prompt_eval_count"] + last_usage["eval_count"],
                            },
                        }
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            except OllamaError as exc:
                err_payload = {"error": {"message": str(exc), "type": "upstream_error"}}
                yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            finally:
                _audit_update(
                    request,
                    response_payload={
                        "stream": True,
                        "prompt_tokens": last_usage["prompt_eval_count"],
                        "completion_tokens": last_usage["eval_count"],
                        "backend": "ollama",
                    },
                )

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    heartbeat_interval = float(config.NON_STREAM_HEARTBEAT_INTERVAL_S or 0)

    if heartbeat_interval <= 0:
        data = await chat(body)
        completion = _build_chat_completion(data, req.model)
        _audit_update(request, response_payload=_safe_json(completion))
        return JSONResponse(completion)

    async def non_stream_with_heartbeat() -> AsyncIterator[bytes]:
        accum_content = ""
        tool_calls_acc: list[dict[str, Any]] = []
        usage = {"prompt": 0, "completion": 0}
        finish_reason = "stop"
        last_heartbeat = time.monotonic()
        yielded_any = False

        async def heartbeat_if_needed() -> bytes | None:
            nonlocal last_heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                last_heartbeat = now
                return b" "
            return None

        effective_body = body

        async def iter_frames() -> AsyncIterator[dict[str, Any]]:
            nonlocal effective_body
            try:
                async for fr in chat_stream(effective_body):
                    yield fr
                    if fr.get("done"):
                        return
            except OllamaError as err:
                if _is_no_tools_error(err) and effective_body.get("tools"):
                    logger.warning("retry_without_tools | model=%s | err=%s", req.model, err)
                    retry_body = dict(effective_body)
                    retry_body.pop("tools", None)
                    effective_body = retry_body
                    _mark_model_no_tools(req.model)
                    async for fr in chat_stream(retry_body):
                        yield fr
                else:
                    raise

        try:
            async for frame in iter_frames():
                hb = await heartbeat_if_needed()
                if hb is not None:
                    yielded_any = True
                    yield hb
                message = frame.get("message") or {}
                piece = str(message.get("content") or message.get("thinking") or "")
                if piece:
                    accum_content += piece
                if message.get("tool_calls"):
                    tool_calls_acc = list(message["tool_calls"])
                if frame.get("prompt_eval_count"):
                    usage["prompt"] = int(frame["prompt_eval_count"])
                if frame.get("eval_count"):
                    usage["completion"] = int(frame["eval_count"])
                if frame.get("done"):
                    if tool_calls_acc:
                        finish_reason = "tool_calls"
                    final_message: dict[str, Any] = {"role": "assistant", "content": accum_content}
                    if tool_calls_acc:
                        final_message["tool_calls"] = tool_calls_acc
                    final_payload = {
                        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion",
                        "created": int(time.time()), "model": req.model,
                        "choices": [{"index": 0, "message": _ollama_message_to_openai(final_message), "finish_reason": finish_reason}],
                        "usage": {
                            "prompt_tokens": usage["prompt"],
                            "completion_tokens": usage["completion"],
                            "total_tokens": usage["prompt"] + usage["completion"],
                        },
                    }
                    yield json.dumps(final_payload, ensure_ascii=False).encode("utf-8")
                    yielded_any = True
                    return
        except OllamaError as exc:
            logger.warning("chat_completion_error | model=%s | err=%s", req.model, exc)
            err_payload = {"error": {"message": str(exc), "type": "upstream_error"}}
            if yielded_any:
                yield b"\n"
            yield json.dumps(err_payload, ensure_ascii=False).encode("utf-8")
        finally:
            _audit_update(
                request,
                response_payload={
                    "stream": False,
                    "finish_reason": finish_reason,
                    "prompt_tokens": usage["prompt"],
                    "completion_tokens": usage["completion"],
                    "content_preview": accum_content[:500],
                    "tool_calls": tool_calls_acc,
                    "backend": "ollama",
                },
            )

    return StreamingResponse(
        non_stream_with_heartbeat(),
        media_type="application/json",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Endpoint OCR (nuovi)
# ---------------------------------------------------------------------------


def _parse_ocr_max_pages(raw: str) -> int | None:
    """max_pages opzionale su POST /process; vuoto = usa MAX_PAGES da config."""

    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="max_pages deve essere un intero") from exc
    if value < 1:
        raise HTTPException(status_code=400, detail="max_pages deve essere >= 1")
    if value > config.MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"max_pages non può superare MAX_PAGES ({config.MAX_PAGES})",
        )
    return value


@app.post("/process", response_model=OcrJobResponse)
async def process_file(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default="json"),
    skip_cache: str = Form(default="0"),
    max_pages: str = Form(default=""),
    ip: str = Depends(require_api_key),
):
    """Accetta un file (PDF o immagine) e accoda un job OCR.

    Il job resta `queued` finche' un worker libero non lo preleva dalla coda
    (concorrenza OCR_MAX_CONCURRENT_JOBS, default 1).

    Compatibile con il client in vision.py di 09B:
      files={"file": (name, bytes, mime)}
      data={"mode": "json", "skip_cache": "1", "max_pages": "10"}
    """

    mime = str(file.content_type or "").strip().lower()
    filename = str(file.filename or "upload").strip()

    # Fallback MIME da estensione
    if not mime or mime == "application/octet-stream":
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        mime_map = {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }
        mime = mime_map.get(ext, mime or "application/octet-stream")

    if mime not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo file non supportato: {mime}. Supportati: {sorted(ALLOWED_MIMES)}",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="File vuoto")

    parsed_max_pages = _parse_ocr_max_pages(max_pages)
    _audit_update(
        request,
        route={"kind": "ocr_process"},
        request_payload={
            "mode": mode,
            "skip_cache": skip_cache,
            "max_pages": parsed_max_pages if parsed_max_pages is not None else config.MAX_PAGES,
            "file": {"filename": filename, "mime": mime, "size_bytes": len(raw_bytes)},
        },
        request_items=_form_request_items(
            {
                "mode": mode,
                "skip_cache": skip_cache,
                "max_pages": parsed_max_pages if parsed_max_pages is not None else config.MAX_PAGES,
                "filename": filename,
                "mime": mime,
                "size_bytes": len(raw_bytes),
            }
        ),
    )

    job_id = uuid.uuid4().hex
    store = get_store()
    await store.create(job_id, filename, mime)

    sched = get_scheduler()
    try:
        sched.submit_nowait(
            OcrWorkItem(
                job_id=job_id,
                raw_bytes=raw_bytes,
                filename=filename,
                mime=mime,
                max_pages=parsed_max_pages,
            )
        )
    except asyncio.QueueFull:
        await store.remove(job_id)
        raise HTTPException(
            status_code=503,
            detail=(
                "Coda OCR piena: aumenta OCR_QUEUE_MAX_SIZE o attendi. "
                f"max_backlog={sched.queue.maxsize}"
            ),
        )

    logger.info(
        "ocr_process_accepted | ip=%s | job_id=%s | file=%s | mime=%s | size=%d | max_pages=%s | qsize=%s",
        ip,
        job_id,
        filename,
        mime,
        len(raw_bytes),
        parsed_max_pages if parsed_max_pages is not None else config.MAX_PAGES,
        sched.queue.qsize(),
    )
    response = OcrJobResponse(job_id=job_id)
    _audit_update(request, response_payload=response.model_dump())
    return response


@app.post("/cancel/{job_id}", response_model=OcrCancelResponse)
async def cancel_job(job_id: str, ip: str = Depends(require_api_key)):
    """Annulla un job OCR in coda o richiede stop per un job in esecuzione."""

    if not config.OCR_ALLOW_CANCEL:
        raise HTTPException(status_code=403, detail="OCR cancel disabilitato (OCR_ALLOW_CANCEL=0)")
    store = get_store()
    res = await store.request_cancel(job_id)
    if res.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=str(res.get("reason", "cancel_failed")))
    return OcrCancelResponse(
        ok=True,
        job_id=job_id,
        message=res.get("note") or "ok",
        previous=str(res.get("previous")) if res.get("previous") is not None else None,
        state=str(res.get("state")) if res.get("state") is not None else None,
        cancel_requested=bool(res.get("cancel_requested")) if res.get("cancel_requested") is not None else None,
    )


@app.get("/jobs", response_model=OcrJobsListResponse)
async def list_ocr_jobs(_ip: str = Depends(require_api_key)):
    """Elenco sintetico job OCR recenti (in-memory)."""

    snap = await get_store().list_jobs_snapshot(limit=500)
    return OcrJobsListResponse(jobs=snap)


@app.post("/cancel-all", response_model=OcrCancelAllResponse)
async def cancel_all_ocr_jobs(ip: str = Depends(require_api_key)):
    """Cancella tutti i queued; imposta cancel_requested sui running."""

    if not config.OCR_ALLOW_CANCEL:
        raise HTTPException(status_code=403, detail="OCR cancel disabilitato (OCR_ALLOW_CANCEL=0)")
    agg = await get_store().cancel_all()
    logger.info(
        "ocr_cancel_all | ip=%s | queued_cancelled=%d | running_requested=%d",
        ip,
        agg["queued_cancelled"],
        agg["running_cancel_requested"],
    )
    return OcrCancelAllResponse(
        ok=True,
        queued_cancelled=agg["queued_cancelled"],
        running_cancel_requested=agg["running_cancel_requested"],
    )


@app.get("/status/{job_id}", response_model=OcrStatusResponse)
async def job_status(job_id: str):
    """Restituisce lo stato del job OCR.

    vision.py (09) interpreta status: queued | running | done | error.
    Una cancellazione interna e' esposta come error + error=cancelled_by_user.
    """

    store = get_store()
    data = await store.get_status(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")

    progress = None
    if "progress" in data and data["progress"]:
        progress = OcrProgress(**data["progress"])

    api_status = data["status"]
    if api_status not in ("queued", "running", "done", "error"):
        api_status = "error"

    return OcrStatusResponse(
        job_id=data["job_id"],
        status=api_status,
        progress=progress,
        error=data.get("error"),
        cancel_requested=data.get("cancel_requested"),
        internal_status=data.get("internal_status"),
    )


@app.get("/result/{job_id}")
async def job_result(job_id: str):
    """Restituisce il risultato OCR.

    - 200 con OcrResultResponse se status == done.
    - 202 con {status, job_id} se il job non e' ancora completato
      (vision.py interpreta 202 come "non ancora pronto").
    - 404 se il job non esiste.
    - 200 con error se status == error.
    """

    store = get_store()
    data = await store.get_result(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")

    status = data["status"]

    if status in ("queued", "running"):
        return JSONResponse(
            status_code=202,
            content={"status": status, "job_id": job_id},
        )

    if status == "error":
        return JSONResponse(
            status_code=200,
            content={
                "job_id": job_id,
                "status": "error",
                "error": data.get("error") or "Errore sconosciuto",
                "blocks": [],
            },
        )

    # status == done
    blocks_raw = data.get("blocks") or []
    blocks = [
        OcrBlock(
            type=b.get("type", "text"),
            page=b.get("page", 1),
            content=b.get("content") or b.get("text") or "",
            latex=b.get("latex"),
            text=b.get("text") or b.get("content") or "",
        )
        for b in blocks_raw
    ]

    pages_detail = data.get("pages_detail") or []
    num_pages = data.get("num_pages") or max((b.page for b in blocks), default=1) if blocks else 1

    result = OcrResultResponse(
        job_id=job_id,
        status="done",
        model=config.OCR_MODEL,
        source=OcrSource(
            filename=data.get("filename") or "file",
            mime=data.get("mime") or "unknown",
            num_pages=num_pages,
        ),
        blocks=blocks,
        pages=pages_detail,
    )

    return JSONResponse(content=result.model_dump())


# ---------------------------------------------------------------------------
# Endpoint Media Enhancement (opzionale — MEDIA_ENABLED=1, SUPIR-v0F)
# ---------------------------------------------------------------------------


def _media_not_enabled() -> None:
    if not config.MEDIA_ENABLED:
        raise HTTPException(status_code=404, detail="Modulo media disabilitato (MEDIA_ENABLED=0)")


def _parse_media_mode(mode: str) -> str:
    normalized = str(mode or "restore+upscale").strip().lower()
    allowed = {"restore", "upscale", "restore+upscale", "enhance", "supir"}
    if normalized not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"mode non valido: {mode!r}. Valori: {sorted(allowed)}",
        )
    return normalized


def _parse_media_upscale(raw: str) -> int:
    try:
        value = int(str(raw or config.MEDIA_DEFAULT_UPSCALE).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="upscale deve essere 2 o 4") from exc
    if value not in (2, 4):
        raise HTTPException(status_code=400, detail="upscale deve essere 2 o 4")
    return value


def _parse_media_fidelity(raw: str) -> float:
    try:
        value = float(str(raw or config.MEDIA_DEFAULT_FIDELITY).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="fidelity deve essere un float 0-1") from exc
    return max(0.0, min(1.0, value))


def _parse_face_only(raw: str) -> bool:
    return str(raw or "0").strip().lower() in ("1", "true", "yes", "on")


def _wants_json_result(request: Request, format_q: str | None = None) -> bool:
    if format_q and str(format_q).lower() == "json":
        return True
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return True
    if config.MEDIA_RESULT_FORMAT == "json":
        return True
    if config.MEDIA_RESULT_FORMAT == "png":
        return False
    return False


@app.post("/media/process", response_model=MediaJobResponse)
async def media_process_file(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default="restore+upscale"),
    upscale: str = Form(default="2"),
    fidelity: str = Form(default="0.85"),
    face_only: str = Form(default="0"),
    ip: str = Depends(require_api_key),
):
    _media_not_enabled()

    from media.media_pipeline import ALLOWED_MIMES
    from media.media_scheduler import MediaWorkItem, get_media_scheduler
    from media.media_job_store import get_media_store

    mime = str(file.content_type or "").strip().lower()
    filename = str(file.filename or "upload").strip()
    if not mime or mime == "application/octet-stream":
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        mime_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }
        mime = mime_map.get(ext, mime or "application/octet-stream")

    if mime not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo immagine non supportato: {mime}. Supportati: {sorted(ALLOWED_MIMES)}",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="File vuoto")
    if len(raw_bytes) > config.MEDIA_MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File troppo grande (max {config.MEDIA_MAX_INPUT_BYTES} bytes)",
        )

    parsed_mode = _parse_media_mode(mode)
    parsed_upscale = _parse_media_upscale(upscale)
    parsed_fidelity = _parse_media_fidelity(fidelity)
    parsed_face_only = _parse_face_only(face_only)
    _audit_update(
        request,
        route={"kind": "media_process"},
        request_payload={
            "mode": parsed_mode,
            "upscale": parsed_upscale,
            "fidelity": parsed_fidelity,
            "face_only": parsed_face_only,
            "file": {"filename": filename, "mime": mime, "size_bytes": len(raw_bytes)},
        },
        request_items=_form_request_items(
            {
                "mode": parsed_mode,
                "upscale": parsed_upscale,
                "fidelity": parsed_fidelity,
                "face_only": parsed_face_only,
                "filename": filename,
                "mime": mime,
                "size_bytes": len(raw_bytes),
            }
        ),
    )

    job_id = uuid.uuid4().hex
    store = get_media_store()
    await store.create(
        job_id,
        filename,
        mime,
        parsed_mode,
        parsed_upscale,
        parsed_fidelity,
        parsed_face_only,
    )

    sched = get_media_scheduler()
    try:
        sched.submit_nowait(
            MediaWorkItem(
                job_id=job_id,
                raw_bytes=raw_bytes,
                filename=filename,
                mime=mime,
                mode=parsed_mode,
                upscale=parsed_upscale,
                fidelity=parsed_fidelity,
                face_only=parsed_face_only,
            )
        )
    except asyncio.QueueFull:
        await store.remove(job_id)
        raise HTTPException(
            status_code=503,
            detail=(
                "Coda media piena: aumenta MEDIA_QUEUE_MAX_SIZE o attendi. "
                f"max_backlog={sched.queue.maxsize}"
            ),
        )

    logger.info(
        "media_process_accepted | ip=%s | job_id=%s | file=%s | engine=SUPIR | upscale=%d",
        ip,
        job_id,
        filename,
        parsed_upscale,
    )
    response = MediaJobResponse(job_id=job_id)
    _audit_update(request, response_payload=response.model_dump())
    return response


@app.post("/media/cancel/{job_id}", response_model=MediaCancelResponse)
async def media_cancel_job(job_id: str, ip: str = Depends(require_api_key)):
    _media_not_enabled()
    if not config.MEDIA_ALLOW_CANCEL:
        raise HTTPException(status_code=403, detail="Media cancel disabilitato (MEDIA_ALLOW_CANCEL=0)")

    from media.media_job_store import get_media_store

    res = await get_media_store().request_cancel(job_id)
    if res.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=str(res.get("reason", "cancel_failed")))
    return MediaCancelResponse(
        ok=True,
        job_id=job_id,
        message=res.get("note") or "ok",
        previous=str(res.get("previous")) if res.get("previous") is not None else None,
        state=str(res.get("state")) if res.get("state") is not None else None,
        cancel_requested=bool(res.get("cancel_requested")) if res.get("cancel_requested") is not None else None,
    )


@app.get("/media/jobs", response_model=MediaJobsListResponse)
async def list_media_jobs(_ip: str = Depends(require_api_key)):
    _media_not_enabled()
    from media.media_job_store import get_media_store

    snap = await get_media_store().list_jobs_snapshot(limit=500)
    return MediaJobsListResponse(jobs=snap)


@app.post("/media/cancel-all", response_model=MediaCancelAllResponse)
async def cancel_all_media_jobs(ip: str = Depends(require_api_key)):
    _media_not_enabled()
    if not config.MEDIA_ALLOW_CANCEL:
        raise HTTPException(status_code=403, detail="Media cancel disabilitato (MEDIA_ALLOW_CANCEL=0)")

    from media.media_job_store import get_media_store

    agg = await get_media_store().cancel_all()
    logger.info(
        "media_cancel_all | ip=%s | queued_cancelled=%d | running_requested=%d",
        ip,
        agg["queued_cancelled"],
        agg["running_cancel_requested"],
    )
    return MediaCancelAllResponse(
        ok=True,
        queued_cancelled=agg["queued_cancelled"],
        running_cancel_requested=agg["running_cancel_requested"],
    )


@app.get("/media/status/{job_id}", response_model=MediaStatusResponse)
async def media_job_status(job_id: str):
    _media_not_enabled()
    from media.media_job_store import get_media_store

    data = await get_media_store().get_status(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")

    api_status = data["status"]
    if api_status not in ("queued", "running", "done", "error"):
        api_status = "error"

    return MediaStatusResponse(
        job_id=data["job_id"],
        status=api_status,
        mode=data.get("mode"),
        upscale=data.get("upscale"),
        width=data.get("width"),
        height=data.get("height"),
        error=data.get("error"),
        cancel_requested=data.get("cancel_requested"),
        internal_status=data.get("internal_status"),
    )


@app.get("/media/result/{job_id}")
async def media_job_result(job_id: str, request: Request, format: str | None = None):
    _media_not_enabled()
    from media.media_job_store import get_media_store

    store = get_media_store()
    meta = await store.get_result_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Job non trovato: {job_id}")

    status = meta["status"]
    if status in ("queued", "running"):
        return JSONResponse(status_code=202, content={"status": status, "job_id": job_id})

    if status == "error":
        return JSONResponse(
            status_code=200,
            content={
                "job_id": job_id,
                "status": "error",
                "error": meta.get("error") or "Errore sconosciuto",
            },
        )

    png_bytes = await store.read_result_bytes(job_id)
    if not png_bytes:
        raise HTTPException(status_code=500, detail="Risultato media non disponibile")

    if _wants_json_result(request, format):
        payload = MediaResultJsonResponse(
            job_id=job_id,
            mime="image/png",
            image_base64=base64.b64encode(png_bytes).decode("ascii"),
            width=int(meta.get("width") or 0),
            height=int(meta.get("height") or 0),
            mode=str(meta.get("mode") or ""),
            upscale=int(meta.get("upscale") or 2),
            fidelity=float(meta.get("fidelity") or 0.85),
            face_only=bool(meta.get("face_only")),
        )
        return JSONResponse(content=payload.model_dump())

    return Response(content=png_bytes, media_type="image/png")


# ---------------------------------------------------------------------------
# Endpoint Whisper (trascrizione audio — opzionale, WHISPER_ENABLED=1)
# + inoltro a speaches se VOCE_URL e' impostato
# ---------------------------------------------------------------------------


def _voce_http_client() -> httpx.AsyncClient:
    """Client HTTP verso speaches. I test lo sostituiscono con MockTransport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(config.VOCE_TIMEOUT_S))


async def _inoltra_voce_multipart(
    percorso: str,
    file: UploadFile,
    campi: dict[str, str],
) -> Response:
    """Inoltra multipart (file + form) a VOCE_URL+percorso. Errore remoto -> 502."""
    url = f"{config.VOCE_URL}{percorso}"
    raw = await file.read()
    filename = str(file.filename or "audio").strip() or "audio"
    content_type = file.content_type or "application/octet-stream"
    data = {k: v for k, v in campi.items() if v is not None and str(v) != ""}
    files = {"file": (filename, raw, content_type)}
    try:
        async with _voce_http_client() as client:
            remoto = await client.post(url, data=data, files=files)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Servizio voce non raggiungibile ({config.VOCE_URL}): {exc}",
        ) from exc
    if remoto.is_error:
        anteprima = (remoto.text or "")[:500]
        raise HTTPException(
            status_code=502,
            detail=f"Servizio voce ha risposto {remoto.status_code}: {anteprima}",
        )
    media = remoto.headers.get("content-type") or "application/json"
    return Response(content=remoto.content, media_type=media, status_code=remoto.status_code)


async def _inoltra_voce_speech(body: dict[str, Any]) -> Response:
    """Inoltra JSON TTS a VOCE_URL/v1/audio/speech. Errore remoto -> 502."""
    url = f"{config.VOCE_URL}/v1/audio/speech"
    try:
        async with _voce_http_client() as client:
            remoto = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Servizio voce non raggiungibile ({config.VOCE_URL}): {exc}",
        ) from exc
    if remoto.is_error:
        anteprima = (remoto.text or "")[:500]
        raise HTTPException(
            status_code=502,
            detail=f"Servizio voce ha risposto {remoto.status_code}: {anteprima}",
        )
    media = remoto.headers.get("content-type") or "audio/wav"
    return Response(content=remoto.content, media_type=media, status_code=remoto.status_code)


def _whisper_not_enabled() -> None:
    if not config.WHISPER_ENABLED:
        raise HTTPException(
            status_code=404, detail="Modulo Whisper disabilitato (WHISPER_ENABLED=0)"
        )


@app.post("/whisper/unload-llms")
async def whisper_unload_llms(_ip: str = Depends(require_api_key)) -> dict[str, Any]:
    """Scarica tutti i modelli LM Studio dalla VRAM prima di Whisper su GPU.

    Equivalente a POST /v1/models/unload-all, esposto nel namespace whisper
  per liberare la 3060 prima di large-v3 (~3-4 GB VRAM).
    """
    _whisper_not_enabled()
    from transcribe.whisper_admin import unload_lmstudio_llms

    return await unload_lmstudio_llms()


@app.post("/whisper/download-model")
async def whisper_download_model(
    model: str = "",
    device: str = "",
    compute_type: str = "",
    _ip: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Prefetch modello Whisper in WHISPER_MODELS_DIR (es. D:\\Model\\whisper, ~3 GB per large-v3).

    Parametri query opzionali: model, device, compute_type.
    Default da .env (WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE).
    """
    _whisper_not_enabled()
    from transcribe.whisper_admin import WhisperAdminError, download_model

    if not all(re.fullmatch(r"[A-Za-z0-9._-]*", v) for v in (model, device, compute_type)):
        raise HTTPException(status_code=400, detail="Parametri non validi")

    try:
        return await download_model(
            model or None,
            device=device or None,
            compute_type=compute_type or None,
        )
    except WhisperAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _do_transcription(
    request: Request,
    file: UploadFile,
    language: str,
    prompt: str,
    response_format: str,
    task: str,
):
    _whisper_not_enabled()

    from transcribe.whisper_pipeline import (
        WhisperBusyError,
        WhisperError,
        WhisperOptions,
        run_transcription,
    )

    raw_bytes = await file.read()
    filename = str(file.filename or "audio").strip()

    lang = (language or config.WHISPER_DEFAULT_LANGUAGE or "").strip() or None
    task_norm = (task or "transcribe").strip().lower()
    if task_norm not in ("transcribe", "translate"):
        raise HTTPException(status_code=400, detail="task deve essere transcribe o translate")

    fmt = (response_format or "json").strip().lower()
    want_segments = fmt in ("verbose_json", "srt", "vtt", "native")

    options = WhisperOptions(
        language=lang,
        task=task_norm,
        beam_size=config.WHISPER_BEAM_SIZE,
        vad_filter=config.WHISPER_VAD_FILTER,
        initial_prompt=(prompt or None),
        want_segments=want_segments,
    )

    try:
        result = await run_transcription(raw_bytes, filename, options)
    except WhisperBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WhisperError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _audit_update(
        request,
        route={"kind": "audio_transcription", "task": task_norm},
        request_payload={
            "language": lang,
            "prompt": prompt or None,
            "response_format": fmt,
            "task": task_norm,
            "file": {"filename": filename, "size_bytes": len(raw_bytes)},
        },
        request_items=_form_request_items(
            {
                "language": lang or "",
                "prompt": prompt or "",
                "response_format": fmt,
                "task": task_norm,
                "filename": filename,
                "size_bytes": len(raw_bytes),
            }
        ),
    )

    return result, fmt


def _fmt_ts_srt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600 * 1000)
    m, ms = divmod(ms, 60 * 1000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ts_vtt(seconds: float) -> str:
    return _fmt_ts_srt(seconds).replace(",", ".")


def _render_transcription(result, fmt: str):
    if fmt == "text":
        return Response(content=result.text + "\n", media_type="text/plain; charset=utf-8")

    if fmt == "srt":
        lines = []
        for i, seg in enumerate(result.segments, start=1):
            lines.append(str(i))
            lines.append(f"{_fmt_ts_srt(seg['start'])} --> {_fmt_ts_srt(seg['end'])}")
            lines.append(seg["text"].strip())
            lines.append("")
        return Response(content="\n".join(lines), media_type="text/plain; charset=utf-8")

    if fmt == "vtt":
        lines = ["WEBVTT", ""]
        for seg in result.segments:
            lines.append(f"{_fmt_ts_vtt(seg['start'])} --> {_fmt_ts_vtt(seg['end'])}")
            lines.append(seg["text"].strip())
            lines.append("")
        return Response(content="\n".join(lines), media_type="text/vtt; charset=utf-8")

    if fmt == "verbose_json":
        return JSONResponse(
            content={
                "task": "transcribe",
                "language": result.language,
                "duration": result.duration,
                "text": result.text,
                "segments": result.segments,
            }
        )

    # default: json
    return JSONResponse(content={"text": result.text})


@app.post("/v1/audio/transcriptions")
async def openai_audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(default=""),
    language: str = Form(default=""),
    prompt: str = Form(default=""),
    response_format: str = Form(default="json"),
    temperature: str = Form(default=""),
    _ip: str = Depends(require_api_key),
):
    """Trascrizione audio compatibile con OpenAI (Whisper o speaches via VOCE_URL).

    Form-data:
      - file: file audio (wav/mp3/m4a/ogg/flac/...) OBBLIGATORIO
      - model: con VOCE_URL default Systran/faster-whisper-small; con Whisper interno ignorato
      - language: ISO 639-1 (es. "it"); vuoto = autodetect
      - prompt: initial_prompt opzionale
      - response_format: json (default) | verbose_json | text | srt | vtt
    """
    if config.VOCE_URL:
        return await _inoltra_voce_multipart(
            "/v1/audio/transcriptions",
            file,
            {
                "model": (model or "").strip() or config.VOCE_STT_MODEL_DEFAULT,
                "language": language,
                "prompt": prompt,
                "response_format": response_format or "json",
                "temperature": temperature,
            },
        )
    result, fmt = await _do_transcription(request, file, language, prompt, response_format, "transcribe")
    rendered = _render_transcription(result, fmt)
    _audit_update(
        request,
        model=config.WHISPER_MODEL,
        response_payload={"format": fmt, "text_preview": result.text[:500], "language": result.language},
    )
    return rendered


@app.post("/v1/audio/translations")
async def openai_audio_translations(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(default=""),
    prompt: str = Form(default=""),
    response_format: str = Form(default="json"),
    temperature: str = Form(default=""),
    _ip: str = Depends(require_api_key),
):
    """Traduzione audio->inglese compatibile con OpenAI (Whisper task=translate)."""
    if config.VOCE_URL:
        return await _inoltra_voce_multipart(
            "/v1/audio/translations",
            file,
            {
                "model": (model or "").strip() or config.VOCE_STT_MODEL_DEFAULT,
                "prompt": prompt,
                "response_format": response_format or "json",
                "temperature": temperature,
            },
        )
    result, fmt = await _do_transcription(request, file, "", prompt, response_format, "translate")
    rendered = _render_transcription(result, fmt)
    _audit_update(
        request,
        model=config.WHISPER_MODEL,
        response_payload={"format": fmt, "text_preview": result.text[:500], "language": result.language},
    )
    return rendered


@app.post("/v1/audio/speech")
async def openai_audio_speech(
    request: Request,
    _ip: str = Depends(require_api_key),
):
    """Sintesi vocale OpenAI-compat via speaches (VOCE_URL).

    JSON: model, input, voice, response_format (default wav: sul 5060 non c'e' FFmpeg).
    Senza VOCE_URL: 404.
    """
    if not config.VOCE_URL:
        raise HTTPException(
            status_code=404,
            detail="Servizio voce non configurato (VOCE_URL vuoto)",
        )
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body JSON non valido") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body deve essere un oggetto JSON")
    payload = dict(body)
    if not str(payload.get("model") or "").strip():
        payload["model"] = config.VOCE_TTS_MODEL_DEFAULT
    if not str(payload.get("voice") or "").strip():
        payload["voice"] = config.VOCE_TTS_VOICE_DEFAULT
    if not str(payload.get("response_format") or "").strip():
        payload["response_format"] = config.VOCE_TTS_FORMAT_DEFAULT
    return await _inoltra_voce_speech(payload)


@app.post("/audio/transcribe")
async def audio_transcribe(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form(default=""),
    prompt: str = Form(default=""),
    response_format: str = Form(default="native"),
    task: str = Form(default="transcribe"),
    _ip: str = Depends(require_api_key),
):
    """Endpoint nativo di trascrizione (default JSON ricco: segmenti, timing, metriche).

    response_format: native (default) | json | text | srt | vtt | verbose_json
    """
    result, fmt = await _do_transcription(request, file, language, prompt, response_format, task)
    if fmt in ("text", "srt", "vtt", "verbose_json"):
        rendered = _render_transcription(result, fmt)
        _audit_update(
            request,
            model=config.WHISPER_MODEL,
            response_payload={"format": fmt, "text_preview": result.text[:500], "language": result.language},
        )
        return rendered
    payload = {
        "text": result.text,
        "language": result.language,
        "language_probability": result.language_probability,
        "duration": result.duration,
        "segments": result.segments,
        "model": config.WHISPER_MODEL,
        "device": config.WHISPER_DEVICE,
        "load_s": result.load_s,
        "transcribe_s": result.transcribe_s,
    }
    _audit_update(request, model=config.WHISPER_MODEL, response_payload=_safe_json(payload))
    return JSONResponse(content=payload)


# ---------------------------------------------------------------------------
# Decisori (proxy a DECIDER_URL — Laya + Decision-4B, un solo upstream)
# ---------------------------------------------------------------------------


def _decider_http_client() -> httpx.AsyncClient:
    """Client HTTP verso il server decisori. I test lo sostituiscono."""
    return httpx.AsyncClient(timeout=httpx.Timeout(config.DECIDER_TIMEOUT_S))


@app.post("/v1/decide")
async def decide_proxy(
    request: Request,
    _ip: str = Depends(require_api_key),
) -> JSONResponse:
    """Inoltra una decisione tipizzata al server FastAPI locale.

    Body JSON: {model, stato|state, opzioni|options, istruzioni|instructions}.
    Accetta model con o senza prefisso ``decider:``. Senza DECIDER_URL → 404.
    """
    if not config.DECIDER_URL:
        raise HTTPException(
            status_code=404,
            detail="Servizio decisori non configurato (DECIDER_URL vuoto)",
        )
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body JSON non valido") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body deve essere un oggetto JSON")

    payload = dict(body)
    raw_model = str(payload.get("model") or "").strip()
    if raw_model.startswith("decider:"):
        payload["model"] = raw_model.split(":", 1)[1]
    # Alias inglese → italiano (il server locale usa i campi italiani)
    if "stato" not in payload and "state" in payload:
        payload["stato"] = payload.pop("state")
    if "istruzioni" not in payload and "instructions" in payload:
        payload["istruzioni"] = payload.pop("instructions")
    if "opzioni" not in payload and "options" in payload:
        payload["opzioni"] = payload.pop("options")

    headers: dict[str, str] = {}
    if config.DECIDER_API_KEY:
        headers["Authorization"] = f"Bearer {config.DECIDER_API_KEY}"

    url = f"{config.DECIDER_URL}/v1/decide"
    try:
        async with _decider_http_client() as client:
            remoto = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Servizio decisori non raggiungibile ({config.DECIDER_URL}): {exc}",
        ) from exc

    _audit_update(
        request,
        route={"kind": "decide", "backend": "decider"},
        model=f"decider:{payload.get('model')}",
        request_payload=_safe_json(payload),
        response_payload={"status": remoto.status_code},
    )

    try:
        data = remoto.json()
    except Exception:
        anteprima = (remoto.text or "")[:500]
        raise HTTPException(
            status_code=502,
            detail=f"Decisore ha risposto {remoto.status_code} non-JSON: {anteprima}",
        )
    if remoto.is_error:
        raise HTTPException(status_code=remoto.status_code, detail=data)
    return JSONResponse(content=data, status_code=remoto.status_code)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    if config.MODO == "client":
        import cliente

        cliente.main()
        return

    uvicorn.run(
        "server:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
