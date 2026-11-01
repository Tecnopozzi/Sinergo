"""Regressioni gateway: errori upstream e token reali, senza GPU/rete."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import config
import lmstudio_client
import server


async def _gateway(monkeypatch, handler, stream=False, after_heartbeat=False):
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_MAX", 1)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 0)
    # Battito rapido solo quando il test lo aspetta: su Windows il giro della richiesta
    # supera i 10 ms e il battito partirebbe prima della risposta immediata del finto.
    monkeypatch.setattr(config, "NON_STREAM_HEARTBEAT_INTERVAL_S", 0.01 if after_heartbeat else 5.0)
    monkeypatch.setattr(lmstudio_client, "ensure_model_context", AsyncMock(return_value=True))
    monkeypatch.setattr(lmstudio_client, "_chat_slot_sem", None)
    release = asyncio.Event()

    async def upstream(request):
        if after_heartbeat:
            await release.wait()
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://fake") as client:
        monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
        payload = {"model": "lmstudio:gemma", "messages": [], "stream": stream}
        req = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": [], "query_string": b""})
        req._body = json.dumps(payload).encode()
        monkeypatch.setattr(req, "is_disconnected", AsyncMock(return_value=False))
        server._audit_init(req)
        response = await server.chat_completions(req, ip="test")
        events = []

        async def send(event):
            events.append(event)
            if event.get("body") in (b" ", b": keepalive\n\n"):
                release.set()

        scope = {**req.scope, "asgi": {"spec_version": "2.4"}}
        await response(scope, AsyncMock(), send)
        status = next(e["status"] for e in events if e["type"] == "http.response.start")
        return status, b"".join(e.get("body", b"") for e in events), req


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("after_heartbeat", [False, True])
@pytest.mark.parametrize("upstream_status", [500, 503])
def test_upstream_5xx(monkeypatch, stream, after_heartbeat, upstream_status):
    def upstream(request):
        return httpx.Response(upstream_status, json={"error": {"message": "upstream command exited prematurely"}})

    status, body, req = asyncio.run(asyncio.wait_for(
        _gateway(monkeypatch, upstream, stream, after_heartbeat), timeout=3,
    ))
    assert status == (200 if after_heartbeat else upstream_status)
    if stream and after_heartbeat:
        payload = json.loads(next(line[6:] for line in body.splitlines() if line.startswith(b"data: {")))
    else:
        payload = json.loads(body)
    assert "upstream command exited prematurely" in payload["error"]["message"]
    assert "choices" not in payload
    assert req.state.audit_entry["lmstudio_error"]["upstream_status"] == upstream_status


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                  {"prompt_tokens": 91, "completion_tokens": 7, "total_tokens": 98,
                                   "prompt_tokens_details": {"cached_tokens": 80}},
                                  {"prompt_tokens": 91, "completion_tokens": 0, "total_tokens": 91,
                                   "prompt_tokens_details": {"cached_tokens": 0}}])
def test_usage_from_timings(monkeypatch, stream, usage):
    chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
             "timings": {"prompt_n": 12, "cache_n": 30, "predicted_n": 4}}
    if usage is not None:
        chunk["usage"] = usage

    def upstream(request):
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    status, body, _ = asyncio.run(_gateway(monkeypatch, upstream, stream))
    payload = json.loads(body.split(b"data: ")[1].split(b"\n")[0] if stream else body)
    assert status == 200
    expected = usage if usage and usage["total_tokens"] else {
        "prompt_tokens": 42, "completion_tokens": 4, "total_tokens": 46,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    assert payload["usage"] == expected
    assert payload["timings"] == chunk["timings"]


@pytest.mark.parametrize("stream", [False, True])
def test_sse_error_is_not_aggregated_as_empty_success(monkeypatch, stream):
    def upstream(request):
        return httpx.Response(200, text='data: {"error":{"message":"model failed","code":500}}\n\n')

    status, body, _ = asyncio.run(_gateway(monkeypatch, upstream, stream))
    assert status == 502
    assert "model failed" in json.loads(body)["error"]["message"]


@pytest.mark.parametrize("stream", [False, True])
def test_http_route_preserves_500(monkeypatch, tmp_path, stream):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_MAX", 1)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 0)
    monkeypatch.setattr(lmstudio_client, "ensure_model_context", AsyncMock(return_value=True))
    monkeypatch.setattr(lmstudio_client, "_chat_slot_sem", None)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(500, text="upstream command exited prematurely")
    ), base_url="http://fake")
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: upstream)
    response = TestClient(server.app).post(
        "/v1/chat/completions", headers={"Authorization": "Bearer test-key"},
        json={"model": "lmstudio:gemma", "messages": [], "stream": stream},
    )
    asyncio.run(upstream.aclose())
    assert response.status_code == 500
    assert "upstream command exited prematurely" in response.json()["error"]["message"]
    audit = json.loads((tmp_path / "audit.jsonl").read_text())
    assert audit["status_code"] == 500
