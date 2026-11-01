"""Verifica venv media, repo SUPIR e pesi modelli."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config


def _weight_paths() -> dict[str, list[Path]]:
    root = config.MEDIA_MODELS_DIR
    return {
        "sdxl": [root / "sd_xl_base_1.0_0.9vae.safetensors"],
        "supir_f": [root / "SUPIR-v0F.ckpt"],
    }


def find_weight(kind: str) -> Path | None:
    for path in _weight_paths().get(kind, []):
        if path.is_file():
            return path
    return None


def check_media_runtime() -> dict[str, Any]:
    venv_ok = config.MEDIA_VENV_PYTHON.is_file()
    worker_ok = (config.BASE_DIR / "media" / "worker_infer.py").is_file()
    supir_root_ok = config.MEDIA_SUPIR_ROOT.is_dir()
    yaml_ok = config.MEDIA_SUPIR_YAML.is_file()
    sdxl = find_weight("sdxl")
    supir_f = find_weight("supir_f")
    models_ok = bool(sdxl and supir_f)
    deps_ok = False
    deps_error: str | None = None

    if venv_ok and supir_root_ok:
        from media.venv_bootstrap import verify_media_imports

        deps_ok, msg = verify_media_imports(
            config.MEDIA_VENV_PYTHON,
            config.MEDIA_SUPIR_ROOT,
        )
        if not deps_ok:
            deps_error = msg

    runtime_ok = (
        venv_ok
        and worker_ok
        and supir_root_ok
        and yaml_ok
        and models_ok
        and deps_ok
    )

    return {
        "media_enabled": config.MEDIA_ENABLED,
        "media_ok": runtime_ok if config.MEDIA_ENABLED else False,
        "engine": "SUPIR-v0F",
        "venv_python": str(config.MEDIA_VENV_PYTHON),
        "venv_ok": venv_ok,
        "worker_ok": worker_ok,
        "supir_root": str(config.MEDIA_SUPIR_ROOT),
        "supir_root_ok": supir_root_ok,
        "supir_yaml": str(config.MEDIA_SUPIR_YAML),
        "yaml_ok": yaml_ok,
        "deps_ok": deps_ok,
        "deps_error": deps_error,
        "models_dir": str(config.MEDIA_MODELS_DIR),
        "models": {
            "sdxl": str(sdxl) if sdxl else None,
            "supir_v0f": str(supir_f) if supir_f else None,
        },
        "vram_profile": {
            "no_llava": config.MEDIA_SUPIR_NO_LLAVA,
            "use_tile_vae": config.MEDIA_SUPIR_USE_TILE_VAE,
            "half_params": config.MEDIA_SUPIR_HALF_PARAMS,
            "max_output_side": config.MEDIA_MAX_OUTPUT_SIDE,
        },
    }
