"""Guardie di sicurezza: ogni test fallisce se si toglie la guardia corrispondente."""

import os
import stat
import sys

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import auth
import config
import installa
import server

CHIAVE = "chiave-di-prova-lunga-abbastanza"
H = {"Authorization": f"Bearer {CHIAVE}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    auth._fallimenti.clear()
    return TestClient(server.app)


def test_chiave_sbagliata_ripetuta_diventa_429_ma_la_giusta_passa(client):
    for _ in range(auth.FALLIMENTI_PER_MINUTO):
        assert client.get("/metrics/system", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/metrics/system", headers={"Authorization": "Bearer x"}).status_code == 429
    assert client.get("/metrics/system", headers=H).status_code == 200


def test_health_senza_chiave_non_espone_host_interni(client, monkeypatch):
    async def nessun_modello():
        return []

    monkeypatch.setattr(server, "list_tags", nessun_modello)
    monkeypatch.setattr(config, "LMSTUDIO_ENABLED", False)
    anonimo = client.get("/health").json()
    assert set(anonimo) == {"status"}
    assert "lmstudio_host" in client.get("/health", headers=H).json()


@pytest.mark.parametrize("url", ["http://169.254.169.254/x", "https://evil.example/a.png"])
@pytest.mark.parametrize("rotta", ["/v1/chat/completions", "/v1/chat/jobs"])
def test_url_remoti_nei_media_rifiutati(client, rotta, url):
    corpo = {"model": "lmstudio:m", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    assert client.post(rotta, json=corpo, headers=H).status_code == 400


def test_download_whisper_rifiuta_repo_e_percorsi(client, monkeypatch):
    import transcribe.whisper_admin as wa

    async def scarica(*a, **k):
        return {"ok": True}

    monkeypatch.setattr(config, "WHISPER_ENABLED", True)
    monkeypatch.setattr(wa, "download_model", scarica)
    assert client.post("/whisper/download-model", params={"model": "large-v3"}, headers=H).status_code == 200
    r = client.post("/whisper/download-model", params={"model": "evil/repo"}, headers=H)
    assert r.status_code == 400
    r = client.post("/whisper/download-model", params={"model": "../../etc"}, headers=H)
    assert r.status_code == 400


def test_corpo_troppo_grande_413():
    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(server.LimiteCorpo, massimo=10)
    c = TestClient(app)
    assert c.post("/x", content=b"a" * 10).status_code == 200
    assert c.post("/x", content=b"a" * 11).status_code == 413


def test_corpo_senza_content_length_fermato_nel_flusso():
    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(server.LimiteCorpo, massimo=10)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/x", content=iter([b"a" * 6, b"a" * 6]))
    assert r.status_code != 200


@pytest.mark.skipif(sys.platform == "win32", reason="permessi POSIX")
def test_env_scritto_solo_per_il_proprietario(tmp_path):
    env = tmp_path / ".env"
    installa.scrivi_env(env, {"API_KEY": "x" * 32})
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_chiave_corta_rifiutata_all_avvio(monkeypatch):
    import importlib

    monkeypatch.setenv("API_KEY", "corta")
    with pytest.raises(RuntimeError):
        importlib.reload(config)
    monkeypatch.delenv("API_KEY")
    importlib.reload(config)


# --- attacco locale 31/10 -------------------------------------------------

def test_chiave_non_ascii_da_401_non_500(client):
    for intestazione in ({"X-API-Key": "café".encode("latin-1")}, {"Authorization": "Bearer café".encode("latin-1")}):
        assert client.get("/v1/models", headers=intestazione).status_code == 401


def test_chiave_non_ascii_sul_token_pull_restart(monkeypatch):
    monkeypatch.setattr(auth, "PULL_RESTART_HTTP_KEY", "token-pull-restart-lungo")
    richiesta = Request({"type": "http", "headers": [(b"x-pull-restart-token", "café".encode("latin-1"))], "client": ("127.0.0.1", 1)})
    with pytest.raises(Exception) as exc:
        auth.require_pull_restart_token(richiesta)
    assert getattr(exc.value, "status_code", None) == 401


def test_query_con_la_chiave_non_finisce_nel_registro(client):
    richiesta = Request({"type": "http", "headers": [], "query_string": b"api_key=segretissima&n=3&key=altra", "method": "GET", "path": "/x", "client": ("127.0.0.1", 1)})
    server._audit_init(richiesta)
    assert richiesta.state.audit_entry["query"] == {"api_key": "***", "n": "3", "key": "***"}


def test_dashboard_non_esegue_html_preso_dal_registro(tmp_path):
    """Un anonimo scrive User-Agent/X-Forwarded-For/modello nel registro: la pagina non deve renderli HTML."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node assente")
    script = server._DASHBOARD_HTML.rsplit("<script>", 1)[1].split("</script>")[0]
    veleno = "<img src=x onerror=alert(1)>"
    dati = {"server_name": "s", "generated_at": "2026-01-01T00:00:00Z", "audit_enabled": True, "gpu": None, "series": {},
            "hosts": [{"name": veleno, "base_url": veleno, "error": veleno, "reachable": True, "models": [{"id": veleno}]}],
            "recent": [{"client_name": veleno, "client_ip": veleno, "model": veleno, "backend": veleno, "path": veleno, "status_code": veleno}]}
    harness = tmp_path / "h.js"
    harness.write_text(
        "const out = {};\n"
        "const el = (id) => new Proxy({ classList: { remove() {}, add() {} }, addEventListener() {}, getContext() {} }, {\n"
        "  set(t, k, v) { if (k === 'innerHTML') out[id] = v; t[k] = v; return true; } });\n"
        "const els = {};\n"
        "global.document = { getElementById: (id) => (els[id] ||= el(id)), querySelectorAll: () => [], title: '' };\n"
        "global.location = { search: '' };\n"
        "global.localStorage = { getItem: () => 'k', setItem() {} };\n"
        "global.Chart = function () { this.data = {}; this.update = () => {}; };\n"
        f"global.fetch = async () => ({{ status: 200, ok: true, json: async () => ({json.dumps(dati)}) }});\n"
        "global.setInterval = () => 0;\n"
        f"{script}\n"
        "setTimeout(() => { console.log(JSON.stringify(out)); }, 50);\n",
        encoding="utf-8",
    )
    risultato = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30)
    assert risultato.returncode == 0, risultato.stderr
    html = "".join(json.loads(risultato.stdout).values())
    assert "recentBody" in risultato.stdout and "<img" not in html


def test_client_rifiuta_host_e_origin_non_locali(monkeypatch):
    import cliente
    import httpx

    viste = []

    async def connessione_rifiutata(self, richiesta):
        viste.append(richiesta.url.path)
        raise httpx.ConnectError("Connessione rifiutata simulata", request=richiesta)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", connessione_rifiutata)

    c = TestClient(cliente.crea_app("http://127.0.0.1:9", "chiave-remota"), base_url="http://127.0.0.1")
    assert c.get("/api/version", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/api/version", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.options("/v1/models", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"}).status_code == 403
    assert not viste
    assert c.get("/v1/models").status_code == 502
    assert viste == ["/health"]
