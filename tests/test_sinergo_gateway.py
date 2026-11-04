"""Gateway in modo Sinergo: riuso, host loopback, .env senza stampare la chiave."""

from __future__ import annotations

import httpx
import pytest
import uvicorn

import config
import installa
import server


class _Risposta:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_gateway_vivo_health_ok(monkeypatch):
    def get(url, **kw):
        assert url == "http://127.0.0.1:8088/health"
        return _Risposta(200)

    monkeypatch.setattr(httpx, "get", get)
    assert installa.gateway_vivo(8088) is True


def test_gateway_vivo_porta_chiusa(monkeypatch):
    def get(url, **kw):
        raise httpx.ConnectError("closed")

    monkeypatch.setattr(httpx, "get", get)
    assert installa.gateway_vivo(9) is False


def test_avvio_sinergo_con_gateway_vivo_non_lancia(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    lanci = []
    monkeypatch.setattr(installa, "gateway_vivo", lambda porta: True)
    monkeypatch.setattr(installa, "gateway_accetta_chiave", lambda porta, chiave: True)
    monkeypatch.setattr(installa, "lancia_gateway", lambda *a, **k: lanci.append((a, k)))
    monkeypatch.setattr(installa.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", "")
    installa.avvia_runtime_sinergo(tmp_path)
    assert lanci == []


def test_avvio_sinergo_con_gateway_spento_lancia_una_volta(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    lanci = []
    monkeypatch.setattr(installa, "gateway_vivo", lambda porta: False)
    monkeypatch.setattr(installa, "lancia_gateway", lambda *a, **k: lanci.append((a, k)))
    monkeypatch.setattr(installa.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", "")
    installa.avvia_runtime_sinergo(tmp_path)
    assert len(lanci) == 1


def test_gateway_vivo_models_401_nessun_popen_messaggio(tmp_path, monkeypatch):
    """Gateway acceso ma chiave diversa: niente Popen, messaggio chiaro senza chiave."""
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    installa.prepara_env_sinergo(tmp_path / ".env")
    chiave = installa.leggi_env(tmp_path / ".env")["API_KEY"]
    popen = []

    def get(url, **kw):
        if url.endswith("/health"):
            return _Risposta(200)
        if url.endswith("/v1/models"):
            assert kw.get("headers", {}).get("Authorization") == f"Bearer {chiave}"
            return _Risposta(401)
        raise AssertionError(url)

    class FintoPopen:
        def __init__(self, *a, **k):
            popen.append((a, k))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(installa.subprocess, "Popen", FintoPopen)
    monkeypatch.setattr(installa.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", "")
    with pytest.raises(SystemExit) as esc:
        installa.avvia_runtime_sinergo(tmp_path)
    testo = str(esc.value)
    assert "c'e' gia' un LocalRouter con un'altra chiave" in testo
    assert "SINERGO_GATEWAY_ENV=" in testo
    assert str(tmp_path / ".env") in testo or f"{tmp_path}\\.env" in testo
    assert chiave not in testo
    assert popen == []


def test_gateway_vivo_models_200_riuso_nessun_popen(tmp_path, monkeypatch):
    """Gateway acceso e chiave accettata: riuso, nessun Popen."""
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    installa.prepara_env_sinergo(tmp_path / ".env")
    popen = []

    def get(url, **kw):
        if url.endswith("/health"):
            return _Risposta(200)
        if url.endswith("/v1/models"):
            return _Risposta(200)
        raise AssertionError(url)

    class FintoPopen:
        def __init__(self, *a, **k):
            popen.append((a, k))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(installa.subprocess, "Popen", FintoPopen)
    monkeypatch.setattr(installa.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", "")
    installa.avvia_runtime_sinergo(tmp_path)
    assert popen == []


def test_sinergo_gateway_env_chiave_e_porta_al_collegato(tmp_path, monkeypatch):
    """SINERGO_GATEWAY_ENV: API_KEY e PORT dal file indicato, passati al collegato."""
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    gateway_env = tmp_path / "localrouter.env"
    gateway_env.write_text(
        "API_KEY=chiave-del-gateway-acceso-xx\nPORT=9099\n",
        encoding="utf-8",
    )
    installa.prepara_env_sinergo(tmp_path / ".env")
    visto = {}

    def get(url, **kw):
        if url == "http://127.0.0.1:9099/health":
            return _Risposta(200)
        if url == "http://127.0.0.1:9099/v1/models":
            assert kw["headers"]["Authorization"] == "Bearer chiave-del-gateway-acceso-xx"
            return _Risposta(200)
        raise AssertionError(url)

    def run(argv, **kw):
        visto["env"] = kw.get("env") or {}
        visto["argv"] = argv

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(installa.subprocess, "run", run)
    monkeypatch.setattr(
        installa, "lancia_gateway", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch"))
    )
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", str(gateway_env))
    installa.avvia_runtime_sinergo(tmp_path)
    assert visto["env"]["API_KEY"] == "chiave-del-gateway-acceso-xx"
    assert visto["env"]["PORT"] == "9099"


def test_sinergo_gateway_env_file_assente(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    assente = tmp_path / "non_esiste.env"
    monkeypatch.setattr(config, "SINERGO_GATEWAY_ENV", str(assente))
    monkeypatch.setattr(installa.subprocess, "run", lambda *a, **k: None)
    with pytest.raises(SystemExit) as esc:
        installa.avvia_runtime_sinergo(tmp_path)
    assert "SINERGO_GATEWAY_ENV" in str(esc.value)
    assert "assente" in str(esc.value).lower() or "non esiste" in str(esc.value).lower() or "manca" in str(esc.value).lower()


def test_sinergo_uvicorn_host_loopback(monkeypatch):
    visti = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: visti.update(kw))
    monkeypatch.setattr(config, "MODO", "sinergo")
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "SINERGO_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "PORT", 8088)
    server.main()
    assert visti["host"] == "127.0.0.1"


def test_lancia_gateway_senza_cloudflared(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    chiamate = []

    class FintoPopen:
        def __init__(self, argv, **kw):
            chiamate.append(argv)

    monkeypatch.setattr(installa.subprocess, "Popen", FintoPopen)
    installa.lancia_gateway(tmp_path, python)
    assert chiamate, "gateway non lanciato"
    assert all("cloudflared" not in str(a).lower() for a in chiamate)
    assert any("server.py" in str(parte) for argv in chiamate for parte in argv)


def test_env_sinergo_chiave_stabile(tmp_path, capsys):
    env = tmp_path / ".env"
    installa.prepara_env_sinergo(env)
    primo = installa.leggi_env(env)
    assert primo.get("API_KEY")
    assert len(primo["API_KEY"]) >= 16
    assert primo.get("LOCALROUTER_MODO") == "sinergo"
    assert primo.get("HOST") == "127.0.0.1" or primo.get("SINERGO_BIND_HOST") == "127.0.0.1"
    out = capsys.readouterr().out
    assert primo["API_KEY"] not in out
    installa.prepara_env_sinergo(env)
    secondo = installa.leggi_env(env)
    assert secondo["API_KEY"] == primo["API_KEY"]


def test_installa_sinergo_prepara_env_e_runtime(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    fatti = []
    monkeypatch.setattr(installa.subprocess, "run", lambda argv, **kw: fatti.append(("run", argv)))
    monkeypatch.setattr(installa, "avvio_automatico_sinergo", lambda c: fatti.append(("autostart", c)))
    monkeypatch.setattr(installa, "avvia_runtime_sinergo", lambda c: fatti.append(("runtime", c)))
    installa.installa_sinergo(tmp_path)
    assert ("run", ["uv", "sync", "--extra", "voce", "--extra", "chatterbox"]) in fatti
    assert ("autostart", tmp_path) in fatti
    assert ("runtime", tmp_path) in fatti
    letto = installa.leggi_env(tmp_path / ".env")
    assert letto.get("API_KEY")
    assert letto.get("LOCALROUTER_MODO") == "sinergo"
