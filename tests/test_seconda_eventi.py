"""Gli stream di job conclusi o espulsi devono sempre terminare, senza socket."""

import asyncio

import pytest

import server


@pytest.fixture
def eventi_isolati(monkeypatch):
    monkeypatch.setattr(server, "_CHAT_JOBS", {})
    monkeypatch.setattr(server, "_CHAT_JOB_EVENTS", {})
    monkeypatch.setattr(server, "_CHAT_JOBS_LOCK", asyncio.Lock())


@pytest.mark.anyio
@pytest.mark.parametrize("stato", ["completed", "failed", "cancelled"])
async def test_eventi_conclusi_visibili_a_tutti_i_lettori(eventi_isolati, stato):
    server._CHAT_JOBS["j"] = {"status": stato, "request_id": "r", "partial_text_len": 3, "error": "errore"}
    server._CHAT_JOB_EVENTS["j"] = asyncio.Queue()
    # Un altro lettore ha gia' consumato la notifica terminale dalla coda condivisa.
    for _ in range(2):
        risposta = await server.chat_jobs_events("j", "127.0.0.1")
        primo = await asyncio.wait_for(anext(risposta.body_iterator), 0.2)
        assert f"event: {stato}\n".encode() in primo
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(risposta.body_iterator), 0.2)


@pytest.mark.anyio
async def test_eventi_di_job_espulso_chiudono_il_lettore(eventi_isolati):
    server._CHAT_JOBS["j"] = {"status": "running"}
    coda = server._CHAT_JOB_EVENTS["j"] = asyncio.Queue()
    risposta = await server.chat_jobs_events("j", "127.0.0.1")
    await coda.put({"event": "running", "data": {"job_id": "j"}})
    assert b"event: running" in await anext(risposta.body_iterator)
    # Il lettore conserva la coda, mentre il limite storico espelle il job.
    server._CHAT_JOBS.pop("j")
    server._CHAT_JOB_EVENTS.pop("j")
    prossimo = await asyncio.wait_for(anext(risposta.body_iterator), 0.2)
    assert b"event: failed" in prossimo
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(risposta.body_iterator), 0.2)
