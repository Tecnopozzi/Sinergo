"""
Scheduler Media — coda asyncio + worker pool separati dall'OCR.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import config
from media.media_job_store import get_media_store
from media.media_pipeline import MediaCancelled, MediaOptions, run_media

logger = logging.getLogger("ocr_server.media_scheduler")


@dataclass(frozen=True, slots=True)
class MediaWorkItem:
    job_id: str
    raw_bytes: bytes
    filename: str
    mime: str
    mode: str
    upscale: int
    fidelity: float
    face_only: bool


class MediaScheduler:
    def __init__(self) -> None:
        qmax = int(config.MEDIA_QUEUE_MAX_SIZE or 0)
        self._queue: asyncio.Queue[MediaWorkItem | None] = asyncio.Queue(
            maxsize=qmax if qmax > 0 else 0
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._active_proc: dict[str, asyncio.subprocess.Process] = {}

    @property
    def queue(self) -> asyncio.Queue[MediaWorkItem | None]:
        return self._queue

    def start(self) -> None:
        if self._started:
            return
        n = max(1, int(config.MEDIA_MAX_CONCURRENT_JOBS or 1))
        for i in range(n):
            self._tasks.append(
                asyncio.create_task(self._worker_loop(i), name=f"media_worker_{i}")
            )
        self._started = True
        logger.info(
            "media_scheduler_started | workers=%d | queue_maxsize=%s",
            n,
            self._queue.maxsize or "unlimited",
        )

    def submit_nowait(self, item: MediaWorkItem) -> None:
        self._queue.put_nowait(item)

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        for _ in self._tasks:
            await self._queue.put(None)
        wait_s = float(config.MEDIA_JOB_SHUTDOWN_GRACE_S or 30.0)
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, pend = await asyncio.wait(pending, timeout=wait_s)
            for t in pend:
                t.cancel()
        self._tasks.clear()
        self._started = False
        logger.info("media_scheduler_stopped")

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await self._run_item(worker_id, item)
            finally:
                self._queue.task_done()

    async def _run_item(self, worker_id: int, item: MediaWorkItem) -> None:
        store = get_media_store()
        if await store.should_skip_worker_start(item.job_id):
            logger.info(
                "media_worker_skip | wid=%d | job_id=%s",
                worker_id,
                item.job_id,
            )
            return

        logger.info(
            "media_worker_start | wid=%d | job_id=%s | file=%s | mode=%s",
            worker_id,
            item.job_id,
            item.filename,
            item.mode,
        )
        try:
            await store.set_running(item.job_id)
            snap = await store.get_status(item.job_id)
            if snap and snap.get("internal_status") == "cancelled":
                return

            async def cancel_check() -> bool:
                return await store.maybe_cancel_requested(item.job_id)

            options = MediaOptions(
                mode=item.mode,
                upscale=item.upscale,
                fidelity=item.fidelity,
                face_only=item.face_only,
            )
            result = await run_media(
                item.raw_bytes,
                item.filename,
                item.mime,
                options,
                cancel_check=cancel_check,
                job_id=item.job_id,
            )
            await store.set_done(
                item.job_id,
                result.output_path,
                result.width,
                result.height,
            )
            logger.info("media_worker_done | wid=%d | job_id=%s", worker_id, item.job_id)
        except MediaCancelled:
            logger.info("media_worker_cancelled | wid=%d | job_id=%s", worker_id, item.job_id)
            await store.set_cancelled_final(item.job_id, "cancelled_by_user")
        except Exception as exc:
            msg = str(exc)
            logger.error(
                "media_worker_error | wid=%d | job_id=%s | err=%s",
                worker_id,
                item.job_id,
                msg,
            )
            await store.set_error(item.job_id, msg)


_scheduler: MediaScheduler | None = None


def get_media_scheduler() -> MediaScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MediaScheduler()
    return _scheduler
