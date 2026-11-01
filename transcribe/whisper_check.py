"""Verifica runtime Whisper (venv dedicato, worker, modello) per /health.

Il check e' leggero (nessun import di faster-whisper nel processo principale):
verifica solo l'esistenza del venv, del worker e della cache modello locale.
"""

from __future__ import annotations

from typing import Any

import config


def _model_cached() -> bool:
    """True se il modello risulta gia' scaricato in WHISPER_MODELS_DIR.

    faster-whisper salva i modelli come cartelle models--Systran--faster-whisper-*
    dentro download_root. Se WHISPER_MODEL e' un path locale, verifica il path.
    """
    root = config.WHISPER_MODELS_DIR
    if not root.is_dir():
        return False
    model = config.WHISPER_MODEL.strip()
    # Path locale a un modello convertito
    from pathlib import Path

    p = Path(model)
    if p.is_absolute() and p.exists():
        return True
    try:
        for child in root.iterdir():
            if child.is_dir() and "faster-whisper" in child.name.lower():
                return True
    except OSError:
        return False
    return False


def check_whisper_runtime() -> dict[str, Any]:
    venv_ok = config.WHISPER_VENV_PYTHON.is_file()
    worker_ok = (config.BASE_DIR / "transcribe" / "whisper_worker.py").is_file()
    model_cached = _model_cached()

    # Il modello puo' comunque essere scaricato al primo uso: model_cached e'
    # informativo, non blocca runtime_ok (serve solo venv + worker).
    runtime_ok = venv_ok and worker_ok

    return {
        "whisper_enabled": config.WHISPER_ENABLED,
        "whisper_ok": runtime_ok if config.WHISPER_ENABLED else False,
        "whisper_engine": "faster-whisper",
        "whisper_venv_python": str(config.WHISPER_VENV_PYTHON),
        "whisper_venv_ok": venv_ok,
        "whisper_worker_ok": worker_ok,
        "whisper_model": config.WHISPER_MODEL,
        "whisper_model_cached": model_cached,
        "whisper_device": config.WHISPER_DEVICE,
        "whisper_compute_type": config.WHISPER_COMPUTE_TYPE,
        "whisper_models_dir": str(config.WHISPER_MODELS_DIR),
    }
