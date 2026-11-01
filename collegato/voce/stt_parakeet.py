"""Trascrizione ONNX su CPU (Parakeet), pesi verificati nella cache Sinergo."""
from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import numpy as np

from . import modelli

_model = None
_modello_caricato = ""
_cartella_caricata: Path | None = None
_lock = threading.Lock()


def _carica(modello: str, cartella_dati: Path | None) -> Any:
    global _model, _modello_caricato, _cartella_caricata
    if _model is None or _modello_caricato != modello or _cartella_caricata != cartella_dati:
        import onnx_asr
        import onnxruntime as ort

        path = modelli.verifica(modello, cartella_dati)
        opzioni = ort.SessionOptions()
        opzioni.intra_op_num_threads = modelli.CPU_THREADS
        opzioni.inter_op_num_threads = 1
        _model = onnx_asr.load_model(
            modello, path, quantization="int8",
            sess_options=opzioni, providers=["CPUExecutionProvider"],
        )
        _modello_caricato = modello
        _cartella_caricata = cartella_dati
    return _model


def transcribe(audio: bytes, modello: str, *, cartella_dati: Path | None = None, lingua: str = "") -> str:
    """Accetta WAV e contenitori del browser tramite PyAV, senza ffmpeg di sistema."""
    del lingua  # decoder multilingue con lingua automatica
    import av

    with av.open(io.BytesIO(audio)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        parti = [f.to_ndarray().reshape(-1) for frame in container.decode(audio=0)
                 for f in resampler.resample(frame)]
        parti.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    if not parti:
        return ""
    campioni = np.concatenate(parti).astype(np.float32) / 32768
    with _lock:
        model = _carica(modello, cartella_dati)
        return " ".join(
            model.recognize(campioni[i:i + 320000], sample_rate=16000)
            for i in range(0, len(campioni), 320000)
        ).strip()
