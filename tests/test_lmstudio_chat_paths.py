"""Test percorsi gateway LM Studio (tools fallback + audit retries)."""

from __future__ import annotations

import asyncio

import httpx
from starlette.requests import Request

import config
import lmstudio_client
import server


def _patch_retry(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_MAX", 3)
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 0)
    monkeypatch.setattr(config, "LMSTUDIO_TTL_S", 0)


def _fake_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "query_string": b"",
    }
    req = Request(scope)
    server._audit_init(req)
    return req


def test_audit_finalize_always_sets_lmstudio_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", tmp_path / "audit.jsonl")
    req = _fake_request()
    req.state.audit_entry["route"] = {"kind": "chat_completion", "backend": "lmstudio"}
    req.state.lmstudio_retries = []
    server._audit_finalize(req, 200)
    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"lmstudio_retries"' in line


def test_nonstream_tools_fallback_uses_same_retry_meta(monkeypatch, client_stream_finto):
    _patch_retry(monkeypatch)
    client = client_stream_finto([
        (400, {"error": {"message": "Model does not support tools"}}),
        (200, {"choices": [{"message": {"content": "ok"}}]}),
    ])
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
    retry_meta: list[dict] = []
    body = {
        "model": "gemma",
        "messages": [{"role": "user", "content": "ciao"}],
        "tools": [{"type": "function", "function": {"name": "ping", "parameters": {}}}],
    }
    data = asyncio.run(server._lmstudio_chat_nonstream_call(body, "lmstudio:gemma", retry_meta))
    assert data["choices"][0]["message"]["content"] == "ok"
    assert client.stream.call_count == 2


def test_nonstream_connection_retry_then_ok(monkeypatch, client_stream_finto):
    _patch_retry(monkeypatch)
    client = client_stream_finto([
        httpx.ConnectError("refused"),
        (200, {"choices": []}),
    ])
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
    retry_meta: list[dict] = []
    asyncio.run(
        server._lmstudio_chat_nonstream_call(
            {"model": "gemma", "messages": []},
            "lmstudio:gemma",
            retry_meta,
        )
    )
    assert client.stream.call_count == 2
    assert retry_meta[0]["reason"] == "connection"
