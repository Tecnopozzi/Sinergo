"""Router locale fra server LocalRouter: modelli e lavori con prefisso server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

import config
from remoti import leggi_server, valida_server

USER_AGENT = "Mozilla/5.0"
TIMEOUT = httpx.Timeout(1800.0, connect=30.0)
TIMEOUT_CATALOGO = 3.0
CACHE_SECONDI = 30.0
logger = logging.getLogger(__name__)
_SALTA_RICHIESTA = {"host", "content-length", "connection", "authorization", "x-api-key", "user-agent", "transfer-encoding"}
_SALTA_RISPOSTA = {"connection", "transfer-encoding", "keep-alive"}
_RADICI_API = {"v1", "health", "process", "media", "status", "result", "cancel", "metrics", "dashboard", "debug", "llm", "audio", "whisper", "jobs", "cancel-all", "admin", "api"}
_LAVORO = re.compile(r"^(v1/chat/jobs|media/(?:status|result|cancel)|status|result|cancel)/(.+)$")
_CREAZIONI = {"v1/chat/jobs", "process", "media/process"}
_CREATED_AT_ZERO = "1970-01-01T00:00:00Z"
_CLIENT_VERSION = "localrouter-client-1"


def _strip_lmstudio_prefix(model_id: str) -> str:
    return str(model_id or "").removeprefix("lmstudio:")


def _ollama_name(model_id: str) -> str:
    name = _strip_lmstudio_prefix(model_id)
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _ollama_family(model_id: str) -> str:
    low = _strip_lmstudio_prefix(model_id).lower()
    for family in ("gemma", "qwen", "llama", "mistral", "deepseek", "phi"):
        if family in low:
            return family
    return "localrouter"


def _model_context(model: dict[str, Any]) -> int | None:
    for key in ("max_context_length", "context_window", "context_length"):
        value = model.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _capabilities(model: dict[str, Any]) -> set[str]:
    raw = model.get("capabilities") or []
    if isinstance(raw, dict):
        out = {str(k).lower() for k, v in raw.items() if v}
        reasoning = raw.get("reasoning")
        if reasoning:
            out.add("thinking")
        return out
    if isinstance(raw, list):
        return {str(x).lower() for x in raw}
    return set()


def _lmstudio_v1_model(model: dict[str, Any]) -> dict[str, Any]:
    model_id = _strip_lmstudio_prefix(str(model.get("id") or ""))
    publisher = str(model.get("publisher") or (model_id.split("/", 1)[0] if "/" in model_id else "localrouter"))
    caps = _capabilities(model)
    capabilities: dict[str, Any] = {
        "vision": "vision" in caps,
        "trained_for_tool_use": "tools" in caps or "tool_use" in caps,
    }
    if "thinking" in caps or "reasoning" in caps:
        capabilities["reasoning"] = {"allowed_options": ["off", "on"], "default": "on"}
    return {
        "type": "llm",
        "publisher": publisher,
        "key": model_id,
        "display_name": str(model.get("display_name") or model_id),
        "architecture": model.get("arch") or model.get("architecture"),
        "quantization": model.get("quantization") if isinstance(model.get("quantization"), dict) else None,
        "size_bytes": int(model.get("size") or model.get("size_bytes") or 0),
        "params_string": model.get("params_string"),
        "loaded_instances": [{"id": model_id, "config": {"context_length": _model_context(model)}}] if model.get("loaded") is True else [],
        "max_context_length": _model_context(model),
        "format": model.get("format") or "gguf",
        "capabilities": capabilities,
        "description": model.get("description"),
    }


def _lmstudio_v0_model(model: dict[str, Any]) -> dict[str, Any]:
    model_id = _strip_lmstudio_prefix(str(model.get("id") or ""))
    publisher = str(model.get("publisher") or (model_id.split("/", 1)[0] if "/" in model_id else "localrouter"))
    quant = model.get("quantization")
    if isinstance(quant, dict):
        quant = quant.get("name")
    return {
        "id": model_id,
        "object": "model",
        "type": "llm",
        "publisher": publisher,
        "arch": model.get("arch") or model.get("architecture"),
        "compatibility_type": model.get("format") or "gguf",
        "quantization": quant,
        "state": "loaded" if model.get("loaded") is True else ("not-loaded" if model.get("loaded") is False else "unknown"),
        "max_context_length": _model_context(model),
    }


def _ollama_tag_model(model: dict[str, Any]) -> dict[str, Any]:
    model_id = str(model.get("id") or "")
    name = _ollama_name(model_id)
    family = _ollama_family(model_id)
    quant = model.get("quantization")
    if isinstance(quant, dict):
        quant = quant.get("name")
    return {
        "name": name,
        "model": name,
        "modified_at": str(model.get("modified_at") or _CREATED_AT_ZERO),
        "size": int(model.get("size") or model.get("size_bytes") or 0),
        "digest": str(model.get("digest") or ""),
        "loaded": model.get("loaded"),
        "details": {
            "parent_model": "",
            "format": str(model.get("format") or "gguf"),
            "family": family,
            "families": [family],
            "parameter_size": str(model.get("params_string") or ""),
            "quantization_level": str(quant or ""),
        },
    }


def _ollama_show_model(model: dict[str, Any]) -> dict[str, Any]:
    tag = _ollama_tag_model(model)
    caps = _capabilities(model)
    out_caps = ["completion"]
    if "tools" in caps or "tool_use" in caps:
        out_caps.append("tools")
    if "vision" in caps:
        out_caps.append("vision")
    if "thinking" in caps or "reasoning" in caps:
        out_caps.append("thinking")
    return {
        "modelfile": f"FROM {_strip_lmstudio_prefix(str(model.get('id') or ''))}\n",
        "parameters": "",
        "template": "",
        "details": tag["details"],
        "model_info": {
            "general.architecture": tag["details"]["family"],
            "general.context_length": _model_context(model),
        },
        "capabilities": out_caps,
    }


def _ollama_request_model_to_remote(requested: str, models: list[dict[str, Any]]) -> str:
    requested_clean = str(requested or "")
    requested_no_latest = requested_clean.removesuffix(":latest")
    for model in models:
        model_id = str(model.get("id") or "")
        stripped = _strip_lmstudio_prefix(model_id)
        if requested_clean in {model_id, stripped, _ollama_name(model_id), model_id.partition("/")[2]}:
            return model_id
        if requested_no_latest and requested_no_latest in {model_id, stripped}:
            return model_id
    return requested_no_latest or requested_clean


def _openai_to_ollama_chat(data: dict[str, Any], model_name: str) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    out = {
        "model": model_name,
        "created_at": _CREATED_AT_ZERO,
        "message": {
            "role": message.get("role") or "assistant",
            "content": message.get("content") or "",
        },
        "done": True,
        "done_reason": choice.get("finish_reason") or "stop",
    }
    if message.get("tool_calls"):
        out["message"]["tool_calls"] = message["tool_calls"]
    if usage.get("prompt_tokens") is not None:
        out["prompt_eval_count"] = int(usage.get("prompt_tokens") or 0)
    if usage.get("completion_tokens") is not None:
        out["eval_count"] = int(usage.get("completion_tokens") or 0)
    return out


def _openai_chunk_to_ollama_line(chunk: dict[str, Any], model_name: str) -> bytes:
    choice = (chunk.get("choices") or [{}])[0] or {}
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")
    payload = {
        "model": model_name,
        "created_at": _CREATED_AT_ZERO,
        "message": {
            "role": delta.get("role") or "assistant",
            "content": delta.get("content") or "",
        },
        "done": bool(finish),
    }
    if finish:
        payload["done_reason"] = finish
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def crea_app(server: str = "", chiave: str = "", *, remoti: dict | None = None) -> FastAPI:
    """Crea il router; il vecchio server singolo viene identificato via /health."""
    elenco = valida_server(remoti) if remoti is not None else {}
    if remoti is not None and not elenco:
        raise ValueError("Elenco server vuoto")
    remoto = httpx.AsyncClient(timeout=TIMEOUT)
    blocco = asyncio.Lock()
    cache = None
    scadenza = 0.0

    @asynccontextmanager
    async def durata(app):
        yield
        await remoto.aclose()

    app = FastAPI(title="LocalRouter (client)", docs_url=None, redoc_url=None, openapi_url=None, lifespan=durata)

    @app.middleware("http")
    async def solo_locale(request: Request, call_next):
        """Il client allega la chiave del server: niente DNS rebinding (Host) ne' pagine web (Origin)."""
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"} or "origin" in request.headers:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)

    def headers_server(dati):
        return {"authorization": f"Bearer {dati['chiave']}", "user-agent": USER_AGENT}

    async def inizializza():
        if elenco:
            return
        async with blocco:
            if elenco:
                return
            try:
                risposta = await remoto.get(server.rstrip("/") + "/health", headers=headers_server({"chiave": chiave}), timeout=TIMEOUT_CATALOGO)
                risposta.raise_for_status()
                nome = risposta.json()["server_name"]
                elenco.update(valida_server({nome: {"indirizzo": server, "chiave": chiave}}))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(502, "Impossibile identificare il server legacy tramite /health") from None

    async def catalogo(fresco=False):
        nonlocal cache, scadenza
        if not fresco and cache is not None and time.monotonic() < scadenza:
            return cache
        async with blocco:
            if not fresco and cache is not None and time.monotonic() < scadenza:
                return cache

            async def leggi(nome, dati):
                try:
                    r = await remoto.get(dati["indirizzo"] + "/v1/models?runtime=true", headers=headers_server(dati), timeout=TIMEOUT_CATALOGO)
                    r.raise_for_status()
                    modelli = r.json()["data"]
                    if not isinstance(modelli, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in modelli):
                        raise ValueError()
                    return nome, modelli, True
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    return nome, [], False

            cache = await asyncio.gather(*(leggi(n, d) for n, d in elenco.items()))
            # Lo stato runtime deve riflettere anche unload e cambi di modello immediati.
            dinamico = any("loaded" in m for _, ms, _ in cache for m in ms)
            scadenza = time.monotonic() + (0 if dinamico else CACHE_SECONDI)
            return cache

    async def _modelli_remoti() -> list[dict[str, Any]]:
        await inizializza()
        dati = await catalogo(fresco=True)
        if not any(ok for _, _, ok in dati):
            raise HTTPException(502, "Nessun server LocalRouter raggiungibile")
        return [{**m, "id": f"{n}/{m['id']}", "owned_by": n} for n, ms, _ in dati for m in ms]

    def destinazione_chat(body):
        nome, _, modello = body["model"].partition("/")
        if nome not in elenco:
            raise HTTPException(404, "Server o modello sconosciuto")
        return elenco[nome], {**body, "model": modello}

    async def _post_openai_chat(body: dict[str, Any], stream: bool) -> httpx.Response:
        dati, body = destinazione_chat(body)
        return await remoto.post(
            f"{dati['indirizzo']}/v1/chat/completions",
            headers={**headers_server(dati), "accept-encoding": "identity"},
            json={**body, "stream": stream},
        )

    @app.get("/api/v1/models")
    async def lmstudio_v1_models() -> Response:
        try:
            models = await _modelli_remoti()
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"LocalRouter client: server {server} non raggiungibile: {e!r}"}, status_code=502)
        return JSONResponse({"models": [_lmstudio_v1_model(m) for m in models]})

    @app.get("/api/v0/models")
    async def lmstudio_v0_models() -> Response:
        try:
            models = await _modelli_remoti()
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"LocalRouter client: server {server} non raggiungibile: {e!r}"}, status_code=502)
        return JSONResponse({"object": "list", "data": [_lmstudio_v0_model(m) for m in models]})

    @app.post("/api/v1/models/load")
    async def lmstudio_v1_load(request: Request) -> Response:
        body = await request.json()
        models = await _modelli_remoti()
        requested = str(body.get("model") or "")
        model_id = _ollama_request_model_to_remote(requested, models)
        model = next((m for m in models if m["id"] == model_id), None)
        if model is None:
            raise HTTPException(404, "Modello sconosciuto")
        if model.get("loaded") is not True:
            raise HTTPException(409, "Modello non caricato: il caricamento avviene alla prima inferenza")
        return JSONResponse({"type": "llm", "instance_id": model_id, "status": "loaded"})

    @app.get("/api/ps")
    async def ollama_ps() -> Response:
        models = await _modelli_remoti()
        return JSONResponse({"models": [{**_ollama_tag_model(m), "context_length": _model_context(m)} for m in models if m.get("loaded") is True]})

    @app.get("/api/tags")
    async def ollama_tags() -> Response:
        try:
            models = await _modelli_remoti()
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"LocalRouter client: server {server} non raggiungibile: {e!r}"}, status_code=502)
        return JSONResponse({"models": [_ollama_tag_model(m) for m in models]})

    @app.post("/api/show")
    async def ollama_show(request: Request) -> Response:
        try:
            body = await request.json()
            requested = str(body.get("model") or "")
            models = await _modelli_remoti()
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"LocalRouter client: server {server} non raggiungibile: {e!r}"}, status_code=502)
        remote_model = _ollama_request_model_to_remote(requested, models)
        for model in models:
            if str(model.get("id") or "") == remote_model:
                return JSONResponse(_ollama_show_model(model))
        return JSONResponse({"error": f"model {requested!r} not found"}, status_code=404)

    @app.get("/api/version")
    async def ollama_version() -> dict[str, str]:
        return {"version": _CLIENT_VERSION}

    @app.post("/api/chat")
    async def ollama_chat(request: Request) -> Response:
        try:
            body = await request.json()
            models = await _modelli_remoti()
            requested_model = str(body.get("model") or "")
            remote_model = _ollama_request_model_to_remote(requested_model, models)
            options = body.get("options") if isinstance(body.get("options"), dict) else {}
            openai_body: dict[str, Any] = {
                "model": remote_model,
                "messages": body.get("messages") or [],
            }
            for key in ("temperature", "top_p", "seed", "stop"):
                if key in options:
                    openai_body[key] = options[key]
            if options.get("num_predict") is not None:
                openai_body["max_tokens"] = options["num_predict"]
            if body.get("tools") is not None:
                openai_body["tools"] = body["tools"]
            stream = bool(body.get("stream", True))
            if not stream:
                resp = await _post_openai_chat(openai_body, stream=False)
                if resp.status_code >= 400:
                    return JSONResponse(resp.json(), status_code=resp.status_code)
                return JSONResponse(_openai_to_ollama_chat(resp.json(), requested_model))

            dati, stream_body = destinazione_chat(openai_body)
            resp = await remoto.send(
                remoto.build_request(
                    "POST",
                    f"{dati['indirizzo']}/v1/chat/completions",
                    headers={**headers_server(dati), "accept-encoding": "identity"},
                    json={**stream_body, "stream": True},
                ),
                stream=True,
            )
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"LocalRouter client: server {server} non raggiungibile: {e!r}"}, status_code=502)

        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return Response(content=content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))

        async def ollama_stream() -> AsyncIterator[bytes]:
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        yield (json.dumps({"model": requested_model, "created_at": _CREATED_AT_ZERO, "message": {"role": "assistant", "content": ""}, "done": True}, ensure_ascii=False) + "\n").encode("utf-8")
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield _openai_chunk_to_ollama_line(chunk, requested_model)
            finally:
                await resp.aclose()

        return StreamingResponse(ollama_stream(), media_type="application/x-ndjson")

    @app.api_route("/{percorso:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def inoltra(percorso: str, request: Request) -> Response:
        await inizializza()
        nome = None
        primo, barra, resto = percorso.partition("/")
        if primo in elenco:
            nome, percorso = primo, resto
        elif barra and primo not in _RADICI_API and resto.split("/", 1)[0] in _RADICI_API:
            raise HTTPException(404, f"Server sconosciuto: {primo}")

        if nome is None and percorso in {"v1/models", "health"} and request.method == "GET":
            dati = await catalogo()
            if percorso == "health":
                return JSONResponse({"servers": [{"nome": n, "indirizzo": elenco[n]["indirizzo"], "raggiungibile": ok, "modelli": [m["id"] for m in ms]} for n, ms, ok in dati]})
            return JSONResponse({"object": "list", "data": [{**m, "id": f"{n}/{m['id']}", "owned_by": n} for n, ms, _ in dati for m in ms]})

        lavoro = _LAVORO.fullmatch(percorso)
        if lavoro:
            prefisso, identificatore = lavoro.groups()
            candidato, separatore, locale = identificatore.partition("/")
            legacy_chat = prefisso == "v1/chat/jobs" and locale in {"result", "events"} and candidato not in elenco
            if separatore and not legacy_chat:
                if candidato not in elenco:
                    raise HTTPException(404, f"Server sconosciuto: {candidato}")
                if nome and nome != candidato:
                    raise HTTPException(400, "Prefissi server discordanti")
                nome, percorso = candidato, f"{prefisso}/{locale}"

        corpo = await request.body()
        tipo = request.headers.get("content-type", "").split(";")[0].lower()
        payload = None
        form = None
        modello = None
        if corpo and tipo == "application/json":
            try:
                payload = json.loads(corpo)
                modello = payload.get("model") if isinstance(payload, dict) else None
            except ValueError:
                pass  # La validazione del JSON resta al server destinatario.
        elif corpo and tipo in {"multipart/form-data", "application/x-www-form-urlencoded"}:
            form = await request.form()
            modello = form.get("model")

        compatibile = False
        if isinstance(modello, str) and modello:
            candidato, separatore, locale = modello.partition("/")
            if separatore and candidato in elenco:
                if nome and nome != candidato:
                    raise HTTPException(400, "Prefissi server discordanti")
                nome, modello_locale = candidato, locale
            elif nome:
                if separatore:
                    dati_catalogo = await catalogo()
                    if not any(n == nome and any(m["id"] == modello for m in ms) for n, ms, _ in dati_catalogo):
                        raise HTTPException(404, f"Server sconosciuto: {candidato}")
                modello_locale = modello
            else:
                dati = await catalogo()
                nome = next((n for n, ms, _ in dati if any(m["id"] == modello for m in ms)), None)
                if nome is None:
                    dettaglio = f"Server sconosciuto: {candidato}" if separatore else f"Modello non trovato: {modello}"
                    raise HTTPException(404, dettaglio)
                modello_locale, compatibile = modello, True
                logger.info("Modello senza prefisso instradato al server %s", nome)
            if payload is not None:
                payload["model"] = modello_locale
                corpo = json.dumps(payload).encode()
            elif tipo == "application/x-www-form-urlencoded":
                corpo = urlencode([(k, modello_locale if k == "model" else v) for k, v in parse_qsl(corpo.decode(), keep_blank_values=True)]).encode()

        if form is not None and not (tipo == "multipart/form-data" and isinstance(modello, str) and modello):
            await form.close()

        nome = nome or next(iter(elenco))
        dati = elenco[nome]
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _SALTA_RICHIESTA}
        headers.update(headers_server(dati))
        headers["accept-encoding"] = request.headers.get("accept-encoding", "identity")
        url = f"{dati['indirizzo']}/{percorso}"
        if request.url.query:
            url += f"?{request.url.query}"
        if form is not None and tipo == "multipart/form-data" and isinstance(modello, str) and modello:
            parti = []
            for k, v in form.multi_items():
                if isinstance(v, UploadFile):
                    parti.append((k, (v.filename, await v.read(), v.content_type, {k: h for k, h in v.headers.items() if k.lower() != "content-disposition"})))
                else:
                    parti.append((k, (None, modello_locale if k == "model" else v)))
            headers.pop("content-type", None)
            req = remoto.build_request(request.method, url, headers=headers, files=parti)
            await form.close()
        else:
            req = remoto.build_request(request.method, url, headers=headers, content=corpo)
        try:
            resp = await remoto.send(req, stream=True)
        except httpx.HTTPError:
            raise HTTPException(502, f"LocalRouter client: server {nome} non raggiungibile") from None
        headers_risposta = {k: v for k, v in resp.headers.items() if k.lower() not in _SALTA_RISPOSTA}
        if compatibile:
            headers_risposta["X-LocalRouter-Server"] = nome
        if request.method == "POST" and percorso in _CREAZIONI and resp.is_success and "application/json" in resp.headers.get("content-type", ""):
            try:
                contenuto = json.loads(await resp.aread())
                if isinstance(contenuto, dict) and isinstance(contenuto.get("job_id"), str):
                    contenuto["job_id"] = f"{nome}/{contenuto['job_id']}"
                    headers_risposta = {k: v for k, v in headers_risposta.items() if k.lower() not in {"content-length", "content-encoding"}}
                    return JSONResponse(contenuto, status_code=resp.status_code, headers=headers_risposta)
                return Response(resp.content, status_code=resp.status_code, headers={k: v for k, v in headers_risposta.items() if k.lower() not in {"content-length", "content-encoding"}})
            finally:
                await resp.aclose()
        return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=headers_risposta, background=BackgroundTask(resp.aclose))

    return app


def main() -> None:
    import uvicorn

    percorso = Path(config.BASE_DIR) / "server_remoti.yaml"
    if percorso.exists():
        elenco = leggi_server(percorso)
        if not elenco:
            raise SystemExit("Elenco server vuoto: aggiungi un server con installa.py --client")
        app = crea_app(remoti=elenco)
    elif config.SERVER_REMOTO and config.CHIAVE_REMOTA:
        app = crea_app(config.SERVER_REMOTO, config.CHIAVE_REMOTA)
    else:
        raise SystemExit("LocalRouter client senza server: lancia `python installa.py --client`.")
    # Il proxy senza autenticazione resta accessibile soltanto dalla macchina locale.
    for port in config.CLIENT_EXTRA_PORTS:
        threading.Thread(
            target=uvicorn.run,
            kwargs={
                "app": crea_app(remoti=elenco) if percorso.exists() else crea_app(config.SERVER_REMOTO, config.CHIAVE_REMOTA),
                "host": "127.0.0.1",
                "port": port,
                "log_level": "info",
            },
            daemon=True,
        ).start()
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
