"""Input ostili riprodotti con ASGI in memoria, nessuna connessione reale."""

import json
from pathlib import PureWindowsPath

from fastapi.testclient import TestClient
import httpx
import pytest

import config
import server


CHIAVE = "chiave-sintetica-seconda-revisione"
HEADERS = {"Authorization": f"Bearer {CHIAVE}"}


@pytest.mark.parametrize("numero", ["NaN", "Infinity", "-Infinity"])
def test_numero_non_finito_annidato_produce_jsonl_standard(monkeypatch, tmp_path, numero):
    registro = tmp_path / "richieste.jsonl"
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", registro)
    monkeypatch.setattr(config, "NON_STREAM_HEARTBEAT_INTERVAL_S", 0)

    async def inferenza(*args, **kwargs):
        return {"message": {"role": "assistant", "content": "ok"}}

    monkeypatch.setattr(server, "chat", inferenza)
    client = TestClient(server.app, raise_server_exceptions=False)
    corpo = '{"model":"m","messages":[{"role":"user","content":"ciao","extra":{"annidato":[' + numero + ']}}]}'
    risposta = client.post("/v1/chat/completions", content=corpo, headers={**HEADERS, "Content-Type": "application/json"})
    assert risposta.status_code == 200, risposta.text
    prima = client.get("/debug/requests", headers=HEADERS)
    seconda = client.get("/debug/requests", headers=HEADERS)
    assert prima.status_code == seconda.status_code == 200
    record = prima.json()["records"][0]
    assert record["request_payload"]["messages"][0]["extra"]["annidato"] == [None]
    # Il file resta JSON interoperabile anche con parser che vietano NaN/Infinity.
    for riga in registro.read_text(encoding="utf-8").splitlines():
        json.loads(riga, parse_constant=lambda valore: pytest.fail(f"Costante non JSON: {valore}"))


@pytest.mark.parametrize("nome", ["NUL", "nul.bin", "CON", "con.txt", "PRN", "AUX", "COM1", "com9.bin", "LPT1", "lpt9.bin", ".", ".."])
def test_slot_kv_rifiuta_dispositivi_windows_prima_del_backend(monkeypatch, nome):
    assert nome in {".", ".."} or PureWindowsPath(nome).is_reserved()
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", None)
    viste = []

    def finto(richiesta):
        viste.append(json.loads(richiesta.content))
        return httpx.Response(200, json={"tokens_saved": 10})

    client_backend = httpx.AsyncClient(base_url="http://swap", transport=httpx.MockTransport(finto))
    monkeypatch.setattr(server.lmstudio_client, "get_client", lambda: client_backend)
    locale = TestClient(server.app)
    risposta = locale.post("/v1/kv/slot", headers=HEADERS,
                           json={"model": "lmstudio:m", "azione": "save", "nome": nome})
    assert risposta.status_code == 422
    assert not viste


@pytest.mark.parametrize("nome", ["kairos-testa.bin", "connesso.bin", "com10.bin", "nul_cache.bin"])
def test_slot_kv_conserva_nomi_ordinari(nome):
    from schemas import KvSlotRequest

    assert KvSlotRequest(model="m", azione="save", nome=nome).nome == nome
