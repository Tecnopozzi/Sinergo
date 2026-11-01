"""Scaricamento amministrativo con trasporto HTTP in memoria, senza porte o GPU."""

import asyncio
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import server


@pytest.fixture
def swap(monkeypatch):
    viste = []
    risposta = {"status": 200, "body": {"status": "ok", "unloaded": ["finto"]}}

    async def invia(request):
        if request.url != "http://127.0.0.1:5060/unload":
            raise httpx.ConnectError("Server finto non disponibile", request=request)
        viste.append((request.method, request.url.path))
        if "entrato" in risposta:
            risposta["entrato"].set()
            await asyncio.to_thread(risposta["rilascia"].wait, 5)
        body = risposta["body"]
        return httpx.Response(risposta["status"], **({"text": body} if isinstance(body, str) else {"json": body}))

    inizializza = httpx.AsyncClient.__init__

    def in_memoria(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(invia))
        inizializza(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", in_memoria)
    yield "http://127.0.0.1:5060", viste, risposta


@pytest.fixture
def locale(monkeypatch, swap):
    monkeypatch.setattr(config, "API_KEY", "unload-test")
    monkeypatch.setattr(config, "LMSTUDIO_HOST", swap[0])
    monkeypatch.setattr(config, "ADMIN_UNLOAD_COOLDOWN_S", 60, raising=False)
    monkeypatch.setattr(server, "_ADMIN_UNLOAD_LAST_MONO", None, raising=False)
    return TestClient(server.app)


HEADERS = {"Authorization": "Bearer unload-test"}


def test_unload_inoltra_get_e_registra_esito(locale, swap, caplog):
    with caplog.at_level("INFO"):
        r = locale.post("/admin/unload", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "upstream": swap[2]["body"]}
    assert swap[1] == [("GET", "/unload")]
    assert "admin_unload_success" in caplog.text


def test_swap_con_risposta_testuale(locale, swap):
    swap[2]["body"] = "Models unloaded"
    r = locale.post("/admin/unload", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "upstream": "Models unloaded"}


@pytest.mark.asyncio
async def test_cooldown_prenotato_prima_della_risposta(locale, swap):
    entrato, rilascia = threading.Event(), threading.Event()
    swap[2].update(entrato=entrato, rilascia=rilascia)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://testserver") as client:
        prima = asyncio.create_task(client.post("/admin/unload", headers=HEADERS))
        try:
            assert await asyncio.to_thread(entrato.wait, 3)
            seconda = await asyncio.wait_for(client.post("/admin/unload", headers=HEADERS), 2)
            assert seconda.status_code == 429
            assert len(swap[1]) == 1
        finally:
            rilascia.set()
            risultato = await prima
        assert risultato.status_code == 200


def test_cooldown_retry_after_e_scadenza(locale, swap, monkeypatch):
    assert locale.post("/admin/unload", headers=HEADERS).status_code == 200
    r = locale.post("/admin/unload", headers=HEADERS)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert len(swap[1]) == 1
    monkeypatch.setattr(server, "_ADMIN_UNLOAD_LAST_MONO", time.monotonic() - 60)
    assert locale.post("/admin/unload", headers=HEADERS).status_code == 200
    assert len(swap[1]) == 2


def test_cooldown_configurabile(locale, swap, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_UNLOAD_COOLDOWN_S", 120)
    assert locale.post("/admin/unload", headers=HEADERS).status_code == 200
    r = locale.post("/admin/unload", headers=HEADERS)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 119
    monkeypatch.setattr(config, "ADMIN_UNLOAD_COOLDOWN_S", 0)
    assert locale.post("/admin/unload", headers=HEADERS).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer errata"}])
def test_auth_non_inoltra_ne_consuma_cooldown(locale, swap, headers):
    assert locale.post("/admin/unload", headers=headers).status_code == 401
    assert swap[1] == []
    assert locale.post("/admin/unload", headers={"X-API-Key": "unload-test"}).status_code == 200


@pytest.mark.parametrize("errore", ["connessione", "http"])
def test_errore_swap_502_e_cooldown(locale, swap, monkeypatch, caplog, errore):
    if errore == "connessione":
        monkeypatch.setattr(config, "LMSTUDIO_HOST", "http://127.0.0.1:9")
    else:
        swap[2].update(status=503, body={"error": "swap occupato"})
    with caplog.at_level("WARNING"):
        r = locale.post("/admin/unload", headers=HEADERS)
    assert r.status_code == 502
    assert "llama-swap" in r.json()["detail"]
    assert "admin_unload_failed" in caplog.text
    assert locale.post("/admin/unload", headers=HEADERS).status_code == 429


@pytest.mark.parametrize("valore,atteso", [(None, 60), ("120", 120), ("0", 0), ("-1", 0), ("errato", 60)])
def test_config_cooldown_da_ambiente(valore, atteso):
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_UNLOAD_COOLDOWN_S"}
    if valore is not None:
        env["ADMIN_UNLOAD_COOLDOWN_S"] = valore
    r = subprocess.run([sys.executable, "-c", "import config; print(config.ADMIN_UNLOAD_COOLDOWN_S)"], env=env, text=True, capture_output=True, check=True)
    assert int(r.stdout.strip()) == atteso
