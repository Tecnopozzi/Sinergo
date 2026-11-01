"""Proxy /v1/decide verso DECIDER_URL."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import server

CHIAVE = "chiave-di-prova"


@pytest.fixture
def locale(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "DECIDER_URL", "http://127.0.0.1:8098")
    monkeypatch.setattr(config, "DECIDER_API_KEY", "chiave-dec")
    monkeypatch.setattr(config, "DECIDER_MODELS", ["laya", "decision-4b"])
    return TestClient(server.app, client=("127.0.0.1", 50000))


def test_decide_proxy_inoltra_e_toglie_prefisso(locale, monkeypatch):
    visti: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        visti["url"] = str(request.url)
        visti["auth"] = request.headers.get("authorization")
        visti["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={"scelta": "avanza", "probabilita": {"avanza": 0.9}, "model": "laya"},
        )

    monkeypatch.setattr(
        server,
        "_decider_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(30.0)),
    )

    r = locale.post(
        "/v1/decide",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={
            "model": "decider:laya",
            "stato": "ciao",
            "opzioni": [{"chiave": "avanza", "descrizione": "ok"}],
            "istruzioni": "scegli",
        },
    )
    assert r.status_code == 200
    assert r.json()["scelta"] == "avanza"
    assert visti["url"] == "http://127.0.0.1:8098/v1/decide"
    assert visti["auth"] == "Bearer chiave-dec"
    assert visti["body"]["model"] == "laya"


def test_decide_senza_url_da_404(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "DECIDER_URL", "")
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    r = c.post(
        "/v1/decide",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={"model": "laya", "stato": "x", "opzioni": [{"chiave": "a", "descrizione": "a"}], "istruzioni": "?"},
    )
    assert r.status_code == 404


def test_models_elenco_include_decider(monkeypatch):
    monkeypatch.setattr(config, "DECIDER_URL", "http://127.0.0.1:8098")
    monkeypatch.setattr(config, "DECIDER_MODELS", ["laya", "decision-4b"])
    monkeypatch.setattr(config, "LMSTUDIO_ENABLED", False)
    monkeypatch.setattr(config, "MODEL_ALLOWLIST", [])

    async def empty_tags():
        return []

    monkeypatch.setattr(server, "list_tags", empty_tags)
    server._MODELS_CACHE["at"] = 0.0
    server._MODELS_CACHE["data"] = []

    models = asyncio.run(server._get_models_cached())
    ids = {m["id"] for m in models}
    assert "decider:laya" in ids
    assert "decider:decision-4b" in ids
