"""
Pipeline Media SUPIR: normalizza input e delega inferenza al worker subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config
from image_utils import coerce_to_png, is_image_mime
from media.models_check import check_media_runtime
from subprocess_utils import communicate_with_timeout

logger = logging.getLogger("ocr_server.media_pipeline")

ALLOWED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}


class MediaCancelled(Exception):
    """Job media annullato dall'utente."""


class MediaPipelineError(Exception):
    """Errore configurazione o inferenza media."""


@dataclass(frozen=True, slots=True)
class MediaOptions:
    mode: str
    upscale: int
    fidelity: float
    face_only: bool


@dataclass(frozen=True, slots=True)
class MediaResult:
    output_path: Path
    width: int
    height: int
    elapsed_s: float = 0.0
    vram_peak_mb: float = 0.0


def validate_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    allowed = {"restore", "upscale", "restore+upscale", "enhance", "supir"}
    if normalized not in allowed:
        raise MediaPipelineError(
            f"mode non valido: {mode!r}. Valori: {sorted(allowed)}"
        )
    return normalized


def validate_upscale(upscale: int) -> int:
    value = int(upscale)
    if value not in (2, 4):
        raise MediaPipelineError("upscale deve essere 2 o 4")
    return value


async def run_media(
    raw_bytes: bytes,
    filename: str,
    mime: str,
    options: MediaOptions,
    *,
    cancel_check,
    job_id: str,
) -> MediaResult:
    runtime = check_media_runtime()
    if not runtime.get("media_ok"):
        detail = runtime.get("deps_error") or "setup incompleto"
        raise MediaPipelineError(
            f"Media SUPIR non configurato ({detail}). Esegui scripts/setup_media.ps1"
        )

    mime_l = str(mime or "").strip().lower()
    if mime_l not in ALLOWED_MIMES and not is_image_mime(mime_l):
        raise MediaPipelineError(f"Tipo immagine non supportato: {mime}")

    if len(raw_bytes) > config.MEDIA_MAX_INPUT_BYTES:
        raise MediaPipelineError(
            f"File troppo grande (max {config.MEDIA_MAX_INPUT_BYTES} bytes)"
        )

    if await cancel_check():
        raise MediaCancelled()

    png_bytes = await asyncio.to_thread(coerce_to_png, raw_bytes, mime_l)

    config.MEDIA_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = config.MEDIA_TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "result.png"
    try:
        with tempfile.TemporaryDirectory(dir=str(job_dir)) as scratch:
            return await _run_media_worker(
                png_bytes, filename, options, cancel_check=cancel_check,
                job_id=job_id, scratch=Path(scratch), output_path=output_path,
            )
    except BaseException:
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)
        raise
    finally:
        with contextlib.suppress(OSError):
            job_dir.rmdir()


async def _run_media_worker(
    png_bytes: bytes,
    filename: str,
    options: MediaOptions,
    *,
    cancel_check,
    job_id: str,
    scratch: Path,
    output_path: Path,
) -> MediaResult:
    input_path = scratch / "input.png"
    input_path.write_bytes(png_bytes)

    worker_script = config.BASE_DIR / "media" / "worker_infer.py"
    payload = {
        "input": str(input_path),
        "output": str(output_path),
        "mode": options.mode,
        "upscale": options.upscale,
        "fidelity": options.fidelity,
        "supir_root": str(config.MEDIA_SUPIR_ROOT),
        "yaml_config": str(config.MEDIA_SUPIR_YAML),
        "models_dir": str(config.MEDIA_MODELS_DIR),
        "edm_steps": config.MEDIA_SUPIR_EDM_STEPS,
        "max_output_side": config.MEDIA_MAX_OUTPUT_SIDE,
        "no_llava": config.MEDIA_SUPIR_NO_LLAVA,
        "loading_half_params": config.MEDIA_SUPIR_HALF_PARAMS,
        "use_tile_vae": config.MEDIA_SUPIR_USE_TILE_VAE,
        "encoder_tile_size": 512,
        "decoder_tile_size": 64,
        "ae_dtype": "bf16",
        "diff_dtype": "fp16",
    }

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
        dir=str(scratch),
    ) as fh:
        json.dump(payload, fh)
        config_path = fh.name

    cmd = [
        str(config.MEDIA_VENV_PYTHON),
        str(worker_script),
        "--config",
        config_path,
    ]

    logger.info(
        "media_infer_start | job_id=%s | engine=SUPIR-v0F | upscale=%d | file=%s",
        job_id,
        options.upscale,
        filename,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.MEDIA_SUPIR_ROOT),
    )

    try:
        stdout, stderr = await communicate_with_timeout(proc, config.MEDIA_INFERENCE_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise MediaPipelineError(
            f"Inferenza SUPIR timeout ({config.MEDIA_INFERENCE_TIMEOUT_S}s)"
        ) from exc

    if await cancel_check():
        raise MediaCancelled()

    if proc.returncode != 0:
        err_text = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
        raise MediaPipelineError(err_text or f"worker exit code {proc.returncode}")

    if not output_path.is_file():
        raise MediaPipelineError("Worker completato ma file risultato assente")

    meta: dict = {}
    meta_line = (stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    for line in reversed(meta_line):
        if line.startswith("{") and line.endswith("}"):
            try:
                meta = json.loads(line)
                break
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    if width <= 0 or height <= 0:
        width, height = await asyncio.to_thread(_read_image_size, output_path)

    elapsed_s = float(meta.get("elapsed_s") or 0.0)
    vram_peak_mb = float(meta.get("vram_peak_mb") or 0.0)

    logger.info(
        "media_infer_done | job_id=%s | size=%dx%d | elapsed_s=%.1f | vram_mb=%.0f",
        job_id,
        width,
        height,
        elapsed_s,
        vram_peak_mb,
    )
    return MediaResult(
        output_path=output_path,
        width=width,
        height=height,
        elapsed_s=elapsed_s,
        vram_peak_mb=vram_peak_mb,
    )


def _read_image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as img:
        return img.size
