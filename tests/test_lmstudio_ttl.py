"""Test TTL LM Studio imposto dal gateway."""

from __future__ import annotations

import lmstudio_client
import config


def test_prepare_chat_body_injects_ttl(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_TTL_S", 86400)
    body = lmstudio_client._prepare_chat_body({"model": "gemma-4-26b-a4b-it", "messages": []})
    assert body["ttl"] == 86400


def test_prepare_chat_body_no_ttl_when_zero(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_TTL_S", 0)
    body = lmstudio_client._prepare_chat_body({"model": "gemma-4-26b-a4b-it", "messages": []})
    assert "ttl" not in body
