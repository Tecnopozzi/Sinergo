"""Bootstrap e verifica dipendenze venv media (SUPIR)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def verify_media_imports(python_exe: Path, supir_root: Path) -> tuple[bool, str]:
    base_dir = supir_root.parent.parent
    test_script = base_dir / "scripts" / "test_media_imports.py"
    if not test_script.is_file():
        return False, f"Script test mancante: {test_script}"
    env = {"SUPIR_ROOT": str(supir_root.resolve())}
    try:
        proc = subprocess.run(
            [str(python_exe), str(test_script)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(base_dir),
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), **env},
        )
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err.splitlines()[-1] if err else f"exit {proc.returncode}"
    return True, "ok"
