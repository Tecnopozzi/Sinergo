"""Instradamento verificato contro due app ASGI in memoria, senza porte o GPU."""

import asyncio
import json
import os
import threading

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

import cliente
import installa


@pytest.fixture
def nodi(monkeypatch):
    applicazioni = {}
    registri = {"5060": [], "3060": []}
    indirizzi = {}
    controlli = {"5060": {}, "3060": {}}
    for nome, modelli in [("5060", ["comune", "solo-a", "lmstudio:qwen/qwen3-vl-8b"]),
                          ("3060", ["comune", "solo-b"])]:
        app = FastAPI()

        def gestore(nome, modelli):
            async def rispondi(request: Request, percorso: str):
                corpo = await request.body()
                registri[nome].append((request.method, percorso, corpo, dict(request.headers), request.url.query))
                if request.headers.get("authorization") != f"Bearer segreto-{nome}":
                    return JSONResponse({"errore": "chiave errata"}, status_code=401)
                if percorso == "health":
                    return {"server_name": nome}
                if percorso == "v1/models":
                    controllo = controlli[nome]
                    if "barriera" in controllo:
                        await asyncio.to_thread(controllo["barriera"].wait, 1)
                    if "ritardo" in controllo:
                        await asyncio.sleep(controllo["ritardo"])
                    if "risposta" in controllo:
                        return JSONResponse(controllo["risposta"], status_code=controllo.get("stato", 200))
                    return {"data": [{"id": m, "owned_by": "origine", "extra": 42} for m in modelli]}
                if percorso.endswith("events") or percorso == "sse" or (percorso == "v1/chat/completions" and json.loads(corpo).get("stream")):
                    async def eventi():
                        yield b"data: primo\n\n"
                        await asyncio.sleep(0.03)
                        yield b"data: [DONE]\n\n"
                    return StreamingResponse(eventi(), media_type="text/event-stream")
                if percorso in ("process", "media/process", "v1/chat/jobs"):
                    return JSONResponse({"job_id": "lavoro-1", "status": "queued", "extra": 42}, status_code=202)
                return {"server": nome, "path": percorso, "body": corpo.decode("utf-8", errors="replace")}
            return rispondi

        app.api_route("/{percorso:path}", methods=["GET", "POST", "DELETE"])(gestore(nome, modelli))
        indirizzi[nome] = f"http://127.0.0.1:{nome}"
        applicazioni[nome] = app

    def destinazione(request):
        nome = str(request.url.port)
        if request.url.host != "127.0.0.1" or nome not in applicazioni or controlli[nome].get("spento"):
            raise httpx.ConnectError("Server finto non disponibile", request=request)
        return nome

    async def invia(request):
        nome = destinazione(request)
        trasporto = httpx.ASGITransport(app=applicazioni[nome])
        # ASGITransport non ha timeout di rete: riproduciamo quello del client.
        limite = request.extensions.get("timeout", {}).get("read")
        try:
            return await asyncio.wait_for(trasporto.handle_async_request(request), limite)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout("Timeout del server finto", request=request) from None

    def invia_sincrono(request):
        nome = destinazione(request)
        with TestClient(applicazioni[nome]) as client:
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
    yield {n: {"indirizzo": u, "chiave": f"segreto-{n}"} for n, u in indirizzi.items()}, registri, applicazioni, controlli


@pytest.fixture
def proxy(nodi):
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        yield c


def test_catalogo_unito_e_cache(proxy, nodi):
    dati = proxy.get("/v1/models").json()["data"]
    assert [m["id"] for m in dati] == ["5060/comune", "5060/solo-a", "5060/lmstudio:qwen/qwen3-vl-8b", "3060/comune", "3060/solo-b"]
    assert [m["owned_by"] for m in dati] == ["5060"] * 3 + ["3060"] * 2
    assert all(m["extra"] == 42 for m in dati)
    proxy.get("/v1/models")
    assert all(sum(r[1] == "v1/models" for r in rs) == 1 for rs in nodi[1].values())


def test_server_spento(nodi):
    nodi[3]["5060"]["spento"] = True
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        assert [m["id"] for m in c.get("/v1/models").json()["data"]] == ["3060/comune", "3060/solo-b"]
        h = c.get("/health").json()["servers"]
        assert [(s["nome"], s["raggiungibile"], s["modelli"]) for s in h] == [("5060", False, []), ("3060", True, ["comune", "solo-b"])]
        assert "segreto" not in c.get("/health").text
        assert c.post("/v1/chat/completions", json={"model": "5060/comune"}).status_code == 502
        assert not any(r[1] == "v1/chat/completions" for r in nodi[1]["3060"])


@pytest.mark.parametrize("percorso", ["v1/chat/completions", "v1/decide", "v1/kv/slot", "v1/audio/speech", "qualsiasi"])
def test_modello_json(proxy, nodi, percorso):
    r = proxy.post("/" + percorso + "?x=1", json={"model": "3060/solo-b", "extra": [1]})
    assert r.status_code == 200 and r.json()["server"] == "3060"
    visto = nodi[1]["3060"][-1]
    assert json.loads(visto[2]) == {"model": "solo-b", "extra": [1]}
    assert visto[3]["authorization"] == "Bearer segreto-3060"
    assert visto[3]["user-agent"] == "Mozilla/5.0" and visto[4] == "x=1"


def test_barra_interna_e_compatibilita(proxy, nodi, caplog):
    assert proxy.post("/v1/chat/completions", json={"model": "5060/lmstudio:qwen/qwen3-vl-8b"}).status_code == 200
    assert json.loads(nodi[1]["5060"][-1][2])["model"] == "lmstudio:qwen/qwen3-vl-8b"
    for modello, nome in [("comune", "5060"), ("solo-b", "3060"), ("lmstudio:qwen/qwen3-vl-8b", "5060")]:
        with caplog.at_level("INFO"):
            r = proxy.post("/v1/chat/completions", json={"model": modello})
        assert r.headers["x-localrouter-server"] == nome and r.json()["server"] == nome
        assert nome in caplog.text
    assert "segreto" not in caplog.text


def test_sconosciuto_non_ripiega(proxy, nodi):
    for url, corpo in [("/v1/chat/completions", {"model": "ignoto/comune"}), ("/ignoto/process", {}), ("/status/ignoto/lavoro-1", {})]:
        r = proxy.post(url, json=corpo) if "status" not in url else proxy.get(url)
        assert r.status_code == 404 and "ignoto" in r.text
    assert not any(r[1] in ("process", "v1/chat/completions") for rs in nodi[1].values() for r in rs)


@pytest.mark.parametrize("inizio,seguiti", [("v1/chat/jobs", ["v1/chat/jobs/{id}", "v1/chat/jobs/{id}/result", "v1/chat/jobs/{id}/events"]),
                                           ("process", ["status/{id}", "result/{id}", "cancel/{id}"]),
                                           ("media/process", ["media/status/{id}", "media/result/{id}", "media/cancel/{id}"])])
def test_lavori(proxy, nodi, inizio, seguiti):
    r = proxy.post("/3060/" + inizio, json={"model": "solo-b"} if inizio.startswith("v1") else {})
    assert r.status_code == 202 and r.json() == {"job_id": "3060/lavoro-1", "status": "queued", "extra": 42}
    for seguito in seguiti:
        url = "/" + seguito.format(id=r.json()["job_id"])
        res = proxy.post(url) if "cancel" in url else proxy.get(url)
        assert res.status_code == 200
        assert nodi[1]["3060"][-1][1] == seguito.format(id="lavoro-1")
    assert not nodi[1]["5060"]


def test_job_con_modello_e_servizi(proxy, nodi):
    assert proxy.post("/v1/chat/jobs", json={"model": "3060/solo-b"}).json()["job_id"] == "3060/lavoro-1"
    assert proxy.post("/5060/process").json()["job_id"] == "5060/lavoro-1"
    proxy.post("/v1/models/unload-all")
    assert nodi[1]["5060"][-1][1] == "v1/models/unload-all"
    assert not any(r[1] == "v1/models/unload-all" for r in nodi[1]["3060"])
    assert proxy.get("/3060/health").json() == {"server_name": "3060"}


@pytest.mark.parametrize("percorso,destinatario", [("/3060/admin/unload", "3060"), ("/admin/unload", "5060")])
def test_admin_unload_solo_destinatario(proxy, nodi, percorso, destinatario):
    r = proxy.post(percorso)
    assert r.status_code == 200 and r.json()["server"] == destinatario
    for nome, registro in nodi[1].items():
        assert [(v[0], v[1]) for v in registro] == ([("POST", "admin/unload")] if nome == destinatario else [])


def test_admin_unload_server_sconosciuto_non_ripiega(proxy, nodi):
    assert proxy.post("/ignoto/admin/unload").status_code == 404
    assert all(not registro for registro in nodi[1].values())


def test_form_e_file(proxy, nodi):
    for kw in [dict(data={"model": "3060/solo-b", "language": "it"}),
               dict(data={"model": "3060/solo-b"}, files={"file": ("audio.bin", b"\x00audio\xff", "application/octet-stream")})]:
        r = proxy.post("/v1/audio/transcriptions", **kw)
        assert r.status_code == 200 and r.json()["server"] == "3060"
        corpo = nodi[1]["3060"][-1][2]
        assert b"3060/solo-b" not in corpo and b"solo-b" in corpo
        if "files" not in kw:
            from urllib.parse import parse_qs
            assert parse_qs(corpo.decode()) == {"model": ["solo-b"], "language": ["it"]}
        if "files" in kw:
            assert corpo.lower().count(b"content-disposition:") == 2
            assert b"\x00audio\xff" in corpo and b"audio.bin" in corpo


def test_sse(proxy):
    r = proxy.get("/3060/sse")
    assert r.content == b"data: primo\n\ndata: [DONE]\n\n"
    assert r.headers["content-type"].startswith("text/event-stream")


def test_legacy_scopre_nome(nodi):
    with TestClient(cliente.crea_app(nodi[0]["5060"]["indirizzo"], "segreto-5060"), base_url="http://127.0.0.1") as c:
        assert c.get("/v1/models").json()["data"][0]["id"] == "5060/comune"


def test_installazione_elenco(nodi, tmp_path, capsys):
    env = tmp_path / ".env"
    for nome, dati in nodi[0].items():
        installa.installa_client(**dati, nome=nome, percorso=env)
    with pytest.raises(SystemExit, match="esiste"):
        installa.installa_client(**nodi[0]["5060"], nome="5060", percorso=env)
    installa.elenca_server(env)
    stampato = capsys.readouterr().out
    assert "5060" in stampato and "3060" in stampato and "segreto" not in stampato
    installa.rimuovi_server("5060", env)
    assert list(cliente.leggi_server(env.parent / "server_remoti.yaml")) == ["3060"]
    with pytest.raises(SystemExit, match="non esiste"):
        installa.rimuovi_server("ignoto", env)


def test_configurazione_non_valida(tmp_path):
    percorso = tmp_path / "server_remoti.yaml"
    for testo in ['"a/b": {indirizzo: "http://localhost", chiave: x}', '"a": {indirizzo: "http://localhost"}', '"a": {indirizzo: "http://localhost", chiave: x}\n"a": {indirizzo: "http://localhost", chiave: y}']:
        percorso.write_text(testo)
        with pytest.raises(ValueError):
            cliente.leggi_server(percorso)


def test_prefissi_discordanti_e_sconosciuti(proxy, nodi):
    assert proxy.post("/5060/v1/chat/completions", json={"model": "3060/solo-b"}).status_code == 400
    assert proxy.get("/5060/status/3060/lavoro-1").status_code == 400
    assert proxy.post("/5060/v1/chat/completions", json={"model": "ignoto/comune"}).status_code == 404
    assert not any(r[1] == "v1/chat/completions" for rs in nodi[1].values() for r in rs)


def test_modello_assente(proxy):
    assert proxy.post("/v1/chat/completions", json={"model": "assente"}).status_code == 404


def test_cache_scade(proxy, nodi, monkeypatch):
    proxy.get("/v1/models")
    monkeypatch.setattr(cliente, "CACHE_SECONDI", -1)
    # Una nuova app usa una scadenza immediata senza attendere 30 secondi.
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        c.get("/v1/models")
        c.get("/v1/models")
    assert all(sum(r[1] == "v1/models" for r in rs) == 3 for rs in nodi[1].values())


def test_rimuovi_ultimo_non_resuscita_legacy(nodi, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    installa.installa_client(**nodi[0]["5060"], nome="5060", percorso=env)
    installa.rimuovi_server("5060", env)
    installa.elenca_server(env)
    assert cliente.leggi_server(env.parent / "server_remoti.yaml") == {}
    monkeypatch.setattr(cliente.config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cliente.config, "SERVER_REMOTO", nodi[0]["5060"]["indirizzo"])
    monkeypatch.setattr(cliente.config, "CHIAVE_REMOTA", "segreto-5060")
    with pytest.raises(SystemExit, match="vuoto"):
        cliente.main()


def test_installa_non_salva_server_errato(nodi, tmp_path):
    env = tmp_path / ".env"
    for dati in [dict(indirizzo=nodi[0]["5060"]["indirizzo"], chiave="errata"), dict(indirizzo="http://127.0.0.1:9", chiave="x")]:
        with pytest.raises(SystemExit):
            installa.installa_client(**dati, nome="nuovo", percorso=env)
        assert not env.exists() and not (env.parent / "server_remoti.yaml").exists()


def test_migrazione_legacy(nodi, tmp_path):
    env = tmp_path / ".env"
    installa.installa_client(**nodi[0]["5060"], percorso=env)
    installa.installa_client(**nodi[0]["3060"], nome="3060", percorso=env)
    assert list(cliente.leggi_server(env.parent / "server_remoti.yaml")) == ["5060", "3060"]
    # Windows non ha i permessi POSIX: chmod 0o600 vi imposta solo la sola lettura,
    # il file resta protetto dai permessi del profilo utente (limite documentato).
    if os.name != "nt":
        assert (env.parent / "server_remoti.yaml").stat().st_mode & 0o777 == 0o600


def test_cli_elenco_rimozione(nodi, tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    # main usa i percorsi di default: le sostituzioni indirizzano soltanto il test.
    vero_installa = installa.installa_client
    vero_elenca = installa.elenca_server
    vero_rimuovi = installa.rimuovi_server
    monkeypatch.setattr(installa, "installa_client", lambda indirizzo, chiave, nome: vero_installa(indirizzo, chiave, env, nome))
    monkeypatch.setattr(installa, "elenca_server", lambda: vero_elenca(env))
    monkeypatch.setattr(installa, "rimuovi_server", lambda nome: vero_rimuovi(nome, env))
    import sys
    monkeypatch.setattr(sys, "argv", ["installa.py", "--client", "--nome", "5060", "--indirizzo", nodi[0]["5060"]["indirizzo"], "--chiave", "segreto-5060"])
    installa.main()
    monkeypatch.setattr(sys, "argv", ["installa.py", "--client", "--elenco"])
    installa.main()
    assert "5060" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["installa.py", "--client", "--rimuovi", "5060"])
    installa.main()
    assert cliente.leggi_server(env.parent / "server_remoti.yaml") == {}


def test_cataloghi_in_parallelo(nodi):
    barriera = threading.Barrier(2)
    for controllo in nodi[3].values():
        controllo["barriera"] = barriera
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        assert len(c.get("/v1/models").json()["data"]) == 5


@pytest.mark.parametrize("controllo", [{"ritardo": 0.2}, {"risposta": {"data": [{"id": "vietato"}]}, "stato": 401}, {"risposta": {"data": "errato"}}, {"risposta": {"data": [{"id": 123}]}}])
def test_catalogo_timeout_o_non_valido(nodi, monkeypatch, controllo):
    monkeypatch.setattr(cliente, "TIMEOUT_CATALOGO", 0.05)
    nodi[3]["5060"].update(controllo)
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        assert [m["id"] for m in c.get("/v1/models").json()["data"]] == ["3060/comune", "3060/solo-b"]


def test_nome_legacy_mancante(nodi):
    # Un endpoint esistente che non espone server_name non diventa un nome inventato.
    with TestClient(cliente.crea_app(nodi[0]["5060"]["indirizzo"] + "/assente", "segreto-5060"), base_url="http://127.0.0.1") as c:
        assert c.get("/v1/models").status_code == 502


@pytest.mark.parametrize("testo", ["[]", "null", '"v1": {indirizzo: "http://localhost", chiave: x}', '"a": {indirizzo: "file:///tmp/x", chiave: x}', '"a": {indirizzo: "https://utente:segreto@example.test", chiave: x}', '"a": {indirizzo: "http://localhost", chiave: ""}', '"a": 1', '"a": ['])
def test_validazione_config_senza_segreti(tmp_path, testo):
    registro = tmp_path / "server_remoti.yaml"
    registro.write_text(testo)
    with pytest.raises(ValueError) as errore:
        cliente.leggi_server(registro)
    assert "segreto" not in str(errore.value)


def test_installa_rifiuta_nome_non_valido(nodi, tmp_path):
    with pytest.raises(SystemExit):
        installa.installa_client(**nodi[0]["5060"], nome="a/b", percorso=tmp_path / ".env")
    assert not (tmp_path / "server_remoti.yaml").exists()


def test_probe_rifiuta_http_non_200(nodi):
    nodi[3]["5060"].update({"risposta": {}, "stato": 503})
    with pytest.raises(SystemExit, match="200"):
        installa.prova_server(nodi[0]["5060"]["indirizzo"], "segreto-5060")


def test_chat_sse_con_modello(proxy, nodi):
    r = proxy.post("/v1/chat/completions", json={"model": "3060/solo-b", "stream": True})
    assert r.content == b"data: primo\n\ndata: [DONE]\n\n"
    assert json.loads(nodi[1]["3060"][-1][2]) == {"model": "solo-b", "stream": True}


def test_barra_senza_backend_nel_modello(nodi):
    # Anche un id nativo senza il separatore ':' puo' avere una barra interna.
    nodi[3]["5060"]["risposta"] = {"data": [{"id": "qwen/modello"}]}
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        assert c.post("/5060/v1/chat/completions", json={"model": "qwen/modello"}).status_code == 200
        assert json.loads(nodi[1]["5060"][-1][2])["model"] == "qwen/modello"


def test_job_legacy_con_suffisso(proxy, nodi):
    assert proxy.get("/v1/chat/jobs/lavoro-1/result").status_code == 200
    assert nodi[1]["5060"][-1][1] == "v1/chat/jobs/lavoro-1/result"


def test_prefisso_servizio_sconosciuto(proxy):
    assert proxy.get("/ignoto/status/lavoro-1").status_code == 404


def test_catalogo_conserva_capabilities_con_id_qualificati(nodi):
    modelli = [
        {"id": "lmstudio:qwen3.6-35b-a3b", "capabilities": ["chat", "tools", "thinking", "vision"]},
        {"id": "solo-testo", "capabilities": ["chat"]},
        {"id": "vuoto", "capabilities": []},
    ]
    nodi[3]["5060"]["risposta"] = {"data": modelli}
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        for _ in range(2):  # Anche passando dalla cache del client.
            dati = c.get("/v1/models").json()["data"]
            for modello in modelli:
                qualificato = next(m for m in dati if m["id"] == "5060/" + modello["id"])
                assert qualificato["capabilities"] == modello["capabilities"]


@pytest.mark.parametrize('percorso,contenitore', [('/api/v0/models', 'data'), ('/api/v1/models', 'models'), ('/api/tags', 'models'), ('/api/ps', 'models'), ('/v1/models', 'data')])
def test_stato_reale_multiserver(nodi, percorso, contenitore):
    for nome in nodi[3]:
        nodi[3][nome]['risposta'] = {'data': [
            {'id': 'lmstudio:autore/modello', 'loaded': nome == '3060', 'context_length': 4096},
            {'id': 'freddo', 'loaded': False},
        ]}
    with TestClient(cliente.crea_app(remoti=nodi[0]), base_url="http://127.0.0.1") as c:
        for attivo in ('3060', '5060', None):
            for nome in nodi[3]:
                nodi[3][nome]['risposta']['data'][0]['loaded'] = nome == attivo
            r = c.get(percorso)
            assert r.status_code == 200
            modelli = r.json()[contenitore]
            if percorso == '/api/ps':
                assert [m['name'] for m in modelli] == ([f'{attivo}/lmstudio:autore/modello:latest'] if attivo else [])
            else:
                assert len(modelli) == 4
                for m in modelli:
                    identificatore = m.get('id') or m.get('key') or m['name']
                    caricato = attivo is not None and identificatore.startswith(attivo + '/') and 'autore/modello' in identificatore
                    if percorso == '/api/v0/models':
                        assert m['state'] == ('loaded' if caricato else 'not-loaded')
                    elif percorso == '/api/v1/models':
                        assert bool(m['loaded_instances']) == caricato
                    else:
                        assert m['loaded'] is caricato


def test_chat_ollama_instrada_modello_qualificato(proxy, nodi):
    r = proxy.post('/api/chat', json={'model': '3060/solo-b:latest', 'stream': False})
    assert r.status_code == 200
    assert nodi[1]['3060'][-1][1] == 'v1/chat/completions'
    assert json.loads(nodi[1]['3060'][-1][2])['model'] == 'solo-b'
    assert not any(r[1] == 'v1/chat/completions' for r in nodi[1]['5060'])


def test_load_non_dichiara_successo_per_modello_scarico(proxy, nodi):
    nodi[3]['3060']['risposta'] = {'data': [{'id': 'solo-b', 'loaded': False}]}
    assert proxy.post('/api/v1/models/load', json={'model': '3060/solo-b'}).status_code == 409
    nodi[3]['3060']['risposta']['data'][0]['loaded'] = True
    risposta = proxy.post('/api/v1/models/load', json={'model': '3060/solo-b'})
    assert risposta.status_code == 200
    assert risposta.json()['instance_id'] == '3060/solo-b'
    assert proxy.post('/api/v1/models/load', json={'model': 'ignoto/assente'}).status_code == 404


def test_api_compatibili_non_inventano_modelli_con_server_guasti(proxy, nodi):
    for controllo in nodi[3].values():
        controllo.update({'risposta': {}, 'stato': 503})
    for percorso in ('/api/v0/models', '/api/v1/models', '/api/tags', '/api/ps'):
        assert proxy.get(percorso).status_code == 502


def test_ollama_stream_instrada_sul_server_del_modello(proxy, nodi):
    r = proxy.post('/api/chat', json={'model': '3060/solo-b:latest', 'stream': True})
    assert r.status_code == 200
    assert json.loads(r.content)['done'] is True
    chiamate = [r for r in nodi[1]['3060'] if r[1] == 'v1/chat/completions']
    assert json.loads(chiamate[-1][2]) == {'model': 'solo-b', 'messages': [], 'stream': True}
    assert not any(r[1] == 'v1/chat/completions' for r in nodi[1]['5060'])
