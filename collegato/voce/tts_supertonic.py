"""Supertonic 3 su CPU: solo pesi verificati dal manifesto, nessun download del SDK."""
from __future__ import annotations

import io
import threading
import wave
from pathlib import Path

import numpy as np

from . import modelli

_model = None
_modello_caricato = ""
_cartella_caricata: Path | None = None
_lock = threading.Lock()

VOCE_PREDEFINITA = "M1"


def _carica(modello: str, cartella_dati: Path | None):
    global _model, _modello_caricato, _cartella_caricata
    if _model is None or _modello_caricato != modello or _cartella_caricata != cartella_dati:
        from supertonic import TTS

        modelli.verifica(modello, cartella_dati)
        _model = TTS(
            model_dir=modelli.percorso(modello, ".", cartella_dati),
            auto_download=False,
            intra_op_num_threads=modelli.CPU_THREADS,
            inter_op_num_threads=1,
        )
        _modello_caricato = modello
        _cartella_caricata = cartella_dati
    return _model


def sintetizza(
    testo: str, *, modello: str, lingua: str = "it", voce: str | None = None,
    cartella_dati: Path | None = None,
) -> bytes:
    if not testo.strip():
        return b""
    nome_voce = voce or VOCE_PREDEFINITA
    with _lock:
        tts = _carica(modello, cartella_dati)
        samples, _ = tts.synthesize(
            testo, voice_style=tts.get_voice_style(voice_name=nome_voce),
            lang=lingua or "it", total_steps=5,
        )
        rate = tts.sample_rate
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()
