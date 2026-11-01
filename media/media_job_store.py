"""
Job store in-memory per il servizio Media Enhancement.

Separato dal job store OCR. Persiste il PNG risultato su disco in
MEDIA_TEMP_DIR fino a scadenza TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any, Literal

import config

MediaJobStatus = Literal["queued", "running", "done", "error", "cancelled"]


class _MediaJob:
    __slots__ = (
        "job_id",
        "status",
        "filename",
        "mime",
        "mode",
        "upscale",
        "fidelity",
        "face_only",
        "width",
        "height",
        "error",
        "result_path",
        "created_mono",
        "updated_mono",
        "created_ts",
        "updated_ts",
        "started_ts",
        "finished_ts",
        "cancel_requested",
    )

    def __init__(
        self,
        job_id: str,
        filename: str,
        mime: str,
        mode: str,
        upscale: int,
        fidelity: float,
        face_only: bool,
    ) -> None:
        self.job_id = job_id
        self.status: MediaJobStatus = "queued"
        self.filename = filename
        self.mime = mime
        self.mode = mode
        self.upscale = upscale
        self.fidelity = fidelity
        self.face_only = face_only
        self.width = 0
        self.height = 0
        self.error: str | None = None
        self.result_path: Path | None = None
        now_wall = time.time()
        self.created_mono = time.monotonic()
        self.updated_mono = self.created_mono
        self.created_ts = now_wall
        self.updated_ts = now_wall
        self.started_ts: float | None = None
        self.finished_ts: float | None = None
        self.cancel_requested = False

    def touch(self) -> None:
        self.updated_mono = time.monotonic()
        self.updated_ts = time.time()


class MediaJobStore:
    """Store asyncio-safe per job media."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, _MediaJob] = {}

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if job.status in ("done", "error", "cancelled")
            and now - job.updated_mono > config.MEDIA_JOB_TTL_S
        ]
        for jid in expired:
            job = self._jobs.pop(jid, None)
            if job and job.result_path and job.result_path.is_file():
                with contextlib.suppress(Exception):
                    job.result_path.unlink()
                    if job.result_path.parent == config.MEDIA_TEMP_DIR / jid:
                        job.result_path.parent.rmdir()

    async def create(
        self,
        job_id: str,
        filename: str,
        mime: str,
        mode: str,
        upscale: int,
        fidelity: float,
        face_only: bool,
    ) -> None:
        async with self._lock:
            self._evict_expired()
            self._jobs[job_id] = _MediaJob(
                job_id, filename, mime, mode, upscale, fidelity, face_only
            )

    async def remove(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "queued":
                return False
            del self._jobs[job_id]
            return True

    async def set_running(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status in ("done", "error", "cancelled"):
                return
            if job.status == "queued" and job.cancel_requested:
                job.status = "cancelled"
                job.error = "cancelled_by_user"
                job.finished_ts = time.time()
                job.touch()
                return
            job.status = "running"
            job.started_ts = job.started_ts or time.time()
            job.touch()

    async def set_done(
        self,
        job_id: str,
        result_path: Path,
        width: int,
        height: int,
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "running":
                return
            job.status = "done"
            job.result_path = result_path
            job.width = width
            job.height = height
            job.finished_ts = time.time()
            job.touch()

    async def set_error(self, job_id: str, message: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in ("done", "error", "cancelled"):
                return
            job.status = "error"
            job.error = message
            job.finished_ts = time.time()
            job.touch()

    async def set_cancelled_final(self, job_id: str, message: str = "cancelled_by_user") -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in ("done", "error"):
                return
            job.status = "cancelled"
            job.error = message
            job.finished_ts = time.time()
            job.touch()

    async def maybe_cancel_requested(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return True
            if job.status == "cancelled":
                return True
            return bool(job.cancel_requested)

    async def request_cancel(self, job_id: str) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "reason": "not_found"}
            prev = job.status
            if prev == "done":
                return {"ok": True, "previous": "done", "state": "done", "note": "already_done"}
            if prev == "error":
                return {"ok": True, "previous": "error", "state": "error", "note": "already_error"}
            if prev == "cancelled":
                return {"ok": True, "previous": "cancelled", "state": "cancelled", "note": "already_cancelled"}
            if prev == "queued":
                job.status = "cancelled"
                job.error = "cancelled_by_user"
                job.finished_ts = time.time()
                job.touch()
                return {"ok": True, "previous": "queued", "state": "cancelled"}
            if prev == "running":
                job.cancel_requested = True
                job.touch()
                return {"ok": True, "previous": "running", "state": "running", "cancel_requested": True}
            return {"ok": False, "reason": "unexpected", "previous": prev}

    async def cancel_all(self) -> dict[str, int]:
        async with self._lock:
            q = 0
            r = 0
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "cancelled"
                    job.error = "cancelled_by_user"
                    job.finished_ts = time.time()
                    job.touch()
                    q += 1
                elif job.status == "running":
                    job.cancel_requested = True
                    job.touch()
                    r += 1
            return {"queued_cancelled": q, "running_cancel_requested": r}

    async def list_jobs_snapshot(self, limit: int = 500) -> list[dict[str, Any]]:
        async with self._lock:
            self._evict_expired()
            rows: list[tuple[float, dict[str, Any]]] = []
            for job in self._jobs.values():
                rows.append(
                    (
                        -job.updated_ts,
                        {
                            "job_id": job.job_id,
                            "status": job.status,
                            "filename": job.filename,
                            "mime": job.mime,
                            "mode": job.mode,
                            "upscale": job.upscale,
                            "fidelity": job.fidelity,
                            "face_only": job.face_only,
                            "width": job.width or None,
                            "height": job.height or None,
                            "created_at": job.created_ts,
                            "updated_at": job.updated_ts,
                            "started_at": job.started_ts,
                            "finished_at": job.finished_ts,
                            "error": job.error,
                            "cancel_requested": job.cancel_requested,
                        },
                    )
                )
            rows.sort(key=lambda t: t[0])
            return [item[1] for item in rows[:limit]]

    async def get_status(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status == "cancelled":
                return {
                    "job_id": job.job_id,
                    "status": "error",
                    "error": job.error or "cancelled_by_user",
                    "internal_status": "cancelled",
                    "mode": job.mode,
                }
            return {
                "job_id": job.job_id,
                "status": job.status,
                "internal_status": job.status,
                "error": job.error,
                "mode": job.mode,
                "upscale": job.upscale,
                "width": job.width or None,
                "height": job.height or None,
                "cancel_requested": True if job.status == "running" and job.cancel_requested else None,
            }

    async def get_result_meta(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            status = job.status
            if status == "cancelled":
                status = "error"
            return {
                "job_id": job.job_id,
                "status": status,
                "filename": job.filename,
                "mime": "image/png",
                "mode": job.mode,
                "upscale": job.upscale,
                "fidelity": job.fidelity,
                "face_only": job.face_only,
                "width": job.width,
                "height": job.height,
                "error": job.error,
                "result_path": job.result_path,
            }

    async def read_result_bytes(self, job_id: str) -> bytes | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "done" or not job.result_path:
                return None
            path = job.result_path
        if not path.is_file():
            return None
        return path.read_bytes()

    async def should_skip_worker_start(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return True
            return job.status in ("cancelled", "done", "error")


_store: MediaJobStore | None = None


def get_media_store() -> MediaJobStore:
    global _store
    if _store is None:
        _store = MediaJobStore()
    return _store
