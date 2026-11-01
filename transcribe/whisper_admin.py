"""Operazioni admin Whisper: download modello, unload LLM da VRAM."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import config
from subprocess_utils import communicate_with_timeout

logger = logging.getLogger("ocr_server.whisper_admin")


class WhisperAdminError(Exception):
    """Errore download modello o unload LLM."""


async def unload_lmstudio_llms() -> dict[str, Any]:
    """Scarica tutti i modelli LM Studio dalla VRAM (best-effort)."""
    if not config.LMSTUDIO_ENABLED:
        return {"unloaded": [], "count": 0, "note": "lmstudio_disabled"}

    import lmstudio_client

    try:
        loaded = await lmstudio_client.get_loaded_models_context()
    except Exception as exc:  # noqa: BLE001
        logger.warning("whisper_unload_llms_list_failed | %s", exc)
        loaded = {}

    out: list[str] = []
    for name in list(loaded.keys()):
        await lmstudio_client.unload_model(name)
        out.append(name)

    logger.info("whisper_unload_llms | count=%d | models=%s", len(out), out)
    return {"unloaded": out, "count": len(out)}


async def download_model(
    model: str | None = None,
    *,
    device: str | None = None,
    compute_type: str | None = None,
) -> dict[str, Any]:
    """Prefetch modello Whisper in WHISPER_MODELS_DIR (subprocess venv dedicato)."""
    if not config.WHISPER_VENV_PYTHON.is_file():
        raise WhisperAdminError(
            f"venv Whisper assente: {config.WHISPER_VENV_PYTHON}. Esegui setup_whisper.ps1"
        )

    model_name = (model or config.WHISPER_MODEL).strip()
    dev = (device or config.WHISPER_DEVICE).strip().lower()
    ctype = (compute_type or config.WHISPER_COMPUTE_TYPE).strip()

    config.WHISPER_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    script = config.BASE_DIR / "scripts" / "prefetch_whisper_model.py"
    if not script.is_file():
        raise WhisperAdminError(f"Script prefetch mancante: {script}")

    cmd = [
        str(config.WHISPER_VENV_PYTHON),
        str(script),
        "--model",
        model_name,
        "--device",
        dev,
        "--compute-type",
        ctype,
        "--download-root",
        str(config.WHISPER_MODELS_DIR),
    ]

    logger.info(
        "whisper_download_start | model=%s | device=%s | compute=%s",
        model_name,
        dev,
        ctype,
    )

    from transcribe.whisper_env import whisper_subprocess_env

    sub_env = whisper_subprocess_env(device=dev, venv_python=config.WHISPER_VENV_PYTHON)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=sub_env,
    )
    try:
        stdout, stderr = await communicate_with_timeout(proc, config.WHISPER_DOWNLOAD_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise WhisperAdminError(
            f"Download timeout ({config.WHISPER_DOWNLOAD_TIMEOUT_S}s)"
        ) from exc

    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise WhisperAdminError(err or out or f"exit code {proc.returncode}")

    logger.info("whisper_download_done | model=%s | %s", model_name, out)
    return {
        "ok": True,
        "model": model_name,
        "device": dev,
        "compute_type": ctype,
        "models_dir": str(config.WHISPER_MODELS_DIR),
        "message": out or "download complete",
    }
