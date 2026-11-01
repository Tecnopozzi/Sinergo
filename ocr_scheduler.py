"""
Scheduler OCR centralizzato.

- Coda asyncio + N worker (N = OCR_MAX_CONCURRENT_JOBS, default 1).
- Accettazione job in POST /process solo dopo JobStore.create + enqueue.
- I job restano `queued` finche' un worker non li prende (set_running).

Non usare asyncio.create_task(run_ocr) libero per job: passare sempre da qui.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import config
from job_store import get_store
from ocr_pipeline import OcrCancelled, run_ocr

logger = logging.getLogger("ocr_server.scheduler")


@dataclass(frozen=True, slots=True)
class OcrWorkItem:
    job_id: str
    raw_bytes: bytes
    filename: str
    mime: str
    max_pages: int | None = None


class OcrScheduler:
    """Coda globale + worker pool per esecuzione OCR serializzata."""

    def __init__(self) -> None:
        qmax = int(config.OCR_QUEUE_MAX_SIZE or 0)
        self._queue: asyncio.Queue[OcrWorkItem | None] = asyncio.Queue(maxsize=qmax if qmax > 0 else 0)
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    @property
    def queue(self) -> asyncio.Queue[OcrWorkItem | None]:
        return self._queue

    def start(self) -> None:
        if self._started:
            return
        n = max(1, int(config.OCR_MAX_CONCURRENT_JOBS or 1))
        for i in range(n):
            name = f"ocr_worker_{i}"
            self._tasks.append(asyncio.create_task(self._worker_loop(i), name=name))
        self._started = True
        logger.info("ocr_scheduler_started | workers=%d | queue_maxsize=%s", n, self._queue.maxsize or "unlimited")

    def submit_nowait(self, item: OcrWorkItem) -> None:
        """Accoda un job. Raises asyncio.QueueFull se la coda è piena."""

        self._queue.put_nowait(item)

    async def shutdown(self) -> None:
        """Ferma i worker con sentinelle; attende fino a OCR_JOB_SHUTDOWN_GRACE_S."""

        if not self._tasks:
            return
        for _ in self._tasks:
            await self._queue.put(None)

        wait_s = float(config.OCR_JOB_SHUTDOWN_GRACE_S or 30.0)
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, pend = await asyncio.wait(pending, timeout=wait_s)
            for t in pend:
                t.cancel()
        self._tasks.clear()
        self._started = False
        logger.info("ocr_scheduler_stopped")

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await self._run_item(worker_id, item)
            finally:
                self._queue.task_done()

    async def _run_item(self, worker_id: int, item: OcrWorkItem) -> None:
        store = get_store()
        if await store.should_skip_worker_start(item.job_id):
            logger.info("ocr_worker_skip | wid=%d | job_id=%s | reason=cancelled_or_missing", worker_id, item.job_id)
            return

        logger.info("ocr_worker_start | wid=%d | job_id=%s | file=%s", worker_id, item.job_id, item.filename)
        try:
            await store.set_running(item.job_id, total_pages=0)
            snap = await store.get_status(item.job_id)
            if snap and snap.get("internal_status") == "cancelled":
                return

            async def cancel_check() -> bool:
                return await store.maybe_cancel_requested(item.job_id)

            async def notify_total(n: int) -> None:
                await store.set_page_progress(item.job_id, 0, n)

            async def page_done(p: int, t: int) -> None:
                await store.set_page_progress(item.job_id, p, t)

            blocks, pages_detail, num_pages = await run_ocr(
                item.raw_bytes,
                item.filename,
                item.mime,
                max_pages=item.max_pages,
                cancel_check=cancel_check,
                on_total_pages=notify_total,
                on_page_complete=page_done,
            )
            await store.set_done(item.job_id, blocks, pages_detail, num_pages)
            logger.info(
                "ocr_worker_done | wid=%d | job_id=%s | pages=%d | blocks=%d",
                worker_id, item.job_id, num_pages, len(blocks),
            )
        except OcrCancelled:
            logger.info("ocr_worker_cancelled | wid=%d | job_id=%s", worker_id, item.job_id)
            await store.set_cancelled_final(item.job_id, "cancelled_by_user")
        except Exception as exc:
            msg = str(exc)
            logger.error("ocr_worker_error | wid=%d | job_id=%s | err=%s", worker_id, item.job_id, msg)
            await store.set_error(item.job_id, msg)


_scheduler: OcrScheduler | None = None


def get_scheduler() -> OcrScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = OcrScheduler()
    return _scheduler
