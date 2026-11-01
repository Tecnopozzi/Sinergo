"""
Job store in-memory per il tracking dei job OCR.

Thread-safe tramite asyncio.Lock. Pulizia automatica dei job scaduti
(TTL configurabile) ad ogni accesso.

Stati interni: queued -> running -> done | error | cancelled

Compatibilità 09_Meccan-IA (/status): gli stati `cancelled` vengono
esposti come `error` + `error="cancelled_by_user"` perché vision.py
interrompe solo su `done` o `error`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from config import JOB_TTL_S

JobStatus = Literal["queued", "running", "done", "error", "cancelled"]


class _Job:
    __slots__ = (
        "job_id",
        "status",
        "filename",
        "mime",
        "page",
        "total_pages",
        "blocks",
        "pages_detail",
        "num_pages",
        "error",
        "created_mono",
        "updated_mono",
        "created_ts",
        "updated_ts",
        "started_ts",
        "finished_ts",
        "cancel_requested",
    )

    def __init__(self, job_id: str, filename: str, mime: str) -> None:
        self.job_id = job_id
        self.status: JobStatus = "queued"
        self.filename = filename
        self.mime = mime
        self.page: int = 0
        self.total_pages: int = 0
        self.blocks: list[dict[str, Any]] = []
        self.pages_detail: list[dict[str, Any]] = []
        self.num_pages: int = 0
        self.error: str | None = None
        now_wall = time.time()
        self.created_mono = time.monotonic()
        self.updated_mono = self.created_mono
        self.created_ts = now_wall
        self.updated_ts = now_wall
        self.started_ts: float | None = None
        self.finished_ts: float | None = None
        self.cancel_requested: bool = False

    def touch(self) -> None:
        self.updated_mono = time.monotonic()
        self.updated_ts = time.time()


class JobStore:
    """Store asyncio-safe per i job OCR."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, _Job] = {}

    def _evict_expired(self) -> None:
        """Scade solo i job conclusi, a partire dall'ultimo aggiornamento."""

        now = time.monotonic()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if job.status in ("done", "error", "cancelled")
            and now - job.updated_mono > JOB_TTL_S
        ]
        for jid in expired:
            del self._jobs[jid]

    async def create(self, job_id: str, filename: str, mime: str) -> None:
        async with self._lock:
            self._evict_expired()
            self._jobs[job_id] = _Job(job_id, filename, mime)

    async def remove(self, job_id: str) -> bool:
        """Rimuove un job dalla mappa (es. enqueue fallito)."""

        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status != "queued":
                return False
            del self._jobs[job_id]
            return True

    async def set_running(self, job_id: str, total_pages: int = 0) -> None:
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
            job.total_pages = total_pages
            job.started_ts = job.started_ts or time.time()
            job.touch()

    async def set_page_progress(self, job_id: str, page: int, total_pages: int | None = None) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.page = page
            if total_pages is not None:
                job.total_pages = total_pages
            job.touch()

    async def set_done(
        self,
        job_id: str,
        blocks: list[dict[str, Any]],
        pages_detail: list[dict[str, Any]],
        num_pages: int,
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status != "running":
                return
            job.status = "done"
            job.blocks = blocks
            job.pages_detail = pages_detail
            job.num_pages = num_pages
            job.finished_ts = time.time()
            job.touch()

    async def set_error(self, job_id: str, message: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status in ("done", "error", "cancelled"):
                return
            job.status = "error"
            job.error = message
            job.finished_ts = time.time()
            job.touch()

    async def set_cancelled_final(self, job_id: str, message: str = "cancelled_by_user") -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status in ("done", "error"):
                return
            job.status = "cancelled"
            job.error = message
            job.finished_ts = time.time()
            job.touch()

    async def maybe_cancel_requested(self, job_id: str) -> bool:
        """True se il job deve fermarsi dopo la pagina corrente."""

        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return True
            if job.status == "cancelled":
                return True
            return bool(job.cancel_requested)

    async def request_cancel(self, job_id: str) -> dict[str, Any]:
        """Marca queued come cancelled; running -> cancel_requested."""

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
        """Cancella tutti i queued; richiede cancel sui running."""

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
        """Lista sintetica (piu' recenti prima)."""

        async with self._lock:
            self._evict_expired()
            rows: list[tuple[float, dict[str, Any]]] = []
            for job in self._jobs.values():
                prog = None
                if job.total_pages:
                    prog = {"page": job.page, "total": job.total_pages}
                rows.append(
                    (
                        -job.updated_ts,
                        {
                            "job_id": job.job_id,
                            "internal_status": job.status,
                            "filename": job.filename,
                            "mime": job.mime,
                            "created_at": job.created_ts,
                            "updated_at": job.updated_ts,
                            "started_at": job.started_ts,
                            "finished_at": job.finished_ts,
                            "progress": prog,
                            "error": job.error,
                            "cancel_requested": job.cancel_requested,
                        },
                    )
                )
            rows.sort(key=lambda t: t[0])
            return [item[1] for item in rows[:limit]]

    def _status_for_api(self, job: _Job) -> dict[str, Any]:
        """Mappa stato interno -> payload GET /status (compat 09)."""

        if job.status == "cancelled":
            return {
                "job_id": job.job_id,
                "status": "error",
                "progress": None,
                "error": job.error or "cancelled_by_user",
                "cancel_requested": None,
                "internal_status": "cancelled",
            }

        prog = None
        if job.total_pages:
            prog = {"page": job.page, "total": job.total_pages}

        out: dict[str, Any] = {
            "job_id": job.job_id,
            "status": job.status,
            "internal_status": job.status,
            "progress": prog,
            "error": job.error,
            "cancel_requested": True if job.status == "running" and job.cancel_requested else None,
        }
        return out

    def _result_for_api(self, job: _Job) -> dict[str, Any]:
        base = {
            "job_id": job.job_id,
            "status": job.status,
            "filename": job.filename,
            "mime": job.mime,
            "blocks": job.blocks,
            "pages_detail": job.pages_detail,
            "num_pages": job.num_pages,
            "error": job.error,
        }
        return base

    async def get_status(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            data = dict(self._status_for_api(job))
            return data

    async def get_result(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            data = dict(self._result_for_api(job))
            if job.status == "cancelled":
                data["status"] = "error"
                data["error"] = job.error or "cancelled_by_user"
            return data

    async def should_skip_worker_start(self, job_id: str) -> bool:
        """True se il job non deve partire sul worker (cancellato in coda)."""

        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return True
            return job.status in ("cancelled", "done", "error")


_store: JobStore | None = None


def get_store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore()
    return _store
