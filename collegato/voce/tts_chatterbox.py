"""Chatterbox Multilingual via codec.cpp (ggml): una sola via CPU/AMD/NVIDIA."""
from __future__ import annotations

import io
import logging
import os
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from . import modelli

logger = logging.getLogger("sinergo.voce")

_model = None
_modello_caricato = ""
_cartella_caricata: Path | None = None
_clonazione = False
_log_backend = False
_lock = threading.Lock()


def _n_threads() -> int:
    return min(8, os.cpu_count() or 4)


def _carica(modello: str, cartella_dati: Path | None) -> Any:
    global _model, _modello_caricato, _cartella_caricata, _clonazione, _log_backend
    if _model is None or _modello_caricato != modello or _cartella_caricata != cartella_dati:
        from codec_cpp import TTS

        modelli.verifica(modello, cartella_dati)
        backbone = modelli.file_modello(modello, "modello", cartella_dati)
        codec = modelli.file_modello(modello, "codec", cartella_dati)
        n_threads = _n_threads()
        _model = TTS(backbone, codec, use_gpu=True, n_threads=n_threads)
        _clonazione = bool(getattr(getattr(_model, "audio_lm", None), "caps", None)
                           and _model.audio_lm.caps.takes_ref_audio)
        _modello_caricato = modello
        _cartella_caricata = cartella_dati
        if not _log_backend:
            _log_backend = True
            logger.info(
                "chatterbox codec_cpp caricato (use_gpu=True, ripiego CPU automatico, n_threads=%s, clonazione=%s)",
                n_threads, _clonazione,
            )
    return _model


def sintetizza(
    testo: str, *, modello: str, lingua: str = "it", voce: str | None = None,
    cartella_dati: Path | None = None,
) -> bytes:
    if not testo.strip():
        return b""
    lingua = (lingua or "it").lower()
    prompt = f"[{lingua}]{testo}"
    with _lock:
        motore = _carica(modello, cartella_dati)
        opts: dict[str, Any] = {}
        if _clonazione and voce and Path(voce).is_file():
            opts["ref_audio"] = voce
        pcm, rate = motore.synthesize(prompt, **opts)
    samples = np.asarray(pcm, dtype=np.float32).reshape(-1)
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()
