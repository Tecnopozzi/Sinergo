"""Fotografia KV attraverso un llama-server finto."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

import config
import lmstudio_client
import server


CHIAVE = "chiave-kv"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    return TestClient(server.app)


def test_prefisso_renderizza_taglia_esattamente_e_scalda(monkeypatch):
    viste = []

    def finto(request: httpx.Request) -> httpx.Response:
        viste.append((request.url.path, request.url.query.decode(), request.read()))
        if request.url.path.endswith("/apply-template"):
            return httpx.Response(200, json={"prompt": "<system>TESTA ESATTA</system>\n<assistant>"})
        if request.url.path.endswith("/completion"):
            return httpx.Response(200, json={"content": "", "timings": {"prompt_n": 3}})
        return httpx.Response(404, json={"error": "rotta inattesa"})

    client = httpx.AsyncClient(
        base_url="http://swap", transport=httpx.MockTransport(finto)
    )
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
    locale = _client(monkeypatch)

    risposta = locale.post(
        "/v1/kv/prefisso",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={
            "model": "lmstudio:qwen3.6-35b-a3b",
            "messages": [{"role": "system", "content": "TESTA ESATTA"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "fino_a": "TESTA ESATTA",
        },
    )

    assert risposta.status_code == 200
    assert [v[0] for v in viste] == [
        "/upstream/qwen3.6-35b-a3b/apply-template",
        "/upstream/qwen3.6-35b-a3b/completion",
    ]
    corpo = httpx.Request("POST", "http://x", content=viste[1][2]).read().decode()
    assert '"prompt":"<system>TESTA ESATTA"' in corpo
    assert "</system>" not in corpo
    assert '"n_predict":0' in corpo
    assert '"cache_prompt":true' in corpo


def test_slot_inoltra_save_e_restore(monkeypatch):
    viste = []

    def finto(request: httpx.Request) -> httpx.Response:
        viste.append((request.url.path, request.url.query.decode(), request.read()))
        return httpx.Response(200, json={"tokens_saved": 10})

    client = httpx.AsyncClient(
        base_url="http://swap", transport=httpx.MockTransport(finto)
    )
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
    locale = _client(monkeypatch)
    headers = {"X-API-Key": CHIAVE}

    for azione in ("save", "restore"):
        risposta = locale.post(
            "/v1/kv/slot", headers=headers,
            json={"model": "lmstudio:qwen3.6-35b-a3b", "azione": azione,
                  "nome": "kairos-testa.bin"},
        )
        assert risposta.status_code == 200

    assert [(p, q) for p, q, _ in viste] == [
        ("/upstream/qwen3.6-35b-a3b/slots/0", "action=save"),
        ("/upstream/qwen3.6-35b-a3b/slots/0", "action=restore"),
    ]


def test_errori_upstream_diventano_502_chiaro(monkeypatch):
    client = httpx.AsyncClient(
        base_url="http://swap",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, json={"error": "slot rotto"})
        ),
    )
    monkeypatch.setattr(lmstudio_client, "get_client", lambda: client)
    locale = _client(monkeypatch)

    risposta = locale.post(
        "/v1/kv/slot",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={"model": "lmstudio:qwen3.6-35b-a3b", "azione": "restore",
              "nome": "kairos-testa.bin"},
    )

    assert risposta.status_code == 502
    assert "slot rotto" in risposta.text


def test_rotte_kv_sono_autenticate(monkeypatch):
    locale = _client(monkeypatch)
    assert locale.post("/v1/kv/prefisso", json={}).status_code == 401
    assert locale.post("/v1/kv/slot", json={}).status_code == 401
