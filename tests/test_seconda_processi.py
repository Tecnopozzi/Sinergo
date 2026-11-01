"""Seconda revisione: processi simulati e PDF sintetici, senza servizi."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import config


class ProcessoFinto:
    def __init__(self):
        self.returncode = None
        self.avviato = asyncio.Event()
        self.kill_chiamato = False
        self.wait_chiamato = False
        self.terminato = asyncio.Event()
        self.lettore_annullato = False

    async def communicate(self):
        self.avviato.set()
        try:
            await self.terminato.wait()
            return b"", b""
        except asyncio.CancelledError:
            self.lettore_annullato = True
            raise

    def kill(self):
        self.kill_chiamato = True
        self.returncode = -9
        self.terminato.set()

    async def wait(self):
        self.wait_chiamato = True
        return self.returncode


def _config_media(monkeypatch, tmp_path):
    import media.media_pipeline as mod

    monkeypatch.setattr(config, "MEDIA_TEMP_DIR", tmp_path / "media")
    monkeypatch.setattr(mod, "check_media_runtime", lambda: {"media_ok": True})
    monkeypatch.setattr(mod, "coerce_to_png", lambda *args: b"png-sintetico")
    return mod


def _config_whisper(monkeypatch, tmp_path):
    python = tmp_path / "python-finto"
    python.write_text("test")
    monkeypatch.setattr(config, "WHISPER_ENABLED", True)
    monkeypatch.setattr(config, "WHISPER_VENV_PYTHON", python)
    monkeypatch.setattr(config, "WHISPER_TEMP_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_MODELS_DIR", tmp_path / "modelli")
    monkeypatch.setattr(config, "WHISPER_DEVICE", "cpu")
    monkeypatch.setattr(config, "WHISPER_AUTO_UNLOAD_LLMS", False)


@pytest.mark.anyio
@pytest.mark.parametrize("percorso", ["media", "whisper", "download"])
async def test_cancellazione_attende_termine_figlio(monkeypatch, tmp_path, percorso):
    processo = ProcessoFinto()

    async def spawn(*args, **kwargs):
        return processo

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if percorso == "media":
        mod = _config_media(monkeypatch, tmp_path)

        async def non_annullato():
            return False

        lavoro = mod.run_media(
            b"immagine", "foto.png", "image/png",
            mod.MediaOptions("restore", 2, 0.5, False),
            cancel_check=non_annullato, job_id="job-sintetico",
        )
    else:
        _config_whisper(monkeypatch, tmp_path)
        if percorso == "whisper":
            import transcribe.whisper_pipeline as mod

            monkeypatch.setattr(mod, "_semaphore", asyncio.Semaphore(1))
            lavoro = mod.run_transcription(b"audio", "a.wav", mod.WhisperOptions())
        else:
            import transcribe.whisper_admin as mod

            lavoro = mod.download_model("modello-sintetico")

    task = asyncio.create_task(lavoro)
    try:
        await asyncio.wait_for(processo.avviato.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processo.kill_chiamato, "Il figlio resta vivo dopo la cancellazione"
        assert processo.wait_chiamato, "Il figlio deve terminare prima di liberare il worker"
        assert not processo.lettore_annullato, "Le pipe devono essere drenate prima del reap"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_pdf_dimensioni_enormi_rifiutate_prima_del_raster(monkeypatch):
    import pdf_utils

    eventi = []

    class Pagina:
        def get_size(self):
            return 1_000_000, 1_000_000

        def render(self, **kwargs):
            eventi.append("render")
            raise AssertionError("Il raster enorme non deve essere allocato")

        def close(self):
            eventi.append("pagina_chiusa")

    class Documento:
        def __init__(self, raw):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index):
            return Pagina()

        def close(self):
            eventi.append("documento_chiuso")

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Documento))
    with pytest.raises(ValueError, match="dimensioni"):
        pdf_utils.render_pdf_to_pngs(b"PDF sintetico")
    assert eventi == ["pagina_chiusa", "documento_chiuso"]


def test_pdf_ordinario_chiude_documento_pagine_e_bitmap(monkeypatch):
    import pdf_utils

    eventi = []

    class Immagine:
        def save(self, buffer, **kwargs):
            buffer.write(b"png-sintetico")

        def close(self):
            eventi.append("immagine")

    class Bitmap:
        def to_pil(self):
            return Immagine()

        def close(self):
            eventi.append("bitmap")

    class Pagina:
        def get_size(self):
            return 595, 842  # A4 in punti PDF

        def render(self, **kwargs):
            return Bitmap()

        def close(self):
            eventi.append("pagina")

    class Documento:
        def __init__(self, raw):
            pass

        def __len__(self):
            return 2

        def __getitem__(self, index):
            return Pagina()

        def close(self):
            eventi.append("documento")

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Documento))
    assert pdf_utils.render_pdf_to_pngs(b"PDF sintetico") == [b"png-sintetico"] * 2
    assert eventi == ["immagine", "bitmap", "pagina"] * 2 + ["documento"]


@pytest.mark.anyio
async def test_whisper_attesa_limitata_prima_dei_temporanei(monkeypatch, tmp_path):
    import transcribe.whisper_pipeline as mod

    _config_whisper(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "WHISPER_MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(mod, "_semaphore", asyncio.Semaphore(1))
    processo = ProcessoFinto()

    async def spawn(*args, **kwargs):
        return processo

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tasks = []
    try:
        for _ in range(5):
            tasks.append(asyncio.create_task(mod.run_transcription(b"audio", "a.wav", mod.WhisperOptions())))
            await asyncio.sleep(0)
        await asyncio.wait_for(processo.avviato.wait(), 1)
        prima = set(config.WHISPER_TEMP_DIR.iterdir())
        with pytest.raises(mod.WhisperError, match="coda"):
            await asyncio.wait_for(mod.run_transcription(b"audio", "a.wav", mod.WhisperOptions()), 0.1)
        assert set(config.WHISPER_TEMP_DIR.iterdir()) == prima
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("errore_spawn", [False, True])
async def test_whisper_temporanei_ripuliti_su_interruzione(monkeypatch, tmp_path, errore_spawn):
    import transcribe.whisper_pipeline as mod

    _config_whisper(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_semaphore", asyncio.Semaphore(1))
    processo = ProcessoFinto()

    async def spawn(*args, **kwargs):
        if errore_spawn:
            raise OSError("spawn simulato")
        return processo

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(mod.run_transcription(b"audio", "a.wav", mod.WhisperOptions()))
    if errore_spawn:
        with pytest.raises(OSError, match="spawn simulato"):
            await task
    else:
        await asyncio.wait_for(processo.avviato.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert list(config.WHISPER_TEMP_DIR.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("tipo", ["ocr", "media"])
@pytest.mark.parametrize("stato", ["queued", "running"])
async def test_scadenza_conserva_job_attivi(monkeypatch, tmp_path, tipo, stato):
    if tipo == "ocr":
        import job_store as mod

        store = mod.JobStore()
        await store.create("attivo", "a.pdf", "application/pdf")
        ttl = mod.JOB_TTL_S
    else:
        from media.media_job_store import MediaJobStore

        monkeypatch.setattr(config, "MEDIA_TEMP_DIR", tmp_path)
        store = MediaJobStore()
        await store.create("attivo", "a.png", "image/png", "restore", 2, 0.5, False)
        ttl = config.MEDIA_JOB_TTL_S
    if stato == "running":
        await store.set_running("attivo")
    store._jobs["attivo"].created_mono -= ttl + 1
    await store.list_jobs_snapshot()
    status = await store.get_status("attivo")
    assert status is not None, "La scadenza non deve eliminare lavoro ancora attivo"
    assert status["status"] == stato


@pytest.mark.anyio
@pytest.mark.parametrize("errore_spawn", [False, True])
async def test_media_temporanei_ripuliti_su_interruzione(monkeypatch, tmp_path, errore_spawn):
    mod = _config_media(monkeypatch, tmp_path)
    processo = ProcessoFinto()

    async def spawn(*args, **kwargs):
        if errore_spawn:
            (config.MEDIA_TEMP_DIR / "job-sintetico" / "result.png").write_bytes(b"parziale")
            raise OSError("spawn simulato")
        return processo

    async def non_annullato():
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(mod.run_media(
        b"immagine", "foto.png", "image/png", mod.MediaOptions("restore", 2, 0.5, False),
        cancel_check=non_annullato, job_id="job-sintetico",
    ))
    if errore_spawn:
        with pytest.raises(OSError, match="spawn simulato"):
            await task
    else:
        await asyncio.wait_for(processo.avviato.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert list(config.MEDIA_TEMP_DIR.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("race_uscita", [False, True])
async def test_timeout_figlio_drena_pipe_e_gestisce_uscita_concorrente(race_uscita):
    from subprocess_utils import communicate_with_timeout

    class Processo(ProcessoFinto):
        def kill(self):
            super().kill()
            if race_uscita:
                raise ProcessLookupError("figlio gia' uscito prima del segnale")

    processo = Processo()
    with pytest.raises(asyncio.TimeoutError):
        await communicate_with_timeout(processo, 0.001)
    assert processo.kill_chiamato
    assert processo.wait_chiamato
    assert not processo.lettore_annullato


@pytest.mark.anyio
@pytest.mark.parametrize("cartella_del_job", [True, False])
async def test_media_scadenza_rimuove_solo_cartella_job(monkeypatch, tmp_path, cartella_del_job):
    from media.media_job_store import MediaJobStore

    monkeypatch.setattr(config, "MEDIA_TEMP_DIR", tmp_path / "media")
    store = MediaJobStore()
    await store.create("scaduto", "a.png", "image/png", "restore", 2, 0.5, False)
    await store.set_running("scaduto")
    parent = config.MEDIA_TEMP_DIR / "scaduto" if cartella_del_job else tmp_path / "altri_risultati"
    parent.mkdir(parents=True)
    result = parent / "result.png"
    result.write_bytes(b"risultato")
    await store.set_done("scaduto", result, 10, 8)
    store._jobs["scaduto"].updated_mono -= config.MEDIA_JOB_TTL_S + 1
    await store.list_jobs_snapshot()
    assert not result.exists()
    assert parent.exists() is not cartella_del_job


@pytest.mark.anyio
@pytest.mark.parametrize("percorso", ["media", "whisper", "download"])
async def test_figlio_completato_non_viene_ucciso_e_risultato_conservato(monkeypatch, tmp_path, percorso):
    class Processo(ProcessoFinto):
        async def communicate(self):
            self.returncode = 0
            return b'{"text":"ciao","width":10,"height":8}', b""

    processo = Processo()

    async def spawn(*args, **kwargs):
        if percorso == "media":
            payload = json.loads(Path(args[-1]).read_text())
            Path(payload["output"]).write_bytes(b"png-risultato")
        return processo

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if percorso == "media":
        mod = _config_media(monkeypatch, tmp_path)

        async def non_annullato():
            return False

        result = await mod.run_media(
            b"immagine", "foto.png", "image/png", mod.MediaOptions("restore", 2, 0.5, False),
            cancel_check=non_annullato, job_id="job-sintetico",
        )
        assert result.output_path.read_bytes() == b"png-risultato"
        assert list(result.output_path.parent.iterdir()) == [result.output_path]
        assert (result.width, result.height) == (10, 8)
    else:
        _config_whisper(monkeypatch, tmp_path)
        if percorso == "whisper":
            import transcribe.whisper_pipeline as mod

            monkeypatch.setattr(mod, "_semaphore", asyncio.Semaphore(1))
            result = await mod.run_transcription(b"audio", "a.wav", mod.WhisperOptions())
            assert result.text == "ciao"
            assert list(config.WHISPER_TEMP_DIR.iterdir()) == []
            assert mod._admitted_jobs == 0
        else:
            from transcribe.whisper_admin import download_model

            assert (await download_model("modello-sintetico"))["ok"] is True
    assert not processo.kill_chiamato
    assert processo.wait_chiamato
