"""Chatterbox Multilingual (ResembleAI) su CPU o CUDA, pesi dall'elenco fisso."""
from __future__ import annotations

import io
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from . import modelli

_model = None
_modello_caricato = ""
_cartella_caricata: Path | None = None
_lock = threading.Lock()


def _dispositivo() -> str:
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def _carica(modello: str, cartella_dati: Path | None) -> Any:
    global _model, _modello_caricato, _cartella_caricata
    if _model is None or _modello_caricato != modello or _cartella_caricata != cartella_dati:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        cartella = modelli.verifica(modello, cartella_dati)
        _model = ChatterboxMultilingualTTS.from_local(cartella, _dispositivo())
        _modello_caricato = modello
        _cartella_caricata = cartella_dati
    return _model


def sintetizza(
    testo: str, *, modello: str, lingua: str = "it", voce: str | None = None,
    cartella_dati: Path | None = None,
) -> bytes:
    if not testo.strip():
        return b""
    prompt = voce if voce and Path(voce).is_file() else None
    with _lock:
        motore = _carica(modello, cartella_dati)
        wav = motore.generate(
            testo, language_id=(lingua or "it").lower(),
            audio_prompt_path=prompt,
        )
        rate = int(motore.sr)
    samples = np.asarray(wav, dtype=np.float32).reshape(-1)
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()
