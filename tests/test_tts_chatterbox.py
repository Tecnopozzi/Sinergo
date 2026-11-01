"""Unit test di tts_chatterbox su codec_cpp finto."""
from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest


def _wav_silenzio(secondi: float = 0.1, rate: int = 24000) -> bytes:
    n = int(secondi * rate)
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(np.zeros(n, dtype="<i2").tobytes())
    return out.getvalue()


@pytest.fixture
def chatterbox(monkeypatch, tmp_path):
    import collegato.voce.tts_chatterbox as tts

    tts._model = None
    tts._modello_caricato = ""
    tts._cartella_caricata = None
    tts._clonazione = False
    tts._log_backend = False

    chiamate = {"synthesize": [], "tts_init": 0}

    class FakeTTS:
        def __init__(self, backbone, codec, *, use_gpu=False, n_threads=0):
            chiamate["tts_init"] += 1
            self.backbone = backbone
            self.codec = codec
            self.use_gpu = use_gpu
            self.n_threads = n_threads
            self.audio_lm = SimpleNamespace(caps=SimpleNamespace(takes_ref_audio=False))

        def synthesize(self, text, **opts):
            chiamate["synthesize"].append((text, opts))
            sr = 24000
            return np.zeros(sr // 10, dtype=np.float32), sr

    fake_mod = SimpleNamespace(TTS=FakeTTS)
    monkeypatch.setitem(__import__("sys").modules, "codec_cpp", fake_mod)
    monkeypatch.setattr(tts.modelli, "verifica", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        tts.modelli, "file_modello",
        lambda modello, ruolo, cartella_dati=None: tmp_path / (
            "chatterbox-mtl-q4_k_m.gguf" if ruolo == "modello" else "codec-q4_k_m.gguf"
        ),
    )
    return tts, chiamate


def test_prefisso_lingua_nel_testo(chatterbox):
    tts, chiamate = chatterbox
    audio = tts.sintetizza("Ciao mondo", modello="chatterbox-mtl", lingua="it")
    assert audio
    assert chiamate["synthesize"] == [("[it]Ciao mondo", {})]


def test_prefisso_lingua_en(chatterbox):
    tts, chiamate = chatterbox
    tts.sintetizza("Hello", modello="chatterbox-mtl", lingua="en")
    assert chiamate["synthesize"][0][0] == "[en]Hello"


def test_caricamento_unico(chatterbox):
    tts, chiamate = chatterbox
    tts.sintetizza("Uno", modello="chatterbox-mtl")
    tts.sintetizza("Due", modello="chatterbox-mtl")
    assert chiamate["tts_init"] == 1
    assert len(chiamate["synthesize"]) == 2


def test_voce_ignorata_se_no_clonazione(chatterbox, tmp_path):
    tts, chiamate = chatterbox
    prompt = tmp_path / "ref.wav"
    prompt.write_bytes(_wav_silenzio())
    tts.sintetizza("Ciao", modello="chatterbox-mtl", voce=str(prompt))
    assert chiamate["synthesize"][0][1] == {}


def test_voce_come_ref_audio_se_clonazione(chatterbox, tmp_path):
    tts, chiamate = chatterbox
    prompt = tmp_path / "ref.wav"
    prompt.write_bytes(_wav_silenzio())
    tts.sintetizza("Ciao", modello="chatterbox-mtl")  # carica con caps False
    tts._clonazione = True
    tts.sintetizza("Ciao", modello="chatterbox-mtl", voce=str(prompt))
    assert chiamate["synthesize"][-1][1] == {"ref_audio": str(prompt)}


def test_use_gpu_e_n_threads(chatterbox, monkeypatch):
    tts, chiamate = chatterbox
    monkeypatch.setattr(tts.os, "cpu_count", lambda: 16)
    tts.sintetizza("Ciao", modello="chatterbox-mtl")
    assert chiamate["tts_init"] == 1
    assert tts._model.use_gpu is True
    assert tts._model.n_threads == 8


def test_misura_velocita_warmup_non_misurato(monkeypatch, tmp_path):
    import collegato.voce.ruoli as voce

    chiamate = []

    def fake_sint(motore, testo, **kwargs):
        chiamate.append(testo)
        return _wav_silenzio(0.2)

    monkeypatch.setattr(voce, "_sintetizza", fake_sint)
    out = voce.misura_velocita("kokoro", tmp_path)
    assert chiamate[0] == "Ciao"  # warm-up
    assert chiamate[1:] == list(voce.FRASI_PROVA)
    assert "rtf" in out and "primo_audio_s" in out
