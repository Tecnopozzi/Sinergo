"""Pipeline trascrizione: salva l'audio e delega al worker subprocess (.venv-whisper).

Sincrona dal punto di vista del client (attende il risultato), ma serializzata
con un semaphore per non lanciare piu' inferenze GPU/CPU contemporaneamente.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
from subprocess_utils import communicate_with_timeout

logger = logging.getLogger("ocr_server.whisper_pipeline")

# Estensioni/mime audio-video comuni accettati (faster-whisper via PyAV decodifica molto)
ALLOWED_AUDIO_EXT = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wma", ".webm", ".mp4", ".mkv", ".mov", ".avi", ".3gp",
}

_semaphore: asyncio.Semaphore | None = None
_admitted_jobs = 0


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.WHISPER_MAX_CONCURRENT_JOBS)
    return _semaphore


class WhisperError(Exception):
    """Errore configurazione o inferenza Whisper."""


class WhisperBusyError(WhisperError):
    """Coda di trascrizione piena."""


@dataclass(frozen=True, slots=True)
class WhisperOptions:
    language: str | None = None
    task: str = "transcribe"
    beam_size: int = 5
    vad_filter: bool = True
    initial_prompt: str | None = None
    want_segments: bool = True


@dataclass(slots=True)
class WhisperResult:
    text: str
    language: str | None
    duration: float
    segments: list[dict[str, Any]] = field(default_factory=list)
    language_probability: float = 0.0
    load_s: float = 0.0
    transcribe_s: float = 0.0


def _ext_ok(filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix in ALLOWED_AUDIO_EXT if suffix else True


async def run_transcription(
    raw_bytes: bytes,
    filename: str,
    options: WhisperOptions,
) -> WhisperResult:
    global _admitted_jobs
    # Check e prenotazione non contengono await: atomici nell'event loop.
    if _admitted_jobs >= config.WHISPER_MAX_CONCURRENT_JOBS + 4:
        raise WhisperBusyError("coda Whisper piena: riprovare piu' tardi")
    _admitted_jobs += 1
    try:
        return await _run_transcription(raw_bytes, filename, options)
    finally:
        _admitted_jobs -= 1


async def _run_transcription(
    raw_bytes: bytes,
    filename: str,
    options: WhisperOptions,
) -> WhisperResult:
    if not config.WHISPER_ENABLED:
        raise WhisperError("Modulo Whisper disabilitato (WHISPER_ENABLED=0)")

    if not config.WHISPER_VENV_PYTHON.is_file():
        raise WhisperError(
            f"venv Whisper assente: {config.WHISPER_VENV_PYTHON}. "
            "Esegui scripts/setup_whisper.ps1"
        )

    if not raw_bytes:
        raise WhisperError("File audio vuoto")

    if len(raw_bytes) > config.WHISPER_MAX_INPUT_BYTES:
        raise WhisperError(
            f"File troppo grande (max {config.WHISPER_MAX_INPUT_BYTES} bytes)"
        )

    if filename and not _ext_ok(filename):
        logger.warning("whisper_unknown_ext | file=%s (provo comunque)", filename)

    job_id = uuid.uuid4().hex
    config.WHISPER_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = config.WHISPER_TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        return await _transcribe_in_dir(raw_bytes, filename, options, job_dir)
    finally:
        _cleanup(job_dir)


async def _transcribe_in_dir(
    raw_bytes: bytes,
    filename: str,
    options: WhisperOptions,
    job_dir: Path,
) -> WhisperResult:
    job_id = job_dir.name
    suffix = Path(filename or "audio").suffix.lower() or ".bin"
    input_path = job_dir / f"input{suffix}"
    input_path.write_bytes(raw_bytes)

    payload = {
        "input": str(input_path),
        "model": config.WHISPER_MODEL,
        "device": config.WHISPER_DEVICE,
        "compute_type": config.WHISPER_COMPUTE_TYPE,
        "download_root": str(config.WHISPER_MODELS_DIR),
        "language": options.language,
        "task": options.task,
        "beam_size": options.beam_size,
        "vad_filter": options.vad_filter,
        "initial_prompt": options.initial_prompt,
        "want_segments": options.want_segments,
    }

    config_path = job_dir / "cfg.json"
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    worker_script = config.BASE_DIR / "transcribe" / "whisper_worker.py"
    cmd = [str(config.WHISPER_VENV_PYTHON), str(worker_script), "--config", str(config_path)]

    logger.info(
        "whisper_infer_start | job_id=%s | model=%s | device=%s | file=%s | bytes=%d",
        job_id, config.WHISPER_MODEL, config.WHISPER_DEVICE, filename, len(raw_bytes),
    )

    if config.WHISPER_DEVICE == "cuda" and config.WHISPER_AUTO_UNLOAD_LLMS:
        from transcribe.whisper_admin import unload_lmstudio_llms

        unload_res = await unload_lmstudio_llms()
        logger.info(
            "whisper_auto_unload_llms | count=%d",
            unload_res.get("count", 0),
        )

    from transcribe.whisper_env import whisper_subprocess_env

    sub_env = whisper_subprocess_env(
        device=config.WHISPER_DEVICE,
        venv_python=config.WHISPER_VENV_PYTHON,
    )

    t0 = time.time()
    async with _get_semaphore():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
        )
        try:
            stdout, stderr = await communicate_with_timeout(proc, config.WHISPER_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise WhisperError(
                f"Trascrizione timeout ({config.WHISPER_TIMEOUT_S}s)"
            ) from exc

    if proc.returncode != 0:
        err_text = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
        raise WhisperError(err_text or f"worker exit code {proc.returncode}")

    meta: dict[str, Any] = {}
    for line in reversed((stdout or b"").decode("utf-8", errors="replace").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                meta = json.loads(line)
                break
            except (json.JSONDecodeError, ValueError):
                continue

    if not meta:
        raise WhisperError("Worker completato ma nessun risultato JSON")

    logger.info(
        "whisper_infer_done | job_id=%s | lang=%s | dur=%.1fs | elapsed=%.1fs",
        job_id, meta.get("language"), meta.get("duration") or 0.0, time.time() - t0,
    )

    return WhisperResult(
        text=str(meta.get("text") or ""),
        language=meta.get("language"),
        duration=float(meta.get("duration") or 0.0),
        segments=list(meta.get("segments") or []),
        language_probability=float(meta.get("language_probability") or 0.0),
        load_s=float(meta.get("load_s") or 0.0),
        transcribe_s=float(meta.get("transcribe_s") or 0.0),
    )


def _cleanup(job_dir: Path) -> None:
    import shutil

    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except OSError:
        pass
