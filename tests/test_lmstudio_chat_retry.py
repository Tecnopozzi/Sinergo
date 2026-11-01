"""Test retry transitorio chat LM Studio (post-generazione / connessione)."""

from __future__ import annotations

import asyncio

import httpx

import config
import lmstudio_client


def _patch_retry(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_MAX", 3)
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 1)
    monkeypatch.setattr(config, "LMSTUDIO_TTL_S", 0)


def test_chat_retries_transient_502_then_succeeds(monkeypatch, client_stream_finto):
    _patch_retry(monkeypatch)
    client = client_stream_finto([
        (502, {"error": {"message": "model busy"}}),
        (200, {"choices": [{"message": {"content": "ok"}}]}),
    ])
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)

    retry_meta: list[dict] = []
    data = asyncio.run(
        lmstudio_client.chat(
            {"model": "gemma-4-26b-a4b-it", "messages": []},
            retry_meta=retry_meta,
        )
    )

    assert data["choices"][0]["message"]["content"] == "ok"
    assert client.stream.call_count == 2
    assert retry_meta[0]["phase"] == "transient"
    assert retry_meta[0]["reason"] == "transient_http"


def test_chat_retries_connection_error_then_succeeds(monkeypatch, client_stream_finto):
    _patch_retry(monkeypatch)
    client = client_stream_finto([
        httpx.ConnectError("Connection refused"),
        (200, {"id": "chatcmpl-1", "choices": []}),
    ])
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)

    retry_meta: list[dict] = []
    asyncio.run(lmstudio_client.chat({"model": "gemma", "messages": []}, retry_meta=retry_meta))

    assert client.stream.call_count == 2
    assert retry_meta[0]["reason"] == "connection"


def test_chat_raises_after_transient_retries_exhausted(monkeypatch, client_stream_finto):
    _patch_retry(monkeypatch)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 0)
    client = client_stream_finto([(502, {"error": "busy"})] * 3)
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)

    try:
        asyncio.run(lmstudio_client.chat({"model": "gemma", "messages": []}))
        raised = False
    except lmstudio_client.LMStudioError:
        raised = True

    assert raised
    assert client.stream.call_count == 3


def test_is_transient_chat_error_detects_busy_message():
    assert lmstudio_client._is_transient_chat_error(502, "Model busy, try again")
    assert lmstudio_client._is_transient_chat_error(503, "")
    assert not lmstudio_client._is_transient_chat_error(400, "bad request")
