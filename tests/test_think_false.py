"""Test think:false verso Ollama per modelli thinking."""

from __future__ import annotations

import pytest

from schemas import ChatCompletionRequest, ChatMessage
import server


def _req(model: str, **kwargs) -> ChatCompletionRequest:
    payload = {"model": model, "messages": [ChatMessage(role="user", content="ciao")]}
    payload.update(kwargs)
    return ChatCompletionRequest.model_validate(payload)


@pytest.fixture(autouse=True)
def _models_cache():
    server._MODELS_CACHE["at"] = 9999999999.0
    server._MODELS_CACHE["data"] = [
        {"id": "qwen3:14b", "capabilities": ["chat", "thinking", "tools"]},
        {"id": "qwen2.5:14b-instruct", "capabilities": ["chat", "tools"]},
    ]
    yield
    server._MODELS_CACHE["data"] = []
    server._MODELS_CACHE["at"] = 0.0


def test_thinking_model_forces_think_false():
    body = server._build_ollama_body(_req("qwen3:14b"))
    assert body.get("think") is False


def test_non_thinking_model_no_think_key():
    body = server._build_ollama_body(_req("qwen2.5:14b-instruct"))
    assert "think" not in body


def test_enable_thinking_skips_think_false():
    body = server._build_ollama_body(
        _req("qwen3:14b", chat_template_kwargs={"enable_thinking": True})
    )
    assert "think" not in body


def test_client_think_forwarded():
    body = server._build_ollama_body(_req("qwen3:14b", think=True))
    assert body.get("think") is True


def test_reasoning_effort_skips_think_false():
    body = server._build_ollama_body(_req("qwen3:14b", reasoning_effort="high"))
    assert "think" not in body
