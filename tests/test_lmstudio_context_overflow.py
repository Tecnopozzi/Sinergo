"""Test classificazione context overflow LM Studio → 413 gateway."""

from __future__ import annotations

import asyncio

import config
import lmstudio_client


def test_chat_context_overflow_returns_413(monkeypatch, client_stream_finto):
    monkeypatch.setattr(config, "LMSTUDIO_CHAT_RETRY_MAX", 3)
    monkeypatch.setattr(config, "LMSTUDIO_RETRY_MAX", 0)
    detail = (
        'Engine protocol predict request returned 400: {"error":{"code":400,'
        '"message":"request (13389 tokens) exceeds the available context size (8192 tokens)",'
        '"type":"exceed_context_size_error"}}'
    )
    client = client_stream_finto([(400, {"error": detail})])
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)

    retry_meta: list[dict] = []
    try:
        asyncio.run(
            lmstudio_client.chat(
                {"model": "gemma-4-26b-a4b-it", "messages": []},
                retry_meta=retry_meta,
            )
        )
        assert False, "expected LMStudioError"
    except lmstudio_client.LMStudioError as exc:
        assert exc.status_code == 413
        assert exc.error_kind == "context_overflow"
        assert exc.upstream_status == 400
        assert retry_meta == []
