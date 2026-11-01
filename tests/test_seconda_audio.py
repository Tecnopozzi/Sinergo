"""La saturazione Whisper deve produrre 503 su tutte le rotte locali."""

import pytest
from fastapi.testclient import TestClient

import config
import server
from transcribe import whisper_pipeline


@pytest.mark.parametrize("rotta", ["/v1/audio/transcriptions", "/v1/audio/translations", "/audio/transcribe"])
def test_whisper_saturo_restituisce_503(monkeypatch, rotta):
    monkeypatch.setattr(config, "API_KEY", "chiave-sintetica-audio-lunga")
    monkeypatch.setattr(config, "WHISPER_ENABLED", True)
    monkeypatch.setattr(config, "VOCE_URL", "")

    async def saturo(*args):
        raise whisper_pipeline.WhisperBusyError("coda Whisper piena")

    monkeypatch.setattr(whisper_pipeline, "run_transcription", saturo)
    risposta = TestClient(server.app).post(
        rotta, headers={"Authorization": "Bearer chiave-sintetica-audio-lunga"},
        files={"file": ("prova.wav", b"audio sintetico", "audio/wav")},
    )
    assert risposta.status_code == 503
