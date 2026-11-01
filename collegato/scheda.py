"""Scheda della macchina: stesse fonti di `server.py` (/proc/meminfo o psutil, nvidia-smi), senza importare il server."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _ram_gb() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        for riga in Path("/proc/meminfo").read_text().splitlines():
            if riga.startswith("MemTotal:"):
                return round(int(riga.split()[1]) / 2**20, 1)
    except OSError:
        pass
    return None


def _gpu() -> list[dict]:
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total",
                                       "--format=csv,noheader,nounits"], text=True, timeout=3)
    except Exception:  # noqa: BLE001
        return []
    gpu = []
    for riga in out.splitlines():
        nome, _, mb = riga.rpartition(",")
        try:
            gpu.append({"nome": nome.strip(), "vram_gb": round(int(float(mb)) / 1024, 1)})
        except ValueError:
            continue
    return gpu


def scheda(cartella: Path | None = None) -> dict:
    from .voce import ruoli_scheda
    return {"nome": platform.node() or "computer", "cpu": platform.processor() or platform.machine(),
            "core": os.cpu_count(), "ram_gb": _ram_gb(), "gpu": _gpu(),
            "disco_gb": round(shutil.disk_usage(Path.home()).total / 2**30),
            "os": f"{platform.system()} {platform.release()}",
            "ruoli": ruoli_scheda(cartella)}
