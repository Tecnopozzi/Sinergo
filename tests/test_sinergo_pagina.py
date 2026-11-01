"""Pagina locale e installatore Sinergo, senza servizi esterni."""
from __future__ import annotations

import importlib
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def pagina():
    assert importlib.util.find_spec("collegato.pagina") is not None, "manca la pagina Sinergo"
    return importlib.import_module("collegato.pagina")


def client(stato):
    return TestClient(pagina().crea_app(stato), base_url="http://127.0.0.1")


def test_pagina_codice_tre_blocchi_e_stato_in_italiano():
    stato = pagina().Stato("0123-4567-89AB")
    with client(stato) as c:
        risposta = c.get("/", headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
    assert risposta.status_code == 200
    assert '<title>Sinergo</title>' in risposta.text
    assert 'lang="it"' in risposta.text
    assert '0123-4567-89AB' in risposta.text
    assert 'Cerco Kairos' in risposta.text
    assert 'Dove si trova il tuo Kairos?' not in risposta.text
    assert risposta.headers["cache-control"] == "no-store"


def test_pagina_lingua_browser_inglese_e_stato_aggiornato():
    stato = pagina().Stato("0123-4567-89AB", stato="in_attesa")
    with client(stato) as c:
        risposta = c.get("/", headers={"Accept-Language": "en-GB,en;q=0.9,it;q=0.7"})
        assert 'lang="en"' in risposta.text
        assert 'Waiting for the code' in risposta.text
        stato.codice = "CDEF-GHJK-MNPQ"
        assert "CDEF-GHJK-MNPQ" in c.get("/stato").text


def test_pagina_campo_indirizzo_solo_a_ricerca_finita():
    stato = pagina().Stato("0123-4567-89AB", ricerca_finita=True)
    with client(stato) as c:
        risposta = c.get("/")
        assert "Dove si trova il tuo Kairos?" in risposta.text
        scelta = c.post("/scegli", json={"indirizzo": "https://kairos.esempio.it"})
    assert scelta.status_code == 200
    assert stato.indirizzo == "https://kairos.esempio.it"
    assert stato.indirizzo_scelto.is_set()


def test_pagina_scelta_fra_piu_kairos_e_nomi_escapati():
    stato = pagina().Stato("0123-4567-89AB", ricerca_finita=True,
                           kairos=[{"nome": "Casa <script>", "indirizzo": "http://192.168.1.2:9200"},
                                   {"nome": "Studio", "indirizzo": "https://studio.esempio.it"}])
    with client(stato) as c:
        risposta = c.get("/")
        assert '<select' in risposta.text
        assert "Casa &lt;script&gt;" in risposta.text
        assert "Dove si trova il tuo Kairos?" not in risposta.text
        assert c.post("/scegli", json={"indirizzo": "https://studio.esempio.it"}).status_code == 200
    assert stato.indirizzo == "https://studio.esempio.it"


@pytest.mark.parametrize("stato_atteso,testo", [("trovato", "Trovato Casa"),
                                                ("abbinato", "Abbinato"),
                                                ("errore", "Errore")])
def test_pagina_stati(stato_atteso, testo):
    stato = pagina().Stato("0123-4567-89AB", stato=stato_atteso,
                           kairos=[{"nome": "Casa", "indirizzo": "http://192.168.1.2:9200"}])
    with client(stato) as c:
        assert testo in c.get("/").text


def test_socket_prima_porta_libera_e_solo_loopback():
    primo = pagina().prenota_socket(0)
    try:
        porta = primo.getsockname()[1]
        assert primo.getsockname()[0] == "127.0.0.1"
        secondo = pagina().prenota_socket(porta)
        try:
            assert secondo.getsockname()[0] == "127.0.0.1"
            assert secondo.getsockname()[1] > porta
        finally:
            secondo.close()
    finally:
        primo.close()


def test_codice_non_compare_nei_log(caplog):
    codice = "0123-4567-89AB"
    with caplog.at_level(logging.DEBUG), client(pagina().Stato(codice)) as c:
        assert codice in c.get("/").text
        assert codice in c.get("/stato").text
    assert codice not in caplog.text


def test_pagina_rifiuta_host_esterno_e_post_da_altra_origine():
    with client(pagina().Stato("0123-4567-89AB", ricerca_finita=True)) as c:
        assert c.get("/", headers={"Host": "cattivo.esempio.it"}).status_code == 403
        assert c.post("/scegli", json={"indirizzo": "https://kairos.esempio.it"},
                      headers={"Origin": "https://cattivo.esempio.it"}).status_code == 403


def test_pagina_rifiuta_indirizzo_malformato():
    stato = pagina().Stato("0123-4567-89AB", ricerca_finita=True)
    with client(stato) as c:
        assert c.post("/scegli", json={"indirizzo": "file:///tmp/kairos"}).status_code == 400
    assert stato.indirizzo is None


def test_pagina_errore_consente_altro_indirizzo_anche_dopo_discovery():
    stato = pagina().Stato("0123-4567-89AB", stato="errore", ricerca_finita=True,
                           indirizzo="http://192.168.1.2:9200",
                           kairos=[{"nome": "Casa", "indirizzo": "http://192.168.1.2:9200"}])
    with client(stato) as c:
        assert "Dove si trova il tuo Kairos?" in c.get("/").text
        assert c.post("/scegli", json={"indirizzo": "https://altro.esempio.it"}).status_code == 200
    assert stato.indirizzo == "https://altro.esempio.it"


def test_installa_sinergo_dipendenze_e_avvio_senza_env(tmp_path, monkeypatch):
    import installa

    assert hasattr(installa, "installa_sinergo"), "manca l'installazione Sinergo"
    # uv sync crea .venv: qui lo fingiamo cosi' l'avvio puo' partire.
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    chiamate = []
    monkeypatch.setattr(installa.subprocess, "run", lambda argv, **kw: chiamate.append((argv, kw)))
    installa.installa_sinergo(tmp_path)
    assert chiamate[0][0] == ["uv", "sync", "--extra", "voce", "--extra", "chatterbox"]
    assert chiamate[1][0] == [str(python), "-m", "collegato"]
    assert all(opzioni["cwd"] == tmp_path and opzioni["check"] for _, opzioni in chiamate)
    assert not (tmp_path / ".env").exists()
    assert not hasattr(installa, "_ha_nvidia")


def test_installa_flag_sinergo_non_chiede_configurazione(monkeypatch):
    import installa

    assert hasattr(installa, "installa_sinergo"), "manca il flag --sinergo"
    eseguiti = []
    monkeypatch.setattr(installa, "installa_sinergo", lambda: eseguiti.append("sinergo"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("nessuna domanda per Sinergo"))
    monkeypatch.setattr(sys, "argv", ["installa.py", "--sinergo"])
    installa.main()
    assert eseguiti == ["sinergo"]


def test_installa_sinergo_parte_anche_senza_dipendenze_installate():
    esito = subprocess.run([sys.executable, "-S", "installa.py", "--help"],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert esito.returncode == 0, esito.stderr
    assert "--sinergo" in esito.stdout


def test_avvio_automatico_windows_affianca_una_voce_sinergo(tmp_path, monkeypatch):
    import installa
    from types import SimpleNamespace

    assert hasattr(installa, "avvio_automatico_sinergo"), "manca la voce di avvio Sinergo"
    valori = []

    class Registro:
        def __enter__(self):
            return "chiave"

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(installa.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER=1, REG_SZ=1, CreateKey=lambda *a: Registro(),
        SetValueEx=lambda *a: valori.append(a)))
    installa.avvio_automatico_sinergo(tmp_path)
    assert len(valori) == 1
    assert valori[0][1] == "Sinergo"
    assert "autostart.vbs" in valori[0][-1] and valori[0][-1].endswith(" sinergo")
