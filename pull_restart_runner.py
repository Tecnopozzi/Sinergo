"""
Accoda git pull + riavvio del server in un processo separato (stessi privilegi di uvicorn).

Usato da POST /admin/pull-restart: la risposta HTTP viene inviata prima che il worker
termini il processo in ascolto sulla PORT.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import config

logger = logging.getLogger("ocr_server")


def schedule_pull_restart() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            "POST /admin/pull-restart è supportato solo su Windows (worker PowerShell)."
        )

    ps1 = config.BASE_DIR / "scripts" / "pull_restart_10b.ps1"
    if not ps1.is_file():
        raise FileNotFoundError(f"Script mancante: {ps1}")

    log_dir = config.BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "pull_restart_worker.log"

    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
        "-RepoRoot",
        str(config.GIT_REPO_ROOT),
        "-Port",
        str(config.PORT),
        "-PreDelaySeconds",
        str(config.PULL_RESTART_PREDELAY_S),
        "-HealthWaitSeconds",
        "12",
        "-LogPath",
        str(log_path),
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        else:
            creationflags |= 0x08000000
        subprocess.Popen(
            args,
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    else:
        subprocess.Popen(
            args,
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )

    logger.info("pull_restart_scheduled | log=%s", log_path)
