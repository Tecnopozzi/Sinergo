"""Client httpx per LM Studio (OpenAI-compat su porta 1234).

Retry automatico su 502/503: LM Studio restituisce 502 mentre carica il modello
GGUF in VRAM (può richiedere 30-90 s su modelli grandi). Ritentare invece di
propagare l'errore evita fallback inutili su provider cloud a pagamento.

Nota: delay lineare (non esponenziale) per coprire meglio i cold-start lunghi.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from config import (
    LMSTUDIO_HOST,
    OLLAMA_TIMEOUT_S,
    OCR_TIMEOUT_S,
    lmstudio_model_config,
)
import config

logger = logging.getLogger("ocr_server")


class LMStudioError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        *,
        upstream_status: int | None = None,
        error_kind: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = upstream_status
        self.error_kind = error_kind
        self.detail = detail or message


_client: httpx.AsyncClient | None = None
# Client dedicato a list/health: non condivide il pool delle chat lunghe.
# Stanotte (13/09) /v1/models e' restato a timeout 10s per ~8h perche' il pool
# chat (max_connections=16) era saturo da stream bloccati; list_models non
# riusciva a prendere una connessione e il gateway serviva data:[].
_meta_client: httpx.AsyncClient | None = None
_context_guard = asyncio.Lock()
# Config applicata dall'ultimo load via CLI (l'API LM Studio non espone parallel).
_last_known_load: dict[str, tuple[int, Any, int]] = {}
_chat_slot_sem: asyncio.Semaphore | None = None
_chat_inflight: int = 0
_inflight_chats: dict[str, "InflightChat"] = {}
_inflight_lock = asyncio.Lock()
_reaper_task: asyncio.Task | None = None
_current_inflight_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lmstudio_inflight_id", default=None
)


@dataclass
class InflightChat:
    id: str
    task: asyncio.Task[Any] | None = None
    request: Any = None  # starlette Request opzionale
    started_mono: float = field(default_factory=time.monotonic)
    last_progress_mono: float = field(default_factory=time.monotonic)
    got_progress: bool = False
    cancel_reason: str | None = None


def _chat_http_timeout(timeout_s: float | None = None) -> httpx.Timeout:
    """Timeout chat: read = idle tra chunk SSE (chiude TCP se swap/zombie silenzioso)."""
    # override esplicito (OCR ecc.) mantiene il valore passato
    if timeout_s is not None:
        read_s = float(timeout_s)
    else:
        read_s = max(
            float(config.LMSTUDIO_CHAT_IDLE_S),
            float(config.LMSTUDIO_CHAT_TTFB_S),
        )
    return httpx.Timeout(
        connect=10.0,
        read=read_s,
        write=60.0,
        pool=float(config.LMSTUDIO_CHAT_POOL_TIMEOUT_S),
    )


def _chat_slots() -> asyncio.Semaphore:
    global _chat_slot_sem
    if _chat_slot_sem is None:
        _chat_slot_sem = asyncio.Semaphore(int(config.LMSTUDIO_CHAT_MAX_CONNECTIONS))
    return _chat_slot_sem


def _touch_inflight_progress() -> None:
    iid = _current_inflight_id.get()
    if not iid:
        return
    entry = _inflight_chats.get(iid)
    if entry is not None:
        entry.last_progress_mono = time.monotonic()
        entry.got_progress = True


async def _register_inflight(*, request: Any = None, task: asyncio.Task[Any] | None = None) -> str:
    iid = uuid.uuid4().hex
    entry = InflightChat(id=iid, task=task, request=request)
    async with _inflight_lock:
        _inflight_chats[iid] = entry
    _current_inflight_id.set(iid)
    return iid


async def _unregister_inflight(iid: str | None) -> None:
    if not iid:
        return
    async with _inflight_lock:
        _inflight_chats.pop(iid, None)
    if _current_inflight_id.get() == iid:
        _current_inflight_id.set(None)


def _bind_inflight_task(iid: str, task: asyncio.Task[Any]) -> None:
    entry = _inflight_chats.get(iid)
    if entry is not None:
        entry.task = task


async def cancel_inflight(iid: str, reason: str) -> bool:
    """Cancella una chat in volo (chiude lo stream httpx verso llama-swap)."""
    entry = _inflight_chats.get(iid)
    if entry is None:
        return False
    entry.cancel_reason = reason
    task = entry.task
    if task is not None and not task.done():
        task.cancel()
        logger.warning("lmstudio_inflight_cancel | id=%s | reason=%s", iid[:8], reason)
        return True
    return False


async def reap_inflight_chats(*, force_idle: bool = True) -> int:
    """Cancella chat zombie: client disconnected, idle SSE, o oltre TIMEOUT_S.

    Prima del primo chunk SSE vale TTFB_S (cold-start/prefill), non IDLE_S:
    altrimenti i densi vengono uccisi a 90s mentre il tunnel e' ancora vivo
    grazie all'heartbeat whitespace lato client.
    """
    now = time.monotonic()
    idle_s = float(config.LMSTUDIO_CHAT_IDLE_S)
    ttfb_s = float(config.LMSTUDIO_CHAT_TTFB_S)
    max_s = float(config.LMSTUDIO_CHAT_TIMEOUT_S)
    cancelled = 0
    snapshot = list(_inflight_chats.values())
    for entry in snapshot:
        reason = None
        if entry.request is not None:
            with contextlib.suppress(Exception):
                if await entry.request.is_disconnected():
                    reason = "client_disconnected"
        if reason is None and force_idle:
            if entry.got_progress:
                if (now - entry.last_progress_mono) >= idle_s:
                    reason = "upstream_idle"
            else:
                if (now - entry.started_mono) >= ttfb_s:
                    reason = "upstream_ttfb"
        if reason is None and (now - entry.started_mono) >= max_s:
            reason = "wall_timeout"
        if reason and await cancel_inflight(entry.id, reason):
            cancelled += 1
    if cancelled:
        logger.warning("lmstudio_reap | cancelled=%d | remaining=%d", cancelled, len(_inflight_chats))
    return cancelled


async def _reaper_loop() -> None:
    interval = float(config.LMSTUDIO_CHAT_REAPER_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(interval)
            n = await reap_inflight_chats()
            # Se restano TCP zombie senza task (raro), abbatte il client httpx.
            if n == 0 and _chat_inflight <= 0 and _client and not _client.is_closed:
                # non toccare se non c'e' evidenza di leak lato semaforo
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("lmstudio_reaper_error | %s", exc)


def start_chat_reaper() -> None:
    global _reaper_task
    if _reaper_task is not None and not _reaper_task.done():
        return
    _reaper_task = asyncio.create_task(_reaper_loop(), name="lmstudio-chat-reaper")
    logger.info(
        "lmstudio_reaper_started | idle_s=%s | ttfb_s=%s | max_s=%s",
        config.LMSTUDIO_CHAT_IDLE_S,
        config.LMSTUDIO_CHAT_TTFB_S,
        config.LMSTUDIO_CHAT_TIMEOUT_S,
    )


async def stop_chat_reaper() -> None:
    global _reaper_task
    if _reaper_task is None:
        return
    _reaper_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _reaper_task
    _reaper_task = None


@contextlib.asynccontextmanager
async def _chat_slot(*, request: Any = None) -> Any:
    """Prenota uno slot chat; se il pool e' pieno -> reap + retry, poi 503."""
    global _chat_inflight
    sem = _chat_slots()
    acquired = False
    try:
        await asyncio.wait_for(sem.acquire(), timeout=float(config.LMSTUDIO_CHAT_POOL_TIMEOUT_S))
        acquired = True
    except asyncio.TimeoutError:
        reaped = await reap_inflight_chats()
        if reaped:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(sem.acquire(), timeout=float(config.LMSTUDIO_CHAT_POOL_TIMEOUT_S))
                acquired = True
        if not acquired:
            # Ultima risorsa: abbatte tutte le TCP chat verso swap e azzera il semaforo.
            await reset_chat_http_client(reason="pool_saturated_reset")
            sem = _chat_slots()
            try:
                await asyncio.wait_for(sem.acquire(), timeout=float(config.LMSTUDIO_CHAT_POOL_TIMEOUT_S))
                acquired = True
            except asyncio.TimeoutError as exc:
                raise LMStudioError(
                    (
                        "LocalRouter degraded: chat pool saturated "
                        f"({config.LMSTUDIO_CHAT_MAX_CONNECTIONS} max, inflight={_chat_inflight})"
                    ),
                    status_code=503,
                    error_kind="pool_saturated",
                    detail=f"inflight={_chat_inflight}",
                ) from exc

    _chat_inflight += 1
    iid = await _register_inflight(request=request, task=asyncio.current_task())
    try:
        yield iid
    finally:
        await _unregister_inflight(iid)
        _chat_inflight = max(0, _chat_inflight - 1)
        sem.release()


def _raise_if_pool_saturated(exc: BaseException) -> None:
    if isinstance(exc, httpx.PoolTimeout):
        raise LMStudioError(
            (
                "LocalRouter degraded: chat pool saturated "
                f"(httpx PoolTimeout, max={config.LMSTUDIO_CHAT_MAX_CONNECTIONS})"
            ),
            status_code=503,
            error_kind="pool_saturated",
            detail=str(exc),
        ) from exc


def _raise_if_idle_timeout(exc: BaseException) -> None:
    if isinstance(exc, httpx.ReadTimeout):
        raise LMStudioError(
            (
                "LocalRouter: upstream idle timeout "
                f"(no SSE chunk for {config.LMSTUDIO_CHAT_IDLE_S:.0f}s / "
                f"ttfb {config.LMSTUDIO_CHAT_TTFB_S:.0f}s) — TCP verso llama-swap chiuso"
            ),
            status_code=504,
            error_kind="upstream_idle",
            detail=str(exc),
        ) from exc


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        max_conn = int(config.LMSTUDIO_CHAT_MAX_CONNECTIONS)
        _client = httpx.AsyncClient(
            base_url=LMSTUDIO_HOST,
            timeout=_chat_http_timeout(),
            limits=httpx.Limits(
                max_connections=max_conn,
                max_keepalive_connections=min(8, max_conn),
                keepalive_expiry=30.0,
            ),
        )
    return _client


def get_meta_client() -> httpx.AsyncClient:
    """HTTP client separato per /v1/models e health (pool corto, timeout stretti)."""
    global _meta_client
    if _meta_client is None or _meta_client.is_closed:
        _meta_client = httpx.AsyncClient(
            base_url=LMSTUDIO_HOST,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
    return _meta_client


async def close_client() -> None:
    global _client, _meta_client, _chat_slot_sem, _chat_inflight
    await stop_chat_reaper()
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
    if _meta_client and not _meta_client.is_closed:
        await _meta_client.aclose()
    _meta_client = None
    _chat_slot_sem = None
    _chat_inflight = 0
    _inflight_chats.clear()


async def reset_chat_http_client(*, reason: str = "manual") -> None:
    """Chiude il client chat httpx: abbatte TCP verso llama-swap e azzera il semaforo.

    Usare dopo disconnect di massa dei client (kill banco): altrimenti restano
    connessioni Established che fanno rispondere 429 allo swap e saturano il pool.
    """
    global _client, _chat_slot_sem, _chat_inflight
    # Cancella prima i task in volo cosi' rilasciano gli stream.
    for entry in list(_inflight_chats.values()):
        await cancel_inflight(entry.id, f"reset:{reason}")
    logger.warning(
        "lmstudio_chat_client_reset | reason=%s | inflight=%s",
        reason,
        _chat_inflight,
    )
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
    _chat_slot_sem = None
    _chat_inflight = 0
    _inflight_chats.clear()


def _is_loading_error(status_code: int) -> bool:
    """502/503 da LM Studio = modello ancora in caricamento, non errore permanente."""
    return status_code in (502, 503)


def _extract_error_detail(response: httpx.Response) -> str:
    with contextlib.suppress(Exception):
        err_val = (response.json() or {}).get("error")
        if isinstance(err_val, str):
            return err_val
        if isinstance(err_val, dict):
            return str(err_val.get("message") or "")
    with contextlib.suppress(Exception):
        return response.text[:400]
    return ""


def _is_transient_connection_error(exc: httpx.HTTPError) -> bool:
    """Connessione rifiutata/reset/timeout durante finestra post-generazione LM Studio.

    PoolTimeout NON e' transient: indica pool saturo -> 503 immediato.
    """
    if isinstance(exc, httpx.PoolTimeout):
        return False
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.CloseError,
        ),
    )


def _is_context_overflow_error(status_code: int, detail: str) -> bool:
    """Prompt supera n_ctx — non ritentare, restituire 413 al client."""
    lowered = (detail or "").lower()
    overflow_markers = (
        "exceed_context",
        "available context size",
        "context size has been exceeded",
        "exceeds the available context",
    )
    if any(m in lowered for m in overflow_markers):
        return True
    return status_code == 400 and "exceed" in lowered and "context" in lowered


def _is_transient_chat_error(status_code: int, detail: str) -> bool:
    """502/503/slot busy subito dopo una generazione — retry breve, non cold-start lungo.

    429 (Too many requests) NON e' transient: indica coda/swap saturo dopo disconnect
    dei client. Va esposto come 503 upstream_busy, non ritentato fino a 502.
    """
    if status_code == 429:
        return False
    if status_code in (502, 503):
        return True
    lowered = (detail or "").lower()
    if "too many requests" in lowered:
        return False
    keywords = (
        "busy",
        "model busy",
        "slot",
        "queue",
        "parallel",
        "overload",
        "capacity",
        "not ready",
        "try again",
        "in progress",
    )
    return any(k in lowered for k in keywords)


def _raise_if_upstream_busy(status_code: int, detail: str) -> None:
    """llama-swap/LM Studio 429 o coda piena → 503 chiaro (no hang, no falso 502)."""
    lowered = (detail or "").lower()
    if status_code == 429 or "too many requests" in lowered:
        raise LMStudioError(
            f"Upstream busy (HTTP {status_code}): {detail or 'Too many requests'}",
            status_code=503,
            upstream_status=status_code,
            error_kind="upstream_busy",
            detail=detail or "Too many requests",
        )


def _chat_transient_delay_for_attempt(attempt_index: int) -> float:
    """Delay crescente post-generazione: 0.4s, 0.8s, 1.2s …"""
    base = _chat_transient_delay_s()
    return base * float(min(attempt_index + 1, 3))


def _chat_transient_delay_s() -> float:
    return max(0.1, float(config.LMSTUDIO_CHAT_RETRY_DELAY_S or 0.4))


def _chat_transient_max_attempts() -> int:
    return max(1, int(config.LMSTUDIO_CHAT_RETRY_MAX or 3))


def _loading_retry_delay_s() -> float:
    """Delay lineare tra tentativi per cold-start LM Studio."""
    return max(0.5, float(config.LMSTUDIO_RETRY_DELAY_S or 5.0))


def _loading_retry_max_attempts() -> int:
    return max(0, int(config.LMSTUDIO_RETRY_MAX or 0))


def _record_chat_retry(
    retry_meta: list[dict[str, Any]] | None,
    *,
    phase: str,
    attempt: int,
    max_attempts: int,
    reason: str,
    delay_s: float,
    status_code: int | None = None,
    stream: bool = False,
) -> None:
    entry = {
        "phase": phase,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "reason": reason,
        "delay_s": delay_s,
        "status_code": status_code,
        "stream": stream,
    }
    if retry_meta is not None:
        retry_meta.append(entry)
    logger.warning(
        "lmstudio_chat_retry | phase=%s | attempt=%d/%d | reason=%s | status=%s | retry_in=%.2fs | stream=%s",
        phase,
        attempt,
        max_attempts,
        reason,
        status_code,
        delay_s,
        stream,
    )


async def list_models() -> list[dict[str, Any]]:
    """Restituisce lista modelli LM Studio nel formato OpenAI."""
    try:
        r = await get_meta_client().get("/v1/models", timeout=10.0)
        r.raise_for_status()
        return list((r.json() or {}).get("data") or [])
    except httpx.HTTPStatusError as exc:
        raise LMStudioError(
            f"LM Studio HTTP {exc.response.status_code}",
            status_code=502,
        ) from exc
    except httpx.HTTPError as exc:
        raise LMStudioError(f"LM Studio unreachable: {exc}", status_code=502) from exc


def _kv_model_path(model: str) -> str:
    name = str(model or "").removeprefix("lmstudio:").strip()
    if not name:
        raise LMStudioError("Modello KV mancante", status_code=502)
    return f"/upstream/{quote(name, safe='')}"


async def _kv_post(path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = await get_client().post(path, json=json_body, timeout=60.0)
        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            raise LMStudioError(
                f"llama-server KV HTTP {response.status_code}: {detail or 'errore upstream'}",
                status_code=502,
                upstream_status=response.status_code,
                error_kind="kv_upstream",
                detail=detail,
            )
        data = response.json()
        return data if isinstance(data, dict) else {"content": data}
    except LMStudioError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise LMStudioError(
            f"llama-server KV non raggiungibile: {exc}",
            status_code=502,
            error_kind="kv_connection",
        ) from exc


async def scalda_prefisso(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    fino_a: str,
) -> dict[str, Any]:
    """Renderizza il template, lo tronca alla fine esatta indicata e scalda la KV."""
    base = _kv_model_path(model)
    renderizzato = await _kv_post(
        base + "/apply-template",
        json_body={"messages": messages, "tools": tools, "add_generation_prompt": True},
    )
    prompt = renderizzato.get("prompt") or renderizzato.get("content")
    if not isinstance(prompt, str):
        raise LMStudioError("Risposta /apply-template senza prompt", status_code=502)
    fine = prompt.rfind(fino_a)
    if fine < 0:
        raise LMStudioError("Il testo fino_a non compare nel prompt renderizzato", status_code=502)
    prefisso = prompt[: fine + len(fino_a)]
    risultato = await _kv_post(
        base + "/completion",
        json_body={"prompt": prefisso, "n_predict": 0, "cache_prompt": True},
    )
    return {"ok": True, "caratteri": len(prefisso), "upstream": risultato}


async def fotografia_slot(model: str, azione: str, nome: str) -> dict[str, Any]:
    risultato = await _kv_post(
        _kv_model_path(model) + f"/slots/0?action={azione}",
        json_body={"filename": nome},
    )
    return {"ok": True, "azione": azione, "nome": nome, "upstream": risultato}


# Architetture GGUF che il runtime LM Studio (llama-server ufficiale) non carica.
# DiffusionGemma richiede llama-diffusion-cli (PR ggml-org/llama.cpp#24423), non ancora
# incluso in LM Studio 0.4.x. Vedi: https://avenchat.com/blog/does-diffusiongemma-work-in-lm-studio
_UNSUPPORTED_LMS_ARCH_MARKERS = (
    "diffusion-gemma",
    "diffusion_gemma",
    "diffusiongemma",
)


def is_lmstudio_runtime_unsupported(
    model_id: str,
    *,
    architecture: str | None = None,
) -> bool:
    """True se LM Studio/llama-server non puo' caricare il modello (arch non supportata)."""
    arch = (architecture or "").strip().lower().replace("_", "-")
    if arch and any(m in arch for m in ("diffusion-gemma",)):
        return True
    mid = (model_id or "").strip().lower()
    return any(m in mid for m in _UNSUPPORTED_LMS_ARCH_MARKERS)


def lmstudio_unsupported_detail(model_id: str) -> str:
    return (
        f"Modello '{model_id}' (architettura diffusion-gemma) non e' supportato dal "
        "runtime LM Studio/llama-server. Serve llama-diffusion-cli (PR #24423) oppure "
        "Unsloth Studio / vLLM. I modelli Gemma 4 standard (es. gemma-4-26b-a4b-it) "
        "continuano a funzionare normalmente."
    )


def _chat_endpoint(body: dict[str, Any]) -> str:
    """Endpoint chat upstream.

    In proxy puro (llama-swap) solo /v1/chat/completions: /api/v0/* non esiste.
    Su LM Studio nativo, /api/v0 se c'e' chat_template_kwargs.
    """
    if getattr(config, "LMSTUDIO_PROXY_PURE", False):
        return "/v1/chat/completions"
    return "/api/v0/chat/completions" if body.get("chat_template_kwargs") else "/v1/chat/completions"


def _prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Copia il body chat e, su LM Studio nativo, applica ttl gateway.

    In proxy puro `chat_template_kwargs` viene inoltrato: llama-server lo legge su
    /v1/chat/completions ed e' l'unico modo per spegnere davvero il pensiero
    (`{"enable_thinking": false}`). Il budget a 0 invece lo tronca a meta' e fa
    perdere le chiamate a strumento.
    """
    merged = dict(body)
    # TTL e' specifico di LM Studio: llama-swap lo ignora / non lo supporta.
    if not getattr(config, "LMSTUDIO_PROXY_PURE", False) and config.LMSTUDIO_TTL_S > 0:
        merged["ttl"] = config.LMSTUDIO_TTL_S
    return merged


def _raise_chat_http_error(status_code: int, detail: str) -> None:
    """Classifica errore HTTP LM Studio e solleva LMStudioError con metadati audit."""
    if _is_context_overflow_error(status_code, detail):
        raise LMStudioError(
            f"Prompt supera context LM Studio: {detail[:240]}",
            status_code=413,
            upstream_status=status_code,
            error_kind="context_overflow",
            detail=detail,
        )
    raise LMStudioError(
        f"LM Studio HTTP {status_code}" + (f": {detail}" if detail else ""),
        status_code=status_code if status_code >= 500 else 502,
        upstream_status=status_code,
        error_kind="upstream_http",
        detail=detail,
    )


def _usage_from_timings(data: dict[str, Any]) -> dict[str, Any]:
    """Completa i contatori mancanti/zero senza alterare usage reale upstream."""
    timings = data.get("timings") or {}
    if not timings:
        return data
    usage = dict(data.get("usage") or {})
    if any(usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        return data
    cached = int(timings.get("cache_n") or 0)
    prompt = int(timings.get("prompt_n") or 0) + cached
    completion = int(timings.get("predicted_n") or 0)
    usage["prompt_tokens"] = usage.get("prompt_tokens") or prompt
    usage["completion_tokens"] = usage.get("completion_tokens") or completion
    usage["total_tokens"] = usage.get("total_tokens") or (usage["prompt_tokens"] + usage["completion_tokens"])
    details = dict(usage.get("prompt_tokens_details") or {})
    details["cached_tokens"] = details.get("cached_tokens") or cached
    usage["prompt_tokens_details"] = details
    data["usage"] = usage
    return data


def _aggregate_stream_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Ricostruisce una chat.completion non-stream dai chunk SSE OpenAI-compat."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    role = "assistant"
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model = ""
    completion_id = ""
    timings: dict[str, Any] | None = None
    system_fingerprint: str | None = None
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    logprobs_content: list[Any] = []

    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        completion_id = str(ch.get("id") or completion_id)
        model = str(ch.get("model") or model)
        if isinstance(ch.get("usage"), dict):
            usage = ch["usage"]
        if isinstance(ch.get("timings"), dict):
            timings = ch["timings"]
        if ch.get("system_fingerprint"):
            system_fingerprint = str(ch["system_fingerprint"])
        for choice in ch.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            # 12/10: logprobs chiesti dal client arrivano un pezzo per chunk; ricomposti in ordine.
            if isinstance(choice.get("logprobs"), dict):
                logprobs_content.extend(choice["logprobs"].get("content") or [])
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            if delta.get("role"):
                role = str(delta["role"])
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            if delta.get("reasoning_content"):
                reasoning_parts.append(str(delta["reasoning_content"]))
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                try:
                    idx = int(tc.get("index") or 0)
                except (TypeError, ValueError):
                    idx = 0
                slot = tool_calls_by_index.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                if tc.get("type"):
                    slot["type"] = str(tc["type"])
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if fn.get("name"):
                        slot["function"]["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])

    message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_by_index:
        message["tool_calls"] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
        if not finish_reason:
            finish_reason = "tool_calls"

    out: dict[str, Any] = {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    if logprobs_content:
        out["choices"][0]["logprobs"] = {"content": logprobs_content}
    if timings is not None:
        out["timings"] = timings
    if system_fingerprint is not None:
        out["system_fingerprint"] = system_fingerprint
    return _usage_from_timings(out)


async def chat(
    body: dict[str, Any],
    timeout_s: float | None = None,
    retry_meta: list[dict[str, Any]] | None = None,
    *,
    request: Any = None,
) -> dict[str, Any]:
    """Chat non-streaming: aggrega SSE upstream (idle timeout chiude TCP zombie)."""
    async with _chat_slot(request=request):
        chunks: list[dict[str, Any]] = []
        async for chunk in _chat_stream_inner(body, timeout_s=timeout_s, retry_meta=retry_meta):
            _touch_inflight_progress()
            chunks.append(chunk)
        if not chunks:
            raise LMStudioError("LM Studio empty stream", status_code=502, error_kind="empty_stream")
        return _aggregate_stream_chunks(chunks)


async def chat_stream(
    body: dict[str, Any],
    timeout_s: float | None = None,
    retry_meta: list[dict[str, Any]] | None = None,
    *,
    request: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """Chat streaming verso LM Studio con retry transitorio breve + cold-start (pre-stream)."""
    async with _chat_slot(request=request):
        async for chunk in _chat_stream_inner(body, timeout_s=timeout_s, retry_meta=retry_meta):
            _touch_inflight_progress()
            yield _usage_from_timings(chunk)


async def _chat_stream_inner(
    body: dict[str, Any],
    timeout_s: float | None = None,
    retry_meta: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    merged = _prepare_chat_body(body)
    merged["stream"] = True
    endpoint = _chat_endpoint(merged)
    timeout = _chat_http_timeout(timeout_s)
    transient_max = _chat_transient_max_attempts()
    last_exc: LMStudioError | None = None
    enter_loading_phase = False

    async def _stream_once() -> AsyncIterator[dict[str, Any]]:
        async with get_client().stream(
            "POST",
            endpoint,
            json=merged,
            timeout=timeout,
        ) as r:
            if r.status_code >= 400:
                text = await r.aread()
                detail = text.decode("utf-8", "replace")[:400]
                with contextlib.suppress(Exception):
                    parsed = json.loads(text)
                    err_val = parsed.get("error")
                    if isinstance(err_val, str):
                        detail = err_val
                    elif isinstance(err_val, dict):
                        detail = str(err_val.get("message") or detail)
                raise _StreamStartError(r.status_code, detail)
            async for line in r.aiter_lines():
                line = line.strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error") is not None:
                    error = chunk["error"]
                    detail = str(error.get("message") or error) if isinstance(error, dict) else str(error)
                    raise LMStudioError(
                        f"LM Studio stream error: {detail}", status_code=502,
                        error_kind="upstream_error", detail=detail,
                    )
                yield chunk

    class _StreamStartError(Exception):
        def __init__(self, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"LM Studio HTTP {status_code}: {detail}")

    for t_attempt in range(transient_max):
        delay_s = _chat_transient_delay_for_attempt(t_attempt)
        try:
            async for chunk in _stream_once():
                yield chunk
            if t_attempt > 0 and retry_meta is not None:
                retry_meta.append({"phase": "transient", "recovered": True, "attempts": t_attempt + 1, "stream": True})
            return
        except _StreamStartError as exc:
            if _is_context_overflow_error(exc.status_code, exc.detail):
                _raise_chat_http_error(exc.status_code, exc.detail)
            _raise_if_upstream_busy(exc.status_code, exc.detail)
            if _is_transient_chat_error(exc.status_code, exc.detail):
                last_exc = LMStudioError(
                    f"LM Studio HTTP {exc.status_code}: {exc.detail}",
                    status_code=exc.status_code if exc.status_code >= 500 else 502,
                    upstream_status=exc.status_code,
                    error_kind="transient",
                    detail=exc.detail,
                )
                if t_attempt < transient_max - 1:
                    _record_chat_retry(
                        retry_meta,
                        phase="transient",
                        attempt=t_attempt + 1,
                        max_attempts=transient_max,
                        reason="transient_http",
                        delay_s=delay_s,
                        status_code=exc.status_code,
                        stream=True,
                    )
                    await asyncio.sleep(delay_s)
                    continue
                enter_loading_phase = _is_loading_error(exc.status_code)
                break
            logger.warning("lmstudio_error | status=%d | detail=%s", exc.status_code, exc.detail)
            _raise_chat_http_error(exc.status_code, exc.detail)
        except httpx.HTTPError as exc:
            _raise_if_pool_saturated(exc)
            _raise_if_idle_timeout(exc)
            last_exc = LMStudioError(
                f"LM Studio unreachable: {exc}",
                status_code=502,
                error_kind="connection",
                detail=str(exc),
            )
            if _is_transient_connection_error(exc) and t_attempt < transient_max - 1:
                _record_chat_retry(
                    retry_meta,
                    phase="transient",
                    attempt=t_attempt + 1,
                    max_attempts=transient_max,
                    reason="connection",
                    delay_s=delay_s,
                    stream=True,
                )
                await asyncio.sleep(delay_s)
                continue
            if t_attempt >= transient_max - 1:
                enter_loading_phase = True
                break
            raise last_exc from exc

    if not enter_loading_phase and last_exc is not None:
        raise last_exc

    loading_max = _loading_retry_max_attempts()
    if loading_max <= 0:
        raise last_exc or LMStudioError("LM Studio: max retry raggiunto", status_code=502)

    loading_delay = _loading_retry_delay_s()
    for attempt in range(loading_max):
        try:
            async for chunk in _stream_once():
                yield chunk
            if retry_meta is not None and (attempt > 0 or enter_loading_phase):
                retry_meta.append({"phase": "loading", "recovered": True, "attempts": attempt + 1, "stream": True})
            return
        except _StreamStartError as exc:
            if _is_loading_error(exc.status_code) and attempt < loading_max - 1:
                _record_chat_retry(
                    retry_meta,
                    phase="loading",
                    attempt=attempt + 1,
                    max_attempts=loading_max,
                    reason="model_loading",
                    delay_s=loading_delay,
                    status_code=exc.status_code,
                    stream=True,
                )
                await asyncio.sleep(loading_delay)
                last_exc = LMStudioError(
                    f"LM Studio HTTP {exc.status_code}: {exc.detail}",
                    status_code=exc.status_code,
                    upstream_status=exc.status_code,
                    error_kind="upstream_http",
                    detail=exc.detail,
                )
                continue
            logger.warning("lmstudio_error | status=%d | detail=%s", exc.status_code, exc.detail)
            _raise_chat_http_error(exc.status_code, exc.detail)
        except httpx.HTTPError as exc:
            _raise_if_pool_saturated(exc)
            _raise_if_idle_timeout(exc)
            if _is_transient_connection_error(exc) and attempt < loading_max - 1:
                _record_chat_retry(
                    retry_meta,
                    phase="loading",
                    attempt=attempt + 1,
                    max_attempts=loading_max,
                    reason="connection",
                    delay_s=loading_delay,
                    stream=True,
                )
                await asyncio.sleep(loading_delay)
                last_exc = LMStudioError(f"LM Studio unreachable: {exc}", status_code=502)
                continue
            raise LMStudioError(f"LM Studio unreachable: {exc}", status_code=502) from exc

    raise last_exc or LMStudioError("LM Studio: max retry raggiunto", status_code=502)


async def get_loaded_models_context() -> dict[str, int]:
    """Restituisce {model_id: loaded_context_length} per i SOLI modelli caricati.

    /api/v0/models restituisce {"object":"list","data":[...]} dove ogni item ha
    `state` ("loaded"/"not-loaded") e, se caricato, `loaded_context_length`
    (il context effettivo in VRAM; `max_context_length` è invece il massimo teorico).
    """
    try:
        r = await get_meta_client().get("/api/v0/models", timeout=10.0)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {}
        out: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "")
            if not mid or item.get("state") != "loaded":
                continue
            out[mid] = int(item.get("loaded_context_length") or 0)
        return out
    except Exception as exc:
        logger.debug("lmstudio_context_check_failed | %s", exc)
        return {}


def _lms_path() -> str | None:
    """Percorso dell'eseguibile CLI `lms` (LM Studio).

    Ordine: LMSTUDIO_LMS_PATH → PATH → ~/.lmstudio/bin → scan C:\\Users\\*\\.lmstudio\\bin
    (serve se il gateway gira come SYSTEM/Session 0 mentre LM Studio è dell'utente).
    """
    configured = (getattr(config, "LMSTUDIO_LMS_PATH", None) or "").strip()
    if configured:
        p = Path(configured)
        if p.exists():
            return str(p)

    found = shutil.which("lms") or shutil.which("lms.exe")
    if found:
        return found

    homes: list[Path] = []
    for raw in (os.path.expanduser("~"), os.environ.get("USERPROFILE"), os.environ.get("HOME")):
        if raw:
            homes.append(Path(raw))
    users_root = Path(r"C:\Users")
    if users_root.is_dir():
        with contextlib.suppress(OSError):
            homes.extend(p for p in users_root.iterdir() if p.is_dir())

    seen: set[str] = set()
    for home in homes:
        key = str(home).lower()
        if key in seen:
            continue
        seen.add(key)
        for name in ("lms.exe", "lms"):
            candidate = home / ".lmstudio" / "bin" / name
            if candidate.exists():
                return str(candidate)
    return None


async def _run_lms(args: list[str], timeout_s: float) -> tuple[int, str]:
    """Esegue `lms <args>` e ritorna (returncode, output).

    Usa subprocess.run in un thread (asyncio.to_thread): su Windows l'event loop di
    uvicorn può essere un SelectorEventLoop che non supporta create_subprocess_exec.

    Encoding UTF-8 obbligatorio: l'output di progresso di `lms load` contiene byte
    non-cp1252; con text=True e default Windows il reader thread alza UnicodeDecodeError
    e in alcuni casi il gateway interpreta il load come fallito.
    """
    lms = _lms_path()
    if not lms:
        return (-1, "lms_not_found")

    def _run() -> tuple[int, str]:
        try:
            cp = subprocess.run(
                [lms, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
            return (cp.returncode, ((cp.stdout or "") + (cp.stderr or "")))
        except subprocess.TimeoutExpired:
            return (-2, "timeout")
        except Exception as exc:  # noqa: BLE001 - errore OS/subprocess: loggare e proseguire
            return (-3, str(exc))

    return await asyncio.to_thread(_run)


async def unload_model(model_name: str) -> None:
    """Scarica il modello upstream.

    In proxy puro: POST /api/models/unload di llama-swap.
    Su LM Studio nativo: CLI `lms unload`.
    """
    if getattr(config, "LMSTUDIO_PROXY_PURE", False):
        try:
            r = await get_client().post(
                "/api/models/unload",
                json={"model": model_name},
                timeout=60.0,
            )
            logger.info(
                "lmstudio_unload_model | proxy_pure | model=%s | status=%d",
                model_name, r.status_code,
            )
        except Exception as exc:
            logger.warning("lmstudio_unload_model_failed | proxy_pure | model=%s | %s", model_name, exc)
        return
    rc, out = await _run_lms(["unload", model_name], 30.0)
    logger.info("lmstudio_unload_model | model=%s | rc=%d", model_name, rc)
    if rc not in (0,):
        logger.debug("lmstudio_unload_detail | model=%s | %s", model_name, out.strip()[:200])


# Ultimo errore di load CLI (per audit/502 esplicito).
_last_load_error: str | None = None


def get_last_load_error() -> str | None:
    return _last_load_error


async def load_model_with_context(model_name: str) -> bool:
    """Carica il modello in LM Studio con context/offload corretti via CLI `lms load`.

    Usa la CLI (sincrona) invece dell'endpoint HTTP /api/v0/models/load: quest'ultimo
    non applica realmente contextLength/gpuOffload, quindi il modello finiva caricato
    in JIT con il context di default (4096). La CLI applica i parametri in modo
    affidabile e attende il completamento del caricamento.

    Config per-modello via lmstudio_model_config (es. 26B 16384/max/parallel 1).
    Blocca fino a 240s: il cold-start è normale (client tipo Kairos hanno timeout ≥110s).
    """
    global _last_load_error
    _last_load_error = None
    context_length, gpu_offload, parallel = lmstudio_model_config(model_name)
    if not context_length and not gpu_offload:
        return True

    lms = _lms_path()
    if not lms:
        _last_load_error = "lms_not_found (CLI non in PATH né in ~/.lmstudio/bin)"
        logger.error("lmstudio_load_model_failed | model=%s | %s", model_name, _last_load_error)
        return False

    # Un solo LLM grande per volta. Scarica TUTTI i modelli LLM caricati
    # (incluso il target, così la nuova config non viene ignorata su un modello già
    # in VRAM) prima di ricaricare il target con context/offload/parallel corretti.
    loaded = await get_loaded_models_context()
    for mid in set(loaded.keys()) | {model_name}:
        if mid:
            await unload_model(mid)
    await asyncio.sleep(1.0)

    args = ["load", model_name, "-y", "--identifier", model_name]
    if context_length:
        args += ["-c", str(int(context_length))]
    if gpu_offload:
        args += ["--gpu", str(gpu_offload)]
    if parallel:
        args += ["--parallel", str(int(parallel))]
    if config.LMSTUDIO_TTL_S > 0:
        args += ["--ttl", str(int(config.LMSTUDIO_TTL_S))]

    logger.info(
        "lmstudio_load_model | model=%s | lms=%s | cmd=%s | contextLength=%s | gpuOffload=%s | parallel=%s | ttl=%s | via=cli",
        model_name,
        lms,
        " ".join(args),
        context_length or None,
        gpu_offload or None,
        parallel or None,
        config.LMSTUDIO_TTL_S if config.LMSTUDIO_TTL_S > 0 else None,
    )
    rc, out = await _run_lms(args, timeout_s=240.0)
    out_s = (out or "").strip()
    # Trace persistente per diagnosticare cold-start 502 (audit nasconde il dettaglio).
    with contextlib.suppress(Exception):
        debug_path = Path(__file__).resolve().parent / "logs" / "lms_load_debug.log"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} model={model_name} lms={lms!r} "
                f"rc={rc} args={args!r} out={out_s[:800]!r}\n"
            )

    if rc == 0:
        loaded_after = await get_loaded_models_context()
        if model_name not in loaded_after:
            # Output progresso unicode a volte lascia out vuoto: attendi e ripoll.
            for _ in range(5):
                await asyncio.sleep(1.0)
                loaded_after = await get_loaded_models_context()
                if model_name in loaded_after:
                    break
        if model_name in loaded_after or "successfully" in out_s.lower():
            _last_known_load[model_name] = (
                int(context_length or 0),
                gpu_offload,
                int(parallel or 1),
            )
            logger.info(
                "lmstudio_load_model_ok | model=%s | ctx=%s | via=cli",
                model_name,
                loaded_after.get(model_name),
            )
            return True
        _last_load_error = (
            f"lms rc=0 ma modello non in VRAM | out={out_s[:400]!r}"
        )
        logger.warning("lmstudio_load_model_failed | model=%s | %s", model_name, _last_load_error)
        return False

    _last_load_error = f"lms rc={rc} | out={out_s[:500]!r}"
    logger.warning(
        "lmstudio_load_model_failed | model=%s | rc=%d | out=%s",
        model_name, rc, out_s[:500],
    )
    return False


async def ensure_model_context(model_name: str) -> bool:
    """Garantisce che il modello sia caricato con context/gpu/parallel desiderati.

    In proxy puro (llama-swap) e' no-op: lo swap carica su richiesta e gestisce
    lo scarico dell'altro modello. Su LM Studio nativo forza reload via CLI se
    context/gpu/parallel non coincidono.
    """
    if getattr(config, "LMSTUDIO_PROXY_PURE", False):
        return True

    target_context, target_gpu, target_parallel = lmstudio_model_config(model_name)
    if not target_context:
        return True

    async with _context_guard:
        loaded_ctx = await get_loaded_models_context()
        current = int(loaded_ctx.get(model_name) or 0)
        target = int(target_context)
        expected = (target, target_gpu, int(target_parallel))
        last = _last_known_load.get(model_name)
        if current == target and last == expected:
            return True

        logger.warning(
            "lmstudio_config_mismatch | model=%s | current_ctx=%s | target=%s | last=%s | action=reload",
            model_name,
            current or "not_loaded",
            expected,
            last,
        )
        ok = await load_model_with_context(model_name)
        if ok:
            return True

        err = _last_load_error or "unknown"
        # Non 502-are sul cold-start: la chat userà JIT + retry loading (LMSTUDIO_RETRY_*).
        # Tipico quando il gateway gira come SYSTEM e `lms` non parla con LM Studio utente.
        logger.warning(
            "lmstudio_ensure_fallback_jit | model=%s | err=%s | "
            "chat procederà con load JIT (riavviare Localrouter come utente interattivo "
            "per imporre context/parallel via CLI)",
            model_name,
            err,
        )
        return True


async def warmup(model_name: str) -> bool:
    """Invia richiesta dummy per precaricare il modello in VRAM.

    Restituisce True se il warmup ha avuto successo, False altrimenti.
    Usa retry interno — attende fino a LMSTUDIO_RETRY_MAX * LMSTUDIO_RETRY_DELAY_S secondi.
    """
    logger.info("lmstudio_warmup_start | model=%s", model_name)
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ciao"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        await chat(body)
        logger.info("lmstudio_warmup_ok | model=%s", model_name)
        return True
    except LMStudioError as exc:
        logger.warning("lmstudio_warmup_failed | model=%s | err=%s", model_name, exc)
        return False


async def probe_server(timeout_s: float = 5.0) -> bool:
    """True se il local server LM Studio risponde su /v1/models."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(f"{config.LMSTUDIO_HOST}/v1/models")
            return r.status_code < 500
    except Exception:
        return False


async def ensure_server_running() -> bool:
    """Se il local server e' giu', tenta di ripristinarlo.

    In proxy puro (llama-swap) non c'e' `lms server start`: solo probe.
    Su LM Studio nativo tenta `lms server start -p 1234`.
    """
    if await probe_server():
        return True
    if getattr(config, "LMSTUDIO_PROXY_PURE", False):
        logger.warning("lmstudio_server_down | proxy_pure=1 | no lms revive")
        return False
    logger.warning("lmstudio_server_down | attempting lms server start")
    rc, out = await _run_lms(["server", "start", "-p", "1234"], 90.0)
    logger.info("lmstudio_server_start | rc=%d | out=%s", rc, (out or "")[:300])
    await asyncio.sleep(2.0)
    return await probe_server()


async def revive_runtime(*, load_warmup_model: bool = True) -> bool:
    """Ripristino dopo crash del runtime.

    In proxy puro: solo probe + warmup HTTP (llama-swap carica su richiesta).
    Su LM Studio: restart server + reload modello produzione via CLI.
    """
    if getattr(config, "LMSTUDIO_PROXY_PURE", False):
        if not await probe_server():
            logger.error("lmstudio_revive_failed | proxy_pure unreachable at %s", config.LMSTUDIO_HOST)
            return False
        if load_warmup_model and config.LMSTUDIO_WARMUP_MODEL:
            return await warmup(config.LMSTUDIO_WARMUP_MODEL.strip())
        return True

    ok = await ensure_server_running()
    if not ok:
        await _run_lms(["server", "stop"], 30.0)
        await asyncio.sleep(2.0)
        ok = await ensure_server_running()
    if not ok:
        logger.error("lmstudio_revive_failed | server still unreachable")
        return False
    if load_warmup_model and config.LMSTUDIO_WARMUP_MODEL:
        mid = config.LMSTUDIO_WARMUP_MODEL.strip()
        loaded = await load_model_with_context(mid)
        if loaded:
            await warmup(mid)
        return bool(loaded)
    return True


# Prompt allineato a ollama_client.ocr_call (stesso contratto OCR).
_OCR_PROMPT = (
    "Sei un sistema di trascrizione tecnica accurato e conservativo. "
    "Analizza l'immagine e trascrivi fedelmente tutto il contenuto visibile: "
    "testo, formule matematiche (in LaTeX quando possibile), simboli, "
    "dati numerici con unita' di misura, tabelle, schemi. "
    "Non inventare nulla. Se un elemento e' illeggibile scrivi [ILLEGGIBILE]. "
    "Se ci sono formule, riscrivile in LaTeX (es. $F = m \\cdot a$). "
    "Rispondi con un JSON nel formato:\n"
    '{"blocks": [{"type": "text"|"formula", "content": "...", "latex": "..."}]}\n'
    "Usa type=formula solo se il blocco e' una formula matematica e compila "
    "anche il campo latex. Per tutto il resto usa type=text e lascia latex vuoto."
)


async def ocr_call(
    image_bytes: bytes,
    model: str,
    prompt: str | None = None,
) -> str:
    """OCR via LM Studio OpenAI-compat (/v1/chat/completions + image_url base64).

    Stessa firma di ollama_client.ocr_call. Nessun keep-alive lungo: JIT + TTL gateway.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt or _OCR_PROMPT},
                ],
            }
        ],
        "temperature": 0.0,
        "stream": False,
    }
    data = await chat(body, timeout_s=OCR_TIMEOUT_S)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    return str(content).strip()
