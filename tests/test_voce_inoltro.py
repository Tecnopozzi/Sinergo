"""Inoltro /v1/audio/* a speaches quando VOCE_URL e' impostato."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import server

CHIAVE = "chiave-di-prova"
AUDIO_FAKE = b"RIFF....WAVEfmt "  # byte audio finti, devono arrivare intatti
WAV_FAKE = b"RIFF....fake-wav-bytes"


@pytest.fixture
def locale(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "WHISPER_ENABLED", False)
    monkeypatch.setattr(config, "VOCE_URL", "")
    return TestClient(server.app, client=("127.0.0.1", 50000))


def _client_con_finto(monkeypatch, handler):
    """Sostituisce il client verso speaches con MockTransport."""
    transport = httpx.MockTransport(handler)

    def fabbrica() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(180.0))

    monkeypatch.setattr(server, "_voce_http_client", fabbrica)


def test_senza_voce_url_transcriptions_resta_404_whisper(locale, monkeypatch):
    """VOCE_URL vuota + WHISPER off = comportamento storico (404)."""
    monkeypatch.setattr(config, "VOCE_URL", "")
    monkeypatch.setattr(config, "WHISPER_ENABLED", False)
    r = locale.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        files={"file": ("a.wav", AUDIO_FAKE, "audio/wav")},
        data={"model": "x"},
    )
    assert r.status_code == 404
    assert "WHISPER" in r.json()["detail"]


def test_senza_voce_url_speech_404(locale, monkeypatch):
    monkeypatch.setattr(config, "VOCE_URL", "")
    r = locale.post(
        "/v1/audio/speech",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={"input": "ciao"},
    )
    assert r.status_code == 404


def test_transcriptions_senza_chiave_401(locale, monkeypatch):
    monkeypatch.setattr(config, "VOCE_URL", "http://voce.test")
    r = locale.post(
        "/v1/audio/transcriptions",
        files={"file": ("a.wav", AUDIO_FAKE, "audio/wav")},
    )
    assert r.status_code == 401


def test_speech_senza_chiave_401(locale, monkeypatch):
    monkeypatch.setattr(config, "VOCE_URL", "http://voce.test")
    r = locale.post("/v1/audio/speech", json={"input": "ciao"})
    assert r.status_code == 401


def test_transcriptions_inoltra_file_intatto(locale, monkeypatch):
    visti: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        visti["url"] = str(request.url)
        body = request.read()
        visti["body"] = body
        assert AUDIO_FAKE in body
        assert b"Systran/faster-whisper-small" in body or b"name=\"model\"" in body
        return httpx.Response(200, json={"text": "ciao mondo"})

    monkeypatch.setattr(config, "VOCE_URL", "http://voce.test")
    _client_con_finto(monkeypatch, handler)

    r = locale.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        files={"file": ("prova.wav", AUDIO_FAKE, "audio/wav")},
        data={"language": "it"},
    )
    assert r.status_code == 200
    assert r.json() == {"text": "ciao mondo"}
    assert visti["url"] == "http://voce.test/v1/audio/transcriptions"
    assert AUDIO_FAKE in visti["body"]


def test_speech_inoltra_e_restituisce_byte_e_content_type(locale, monkeypatch):
    visti: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        visti["url"] = str(request.url)
        visti["payload"] = _json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=WAV_FAKE,
            headers={"content-type": "audio/wav"},
        )

    monkeypatch.setattr(config, "VOCE_URL", "http://voce.test")
    _client_con_finto(monkeypatch, handler)

    r = locale.post(
        "/v1/audio/speech",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        json={"input": "buongiorno"},
    )
    assert r.status_code == 200
    assert r.content == WAV_FAKE
    assert r.headers["content-type"].startswith("audio/wav")
    assert visti["url"] == "http://voce.test/v1/audio/speech"
    assert visti["payload"]["input"] == "buongiorno"
    assert visti["payload"]["model"] == config.VOCE_TTS_MODEL_DEFAULT
    assert visti["payload"]["voice"] == config.VOCE_TTS_VOICE_DEFAULT
    assert visti["payload"]["response_format"] == config.VOCE_TTS_FORMAT_DEFAULT


def test_servizio_voce_giu_502(locale, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(config, "VOCE_URL", "http://voce.test")
    _client_con_finto(monkeypatch, handler)

    r = locale.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {CHIAVE}"},
        files={"file": ("a.wav", AUDIO_FAKE, "audio/wav")},
    )
    assert r.status_code == 502
    assert "voce" in r.json()["detail"].lower()
