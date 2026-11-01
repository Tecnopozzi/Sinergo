"""Regressioni della revisione: solo ASGI in memoria e dati sintetici."""

import asyncio
from pathlib import Path
import runpy

import httpx
import pytest

import auth
import config
import server
from media.media_scheduler import MediaScheduler
from ocr_scheduler import OcrScheduler


@pytest.fixture
def anyio_backend():
    return "asyncio"


CHIAVE = "chiave-sintetica-revisione-3110"
HEADERS = {"Authorization": f"Bearer {CHIAVE}"}


def test_fallimenti_auth_memoria_limitata(monkeypatch):
    istante = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: istante[0])
    for _ in range(auth.FALLIMENTI_PER_MINUTO):
        assert not auth.troppi_fallimenti()
    for _ in range(2000):
        assert auth.troppi_fallimenti()
    assert len(auth._fallimenti) <= auth.FALLIMENTI_PER_MINUTO + 1
    istante[0] += 61
    assert not auth.troppi_fallimenti()


def test_bucket_auth_non_crescono_con_ip_dichiarati(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(auth, "RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(auth, "_rate_buckets", {})
    for i in range(20):
        richiesta = server.Request({
            "type": "http", "client": ("127.0.0.1", 1),
            "headers": [(b"authorization", HEADERS["Authorization"].encode()),
                        (b"x-forwarded-for", f"192.0.2.{i}".encode())],
        })
        try:
            auth.require_api_key(richiesta)
        except server.HTTPException as exc:
            assert exc.status_code == 429
    assert len(auth._rate_buckets) == 1


@pytest.mark.parametrize("tipo", ["ocr", "media"])
@pytest.mark.parametrize("valore", [None, "0", "-1", "errato"])
def test_code_upload_sempre_limitate(monkeypatch, tipo, valore):
    nome = f"{tipo.upper()}_QUEUE_MAX_SIZE"
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("REQUESTS_LOG_PATH", "")
    if valore is None:
        monkeypatch.delenv(nome, raising=False)
    else:
        monkeypatch.setenv(nome, valore)
    valori = runpy.run_path(str(Path(config.__file__)))
    monkeypatch.setattr(config, nome, valori[nome])
    sched = OcrScheduler() if tipo == "ocr" else MediaScheduler()
    assert 1 <= sched.queue.maxsize <= 4
    for _ in range(sched.queue.maxsize):
        sched.submit_nowait(object())
    with pytest.raises(asyncio.QueueFull):
        sched.submit_nowait(object())


@pytest.fixture
def chat_isolata(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "CHAT_JOB_MAX_ACTIVE", 2, raising=False)
    monkeypatch.setattr(config, "CHAT_JOB_MAX_RETAINED", 3, raising=False)
    monkeypatch.setattr(server, "_CHAT_JOBS", {})
    monkeypatch.setattr(server, "_CHAT_JOB_EVENTS", {})
    monkeypatch.setattr(server, "_CHAT_JOBS_LOCK", asyncio.Lock())
    monkeypatch.setattr(auth, "_rate_buckets", {})
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", None)


@pytest.mark.anyio
async def test_chat_job_limite_attivi_atomico(chat_isolata, monkeypatch):
    blocco = asyncio.Event()
    tasks = []

    async def inferenza(*args):
        tasks.append(asyncio.current_task())
        await blocco.wait()
        return {"choices": []}, None

    monkeypatch.setattr(server, "_run_chat_nonstream", inferenza)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        try:
            risposte = await asyncio.gather(*(
                client.post("/v1/chat/jobs", headers=HEADERS, json={"model": "m", "messages": []})
                for _ in range(5)
            ))
            assert sorted(r.status_code for r in risposte) == [202, 202, 503, 503, 503]
            assert len(server._CHAT_JOBS) == 2
            assert len(server._CHAT_JOB_EVENTS) == 2
        finally:
            blocco.set()
            await asyncio.gather(*tasks)


@pytest.mark.anyio
async def test_chat_job_storico_limitato(chat_isolata, monkeypatch):
    async def inferenza(*args):
        return {"choices": []}, None

    monkeypatch.setattr(server, "_run_chat_nonstream", inferenza)
    ids = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        for _ in range(6):
            risposta = await client.post("/v1/chat/jobs", headers=HEADERS, json={"model": "m", "messages": []})
            assert risposta.status_code == 202
            ids.append(risposta.json()["job_id"])
            for _ in range(100):
                if server._CHAT_JOBS[ids[-1]]["status"] == "completed":
                    break
                await asyncio.sleep(0)
            assert server._CHAT_JOBS[ids[-1]]["status"] == "completed"
        assert list(server._CHAT_JOBS) == ids[-3:]
        assert list(server._CHAT_JOB_EVENTS) == ids[-3:]
        assert (await client.get(f"/v1/chat/jobs/{ids[0]}", headers=HEADERS)).status_code == 404
        assert (await client.get(f"/v1/chat/jobs/{ids[-1]}/result", headers=HEADERS)).status_code == 200


@pytest.mark.anyio
async def test_chat_job_cancellato_libera_capacita(chat_isolata, monkeypatch):
    async def inferenza(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "_run_chat_nonstream", inferenza)
    server._CHAT_JOBS["annullato"] = {"status": "queued"}
    server._CHAT_JOB_EVENTS["annullato"] = asyncio.Queue()
    req = server.ChatCompletionRequest(model="m", messages=[])
    task = asyncio.create_task(server._chat_job_worker("annullato", req, "127.0.0.1"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server._CHAT_JOBS["annullato"]["status"] == "cancelled"
    assert server._CHAT_JOBS["annullato"]["finished_at"] is not None
    assert server._CHAT_JOB_EVENTS["annullato"].get_nowait()["event"] == "running"
    assert server._CHAT_JOB_EVENTS["annullato"].get_nowait()["event"] == "cancelled"


@pytest.mark.anyio
async def test_health_anonimo_limitato_prima_dei_backend(monkeypatch):
    chiamate = []

    async def modelli():
        chiamate.append(1)
        return []

    monkeypatch.setattr(server, "list_tags", modelli)
    monkeypatch.setattr(config, "LMSTUDIO_ENABLED", False)
    monkeypatch.setattr(config, "MEDIA_ENABLED", False)
    monkeypatch.setattr(config, "WHISPER_ENABLED", False)
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", None)
    monkeypatch.setattr(auth, "RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(auth, "_rate_buckets", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        risposte = [await client.get("/health", headers={"X-Forwarded-For": f"192.0.2.{i}"}) for i in range(3)]
    assert [r.status_code for r in risposte] == [200, 200, 429]
    assert len(chiamate) == 2


@pytest.mark.anyio
async def test_debug_probe_rifiuta_json_non_oggetto(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", CHIAVE)
    monkeypatch.setattr(config, "REQUESTS_LOG_PATH", None)
    monkeypatch.setattr(auth, "_rate_buckets", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test") as client:
        risposta = await client.post("/debug/probe", headers=HEADERS, json=[])
    assert risposta.status_code == 400
