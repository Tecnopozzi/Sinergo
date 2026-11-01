"""Server con chiave obbligatoria, client proxy, installazione."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

import cliente
import auth
import config
import installa
import server

CHIAVE = "chiave-di-prova"


@pytest.fixture
def locale(monkeypatch):
    """Il server chiamato da 127.0.0.1, con la chiave configurata."""
    monkeypatch.setattr(config, "API_KEY", CHIAVE)

    async def modelli_finti():
        return []

    # Anche /health resta isolato dai backend reali durante la scansione delle route.
    monkeypatch.setattr(server, "list_tags", modelli_finti)
    monkeypatch.setattr(server.lmstudio_client, "list_models", modelli_finti)
    return TestClient(server.app, client=("127.0.0.1", 50000))


def test_server_rifiuta_senza_chiave_anche_da_localhost(locale):
    assert locale.get("/").status_code == 401
    assert locale.get("/", headers={"Authorization": "Bearer sbagliata"}).status_code == 401


def test_server_accetta_con_chiave(locale):
    assert locale.get("/", headers={"Authorization": f"Bearer {CHIAVE}"}).status_code == 200
    assert locale.get("/", headers={"X-API-Key": CHIAVE}).status_code == 200


def test_server_senza_api_key_rifiuta_tutto(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    assert TestClient(server.app).get("/", headers={"Authorization": "Bearer "}).status_code == 401


def test_nessuna_route_aperta_salvo_health_e_pagina(locale):
    aperte = set()
    for route in server.app.routes:
        percorso = route.path.replace("{job_id}", "x").replace("{model_id}", "x")
        for metodo in sorted(getattr(route, "methods", None) or {"GET"}):
            if metodo in ("HEAD", "OPTIONS"):
                continue
            auth._fallimenti.clear()
            if locale.request(metodo, percorso).status_code != 401:
                aperte.add(route.path)
    assert aperte == {"/health", "/dashboard"} | ({"/admin/pull-restart"} if config.PULL_RESTART_HTTP_KEY else set())


def test_rigenera_chiave_invalida_la_vecchia(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("# commento\nAPI_KEY=vecchia\nLMSTUDIO_ENABLED=1\n", encoding="utf-8")
    assert installa.installa_server(env) == "vecchia"
    nuova = installa.installa_server(env, rigenera=True)
    assert nuova != "vecchia" and len(nuova) >= 40
    assert nuova in capsys.readouterr().out
    letto = installa.leggi_env(env)
    assert letto["API_KEY"] == nuova and letto["LMSTUDIO_ENABLED"] == "1" and letto["LOCALROUTER_MODO"] == "server"
    assert "# commento" in env.read_text(encoding="utf-8")

    monkeypatch.setattr(config, "API_KEY", letto["API_KEY"])
    c = TestClient(server.app)
    assert c.get("/", headers={"Authorization": "Bearer vecchia"}).status_code == 401
    assert c.get("/", headers={"Authorization": f"Bearer {nuova}"}).status_code == 200


def test_client_ascolta_solo_su_localhost(monkeypatch):
    visti = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: visti.update(kw))
    monkeypatch.setattr(config, "MODO", "client")
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "SERVER_REMOTO", "https://gpu.example")
    monkeypatch.setattr(config, "CHIAVE_REMOTA", CHIAVE)
    server.main()  # l'avvio unico: legge MODO e lancia il client
    assert visti["host"] == "127.0.0.1"


# --- client contro un'app ASGI in memoria, senza servizi -------------------

_secondo_pezzo = threading.Event()
finto = FastAPI()


@finto.get("/health")
async def _health():
    return {"server_name": "finto"}


@finto.get("/v1/models")
async def _modelli(request: Request):
    if request.headers.get("authorization") != f"Bearer {CHIAVE}":
        return JSONResponse({"detail": "no"}, status_code=401)
    return {
        "object": "list",
        "data": [
            {
                "id": "lmstudio:google/gemma-4-26b-a4b",
                "object": "model",
                "created": 0,
                "owned_by": "lmstudio",
                "capabilities": ["chat", "tools", "vision", "thinking"],
                "context_window": 262144,
            },
            {"id": "qwen2.5:7b", "object": "model", "created": 0, "owned_by": "ollama"},
        ],
    }


@finto.post("/v1/chat/completions")
async def _chat_completions(request: Request):
    if request.headers.get("authorization") != f"Bearer {CHIAVE}":
        return JSONResponse({"detail": "no"}, status_code=401)
    body = await request.json()
    assert body["model"] == "qwen2.5:7b"
    assert body["messages"] == [{"role": "user", "content": "ciao"}]
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    return {
        "id": "chatcmpl-prova",
        "object": "chat.completion",
        "created": 1720000000,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "salve"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


@finto.post("/v1/eco")
async def _eco(request: Request):
    return {
        "authorization": request.headers.get("authorization"),
        "user_agent": request.headers.get("user-agent"),
        "corpo": (await request.body()).decode(),
        "query": request.url.query,
        "accept_encoding": request.headers.get("accept-encoding"),
    }


@finto.get("/v1/sse")
async def _sse():
    async def pezzi():
        yield b"data: 1\n\n"
        await asyncio.to_thread(_secondo_pezzo.wait, 10)
        yield b"data: 2\n\n"

    return StreamingResponse(pezzi(), media_type="text/event-stream")


@pytest.fixture
def remoto(monkeypatch):
    url = "http://127.0.0.1:5060"

    class Pezzi(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: 1\n\n"
            assert await asyncio.to_thread(_secondo_pezzo.wait, 2), "Il proxy ha trattenuto il primo pezzo"
            yield b"data: 2\n\n"

    async def invia(request):
        if request.url.host != "127.0.0.1" or request.url.port != 5060:
            raise httpx.ConnectError("Server finto non disponibile", request=request)
        if request.url.path == "/v1/sse":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Pezzi())
        return await httpx.ASGITransport(app=finto).handle_async_request(request)

    def invia_sincrono(request):
        if request.url.host != "127.0.0.1" or request.url.port != 5060:
            raise httpx.ConnectError("Server finto non disponibile", request=request)
        with TestClient(finto) as client:
            risposta = client.request(request.method, str(request.url), headers=request.headers, content=request.content)
        return httpx.Response(risposta.status_code, headers=risposta.headers, content=risposta.content)

    inizializza_async = httpx.AsyncClient.__init__
    inizializza_sync = httpx.Client.__init__

    def async_in_memoria(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(invia))
        inizializza_async(self, *args, **kwargs)

    def sync_in_memoria(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(invia_sincrono))
        inizializza_sync(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", async_in_memoria)
    monkeypatch.setattr(httpx.Client, "__init__", sync_in_memoria)
    yield url


@pytest.fixture
def proxy(remoto):
    with TestClient(cliente.crea_app(remoto, CHIAVE), base_url="http://127.0.0.1") as client:
        yield client


def test_client_inoltra_con_chiave_e_user_agent(proxy):
    r = proxy.post("/v1/eco?a=1", content=b"ciao", headers={"Authorization": "Bearer dell-utente"})
    assert r.status_code == 200
    assert {k: v for k, v in r.json().items() if k != "accept_encoding"} == {
        "authorization": f"Bearer {CHIAVE}", "user_agent": "Mozilla/5.0", "corpo": "ciao", "query": "a=1"}
    assert proxy.get("/v1/models").json()["data"][0]["id"] == "finto/lmstudio:google/gemma-4-26b-a4b"


def test_client_espone_lista_modelli_lmstudio_v1(proxy):
    r = proxy.get("/api/v1/models")
    assert r.status_code == 200
    assert r.json() == {
        "models": [
            {
                "type": "llm",
                "publisher": "finto",
                "key": "finto/lmstudio:google/gemma-4-26b-a4b",
                "display_name": "finto/lmstudio:google/gemma-4-26b-a4b",
                "architecture": None,
                "quantization": None,
                "size_bytes": 0,
                "params_string": None,
                "loaded_instances": [],
                "max_context_length": 262144,
                "format": "gguf",
                "capabilities": {
                    "vision": True,
                    "trained_for_tool_use": True,
                    "reasoning": {"allowed_options": ["off", "on"], "default": "on"},
                },
                "description": None,
            },
            {
                "type": "llm",
                "publisher": "finto",
                "key": "finto/qwen2.5:7b",
                "display_name": "finto/qwen2.5:7b",
                "architecture": None,
                "quantization": None,
                "size_bytes": 0,
                "params_string": None,
                "loaded_instances": [],
                "max_context_length": None,
                "format": "gguf",
                "capabilities": {"vision": False, "trained_for_tool_use": False},
                "description": None,
            },
        ]
    }


def test_client_espone_lista_modelli_lmstudio_v0(proxy):
    r = proxy.get("/api/v0/models")
    assert r.status_code == 200
    assert r.json()["data"][0] == {
        "id": "finto/lmstudio:google/gemma-4-26b-a4b",
        "object": "model",
        "type": "llm",
        "publisher": "finto",
        "arch": None,
        "compatibility_type": "gguf",
        "quantization": None,
        "state": "unknown",
        "max_context_length": 262144,
    }


def test_client_espone_lista_modelli_ollama_tags(proxy):
    r = proxy.get("/api/tags")
    assert r.status_code == 200
    assert r.json()["models"][0] == {
        "name": "finto/lmstudio:google/gemma-4-26b-a4b:latest",
        "model": "finto/lmstudio:google/gemma-4-26b-a4b:latest",
        "modified_at": "1970-01-01T00:00:00Z",
        "size": 0,
        "digest": "",
        "loaded": None,
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "gemma",
            "families": ["gemma"],
            "parameter_size": "",
            "quantization_level": "",
        },
    }


def test_client_espone_show_e_version_ollama(proxy):
    show = proxy.post("/api/show", json={"model": "finto/lmstudio:google/gemma-4-26b-a4b:latest"})
    assert show.status_code == 200
    assert show.json()["details"]["family"] == "gemma"
    assert show.json()["capabilities"] == ["completion", "tools", "vision", "thinking"]

    version = proxy.get("/api/version")
    assert version.status_code == 200
    assert version.json()["version"].startswith("localrouter-client-")


def test_client_ollama_chat_inoltra_alla_chat_openai(proxy):
    r = proxy.post(
        "/api/chat",
        json={
            "model": "qwen2.5:7b",
            "messages": [{"role": "user", "content": "ciao"}],
            "stream": False,
            "options": {"temperature": 0.2},
        },
    )
    assert r.status_code == 200
    assert r.json() == {
        "model": "qwen2.5:7b",
        "created_at": "1970-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": "salve"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 3,
        "eval_count": 2,
    }


def test_client_non_comprime_se_il_chiamante_non_lo_chiede(proxy):
    """24/09: httpx chiedeva gzip al server per conto suo e il client passava i
    byte compressi a curl, che non li aveva chiesti (0x8b in testa al JSON)."""
    richiesta = proxy.build_request("POST", "/v1/eco")
    richiesta.headers.pop("accept-encoding", None)
    visto = proxy.send(richiesta).json()
    assert visto["accept_encoding"] in (None, "identity")
    r = proxy.post("/v1/eco", headers={"Accept-Encoding": "gzip"})
    assert r.json()["accept_encoding"] == "gzip"


@pytest.mark.anyio
async def test_client_inoltra_lo_stream_a_pezzi(proxy):
    _secondo_pezzo.clear()
    pezzi = []

    async def ricevi():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def invia(messaggio):
        if messaggio["type"] == "http.response.body" and messaggio.get("body"):
            pezzi.append(messaggio["body"])
            if len(pezzi) == 1:
                assert pezzi[0] == b"data: 1\n\n"
                assert not _secondo_pezzo.is_set()
                _secondo_pezzo.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/v1/sse",
             "raw_path": b"/v1/sse", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80)}
    await asyncio.wait_for(proxy.app(scope, ricevi, invia), 3)
    assert pezzi == [b"data: 1\n\n", b"data: 2\n\n"]


def test_installa_client_rifiuta_chiave_e_indirizzo_sbagliati(remoto, tmp_path):
    env = tmp_path / ".env"
    with pytest.raises(SystemExit, match="rifiuta la chiave"):
        installa.installa_client(remoto, "sbagliata", env)
    with pytest.raises(SystemExit, match="non raggiungibile"):
        installa.installa_client("http://127.0.0.1:9", CHIAVE, env)
    assert not env.exists()
    installa.installa_client(remoto, CHIAVE, env)
    assert installa.leggi_env(env) == {"LOCALROUTER_MODO": "client", "LOCALROUTER_SERVER": remoto, "LOCALROUTER_CHIAVE": CHIAVE}


def test_server_stato_runtime_non_deriva_dal_catalogo(locale, monkeypatch):
    async def catalogo():
        return [{'id': 'lmstudio:a', 'owned_by': 'lmstudio'}, {'id': 'b:latest'}, {'id': 'decider:c', 'owned_by': 'decider'}]

    caricati = {'a': 4096}

    def risposta(request):
        assert request.url.path == '/api/ps'
        return httpx.Response(200, json={'models': [{'name': 'b:latest', 'context_length': 2048}]})

    monkeypatch.setattr(server, '_get_models_cached', catalogo)
    monkeypatch.setattr(config, 'LMSTUDIO_ENABLED', True)
    monkeypatch.setitem(server._MODELS_CACHE, 'lmstudio_ok', True)
    monkeypatch.setattr(config, 'LMSTUDIO_PROXY_PURE', False)

    def risposta_lm(request):
        assert request.url.path == '/api/v0/models'
        return httpx.Response(200, json={'data': [{'id': k, 'state': 'loaded', 'loaded_context_length': v} for k, v in caricati.items()]})

    meta = httpx.AsyncClient(transport=httpx.MockTransport(risposta_lm), base_url='http://lmstudio')
    monkeypatch.setattr(server.lmstudio_client, 'get_meta_client', lambda: meta)
    import ollama_client
    client = httpx.AsyncClient(transport=httpx.MockTransport(risposta), base_url='http://ollama')
    monkeypatch.setattr(ollama_client, 'get_client', lambda: client)
    try:
        for attivo in (True, False):
            if not attivo:
                caricati.clear()
            r = locale.get('/v1/models?runtime=true', headers={'X-API-Key': CHIAVE})
            assert r.status_code == 200
            dati = r.json()['data']
            assert dati[0]['loaded'] is attivo
            assert dati[0]['state'] == ('loaded' if attivo else 'not-loaded')
            assert dati[1]['loaded'] is True
            assert dati[1]['context_length'] == 2048
            assert dati[2]['loaded'] is None
    finally:
        asyncio.run(client.aclose())
        asyncio.run(meta.aclose())


@pytest.mark.parametrize('swap', [False, True])
@pytest.mark.parametrize('stato_http', [200, 503])
def test_runtime_lmstudio_e_llamaswap_non_confondono_pronto_e_avvio(monkeypatch, swap, stato_http):
    monkeypatch.setattr(config, 'LMSTUDIO_PROXY_PURE', swap)

    def risposta(request):
        assert request.url.path == ('/running' if swap else '/api/v0/models')
        items = [
            {('model' if swap else 'id'): 'pronto', 'state': 'ready' if swap else 'loaded'},
            {('model' if swap else 'id'): 'avvio', 'state': 'starting' if swap else 'not-loaded'},
        ]
        return httpx.Response(stato_http, json={('running' if swap else 'data'): items})

    async def verifica():
        async with httpx.AsyncClient(transport=httpx.MockTransport(risposta), base_url='http://backend') as meta:
            monkeypatch.setattr(server.lmstudio_client, 'get_meta_client', lambda: meta)
            data = await server._models_with_runtime([{'id': 'lmstudio:' + n} for n in ('pronto', 'avvio', 'spento')])
        assert [m['loaded'] for m in data] == ([True, False, False] if stato_http == 200 else [None] * 3)
        assert [m['state'] for m in data] == (['loaded', 'not-loaded', 'not-loaded'] if stato_http == 200 else ['unknown'] * 3)

    asyncio.run(verifica())


@pytest.mark.parametrize('elenco_yaml', [False, True])
def test_porte_aggiuntive_usano_configurazione_multiserver(monkeypatch, tmp_path, elenco_yaml):
    if elenco_yaml:
        (tmp_path / 'server_remoti.yaml').write_text('gpu: {indirizzo: "http://127.0.0.1:9", chiave: prova}\n')
    monkeypatch.setattr(config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(config, 'SERVER_REMOTO', 'http://127.0.0.1:9' if not elenco_yaml else '')
    monkeypatch.setattr(config, 'CHIAVE_REMOTA', 'prova' if not elenco_yaml else '')
    monkeypatch.setattr(config, 'CLIENT_EXTRA_PORTS', [1234, 11434])
    chiamate = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: chiamate.append((app, kw)))

    class ThreadFinto:
        def __init__(self, target, kwargs, daemon):
            self.target, self.kwargs = target, kwargs
            assert daemon

        def start(self):
            self.target(**self.kwargs)

    monkeypatch.setattr(cliente.threading, 'Thread', ThreadFinto)
    cliente.main()
    assert [kw['port'] for _, kw in chiamate] == [1234, 11434, config.PORT]
    assert all(kw['host'] == '127.0.0.1' for _, kw in chiamate)
    assert len({id(app) for app, _ in chiamate}) == 3
    assert all('/api/ps' in {r.path for r in app.routes} for app, _ in chiamate)


@pytest.mark.parametrize("percorso", ["/v1/systemone", "/v1/rank"])
def test_route_ritirate_non_disponibili(locale, percorso):
    """Una route ritirata restituisce il normale 404, senza invocare servizi."""
    risposta = locale.post(percorso, headers={"X-API-Key": CHIAVE}, json={})
    assert risposta.status_code == 404
    assert risposta.json() == {"detail": "Not Found"}
