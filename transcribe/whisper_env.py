"""PATH CUDA per subprocess Whisper (ctranslate2 su Windows senza CUDA Toolkit).

Le wheel pip nvidia-cublas-cu12 / nvidia-cudnn-cu12 installano le DLL in
site-packages/nvidia/*/bin — vanno aggiunte al PATH prima di caricare il modello.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_CUDA_SUBDIRS = ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin")


def _venv_root_from_python(venv_python: Path | None) -> Path:
    if venv_python is not None:
        return venv_python.resolve().parent.parent
    return Path(sys.prefix)


def nvidia_cuda_bin_dirs(*, venv_root: Path | None = None) -> list[Path]:
    """Directory con DLL NVIDIA nel venv Whisper."""
    root = venv_root or Path(sys.prefix)
    nvidia = root / "Lib" / "site-packages" / "nvidia"
    dirs: list[Path] = []
    for sub in _CUDA_SUBDIRS:
        d = nvidia / Path(sub)
        if d.is_dir():
            dirs.append(d)
    return dirs


def ensure_cuda_path(*, venv_root: Path | None = None) -> list[Path]:
    """Antepone le bin CUDA al PATH del processo corrente (idempotente)."""
    dirs = nvidia_cuda_bin_dirs(venv_root=venv_root)
    if not dirs:
        return []
    prepend = os.pathsep.join(str(d) for d in dirs)
    cur = os.environ.get("PATH", "")
    if not any(str(d) in cur for d in dirs):
        os.environ["PATH"] = prepend + os.pathsep + cur
    return dirs


def whisper_subprocess_env(
    *,
    device: str = "cpu",
    venv_python: Path | None = None,
) -> dict[str, str] | None:
    """Env per asyncio.create_subprocess_exec; None = eredita env (profilo CPU)."""
    if device.lower() != "cuda":
        return None
    env = os.environ.copy()
    venv_root = _venv_root_from_python(venv_python)
    dirs = nvidia_cuda_bin_dirs(venv_root=venv_root)
    if not dirs:
        return env
    prepend = os.pathsep.join(str(d) for d in dirs)
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    return env
