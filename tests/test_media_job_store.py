"""Test job store media (senza GPU)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from media.media_job_store import get_media_store


@pytest.fixture(autouse=True)
def _reset_store(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "MEDIA_JOB_TTL_S", 3600)
    monkeypatch.setattr(config, "MEDIA_TEMP_DIR", tmp_path)
    import media.media_job_store as mod

    mod._store = None
    yield
    mod._store = None


@pytest.mark.asyncio
async def test_media_job_lifecycle(tmp_path: Path):
    store = get_media_store()
    job_id = "abc123"
    await store.create(job_id, "foto.jpg", "image/jpeg", "upscale", 2, 0.5, False)
    await store.set_running(job_id)
    out = tmp_path / "result.png"
    out.write_bytes(b"fakepng")
    await store.set_done(job_id, out, 100, 80)

    status = await store.get_status(job_id)
    assert status is not None
    assert status["status"] == "done"

    meta = await store.get_result_meta(job_id)
    assert meta is not None
    assert meta["status"] == "done"
    assert meta["width"] == 100

    data = await store.read_result_bytes(job_id)
    assert data == b"fakepng"


@pytest.mark.asyncio
async def test_media_cancel_queued():
    store = get_media_store()
    job_id = "cancelme"
    await store.create(job_id, "a.png", "image/png", "restore", 2, 0.5, False)
    res = await store.request_cancel(job_id)
    assert res["ok"] is True
    status = await store.get_status(job_id)
    assert status is not None
    assert status["status"] == "error"
    assert status.get("internal_status") == "cancelled"
