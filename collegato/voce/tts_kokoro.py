"""Voce italiana Kokoro ONNX su CPU (fp32), fonemizzatore incluso nelle wheel."""
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

VOCE_PREDEFINITA = "im_nicola"


def _carica(modello: str, cartella_dati: Path | None) -> Any:
    global _model, _modello_caricato, _cartella_caricata
    if _model is None or _modello_caricato != modello or _cartella_caricata != cartella_dati:
        from kokoro_onnx import Kokoro
        import onnxruntime as ort

        modelli.verifica(modello, cartella_dati)
        opzioni = ort.SessionOptions()
        opzioni.intra_op_num_threads = modelli.CPU_THREADS
        opzioni.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(modelli.file_modello(modello, "modello", cartella_dati)),
            sess_options=opzioni, providers=["CPUExecutionProvider"],
        )
        _model = Kokoro.from_session(session, str(modelli.file_modello(modello, "voci", cartella_dati)))
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
        samples, rate = _carica(modello, cartella_dati).create(
            testo, voice=nome_voce, speed=1.0, lang=lingua or "it",
        )
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()
