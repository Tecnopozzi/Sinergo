"""Contratto del canale Sinergo, firme reali e soli programmi avviati dal test."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest


@pytest.fixture
def moduli():
    if importlib.util.find_spec("collegato.canale") is None:
        pytest.skip("il primo ciclo rosso verifica il modulo ancora assente")
    return importlib.import_module("collegato.canale"), importlib.import_module("collegato.firma")


def test_client_del_canale_disponibile():
    assert importlib.util.find_spec("collegato.canale"), "manca il client del canale Sinergo"


def coppia():
    privata = Ed25519PrivateKey.generate()
    return privata.private_bytes_raw(), privata.public_key().public_bytes_raw()


@pytest.fixture
def banco(moduli, tmp_path):
    canale, firma = moduli
    sk_m, pk_m = coppia()
    sk_k, pk_k = coppia()
    sk_c, pk_c = coppia()
    cliente = canale.Cliente("macchina-test", sk_m, pk_k, pk_c, cartella_chiavi=tmp_path)
    cliente.ciao()
    cliente.ricevi(json.dumps(firma.firma(sk_k, "ciao_ok", {})))
    return cliente, firma, sk_m, pk_m, sk_k, pk_k, sk_c, pk_c


def test_firma_identica_al_riferimento(moduli):
    canale, firma = moduli
    # Impronta del riferimento ricevuto: il relativo pacchetto resta fuori dal commit.
    assert hashlib.sha256(Path(firma.__file__).read_bytes()).hexdigest() == "6a6011af81aaf50b3115dd41d56273bc404b20a93b024b5e60ef5086c1f3bbf1"


def test_interoperabilita_firma_con_implementazione_indipendente(moduli):
    _, firma = moduli
    sk, pk = coppia()
    ora = time.time()
    msg = {"id": "ba9f6b73-7677-4417-bc30-f4426e92f94b", "emesso": firma._iso(ora),
           "scade": firma._iso(ora + 120), "tipo": "scheda", "corpo": {"nome": "città"}}
    canonico = json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    msg["firma"] = Ed25519PrivateKey.from_private_bytes(sk).sign(canonico).hex()
    assert firma.verifica(pk, msg, firma.Visti()) == msg
    emesso = firma.firma(sk, "ping", {"nome": "città"})
    grezzo = {k: v for k, v in emesso.items() if k != "firma"}
    Ed25519PrivateKey.from_private_bytes(sk).public_key().verify(
        bytes.fromhex(emesso["firma"]),
        json.dumps(grezzo, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def test_ciao_e_ping_hanno_firma_macchina(banco):
    cliente, firma, _, pk_m, sk_k, *_ = banco
    ciao = firma.verifica(pk_m, json.loads(cliente.ciao()), firma.Visti())
    assert ciao["tipo"] == "ciao"
    assert ciao["corpo"] == {"id_macchina": "macchina-test"}
    assert cliente.ricevi(json.dumps(firma.firma(sk_k, "ciao_ok", {}))) is None
    pong = cliente.ricevi(json.dumps(firma.firma(sk_k, "ping", {})))
    assert firma.verifica(pk_m, json.loads(pong), firma.Visti())["tipo"] == "pong"


@pytest.mark.parametrize("difetto", ["firma", "id_ripetuto", "scaduto", "orologio", "finestra"])
def test_guardie_del_canale_scartano_e_registrano(banco, tmp_path, difetto):
    cliente, firma, _, _, sk_k, *_ = banco
    ora = time.time()
    msg = firma.firma(sk_k, "ping", {}, ora=ora)
    if difetto == "firma":
        msg["firma"] = "00" * 64
    elif difetto == "id_ripetuto":
        cliente.ricevi(json.dumps(msg))
    elif difetto == "scaduto":
        msg = firma.firma(sk_k, "ping", {}, ora=ora - 30)
        msg["scade"] = firma._iso(ora - 1)
        msg["firma"] = Ed25519PrivateKey.from_private_bytes(sk_k).sign(firma._canonico(msg)).hex()
    elif difetto == "orologio":
        msg = firma.firma(sk_k, "ping", {}, ora=ora + 61)
    else:
        msg["scade"] = firma._iso(ora + 121)
        msg["firma"] = Ed25519PrivateKey.from_private_bytes(sk_k).sign(firma._canonico(msg)).hex()
    with pytest.raises(firma.MessaggioRifiutato):
        cliente.ricevi(json.dumps(msg))
    riga = json.loads((tmp_path / "comandi.log").read_text().splitlines()[-1])
    assert riga["esito"] == "rifiutato"
    assert {"chi", "quando", "cosa", "durata"} <= riga.keys()


def test_firma_sbagliata_non_brucia_id(banco):
    cliente, firma, _, _, sk_k, *_ = banco
    originale = firma.firma(sk_k, "ping", {})
    falso = {**originale, "firma": "00" * 64}
    with pytest.raises(firma.MessaggioRifiutato):
        cliente.ricevi(json.dumps(falso))
    assert json.loads(cliente.ricevi(json.dumps(originale)))["tipo"] == "pong"


def test_json_troppo_annidato_viene_rifiutato_e_registrato(banco, tmp_path):
    cliente, firma, *_ = banco
    frame = '{"tipo":"ping","corpo":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    with pytest.raises(firma.MessaggioRifiutato):
        cliente.ricevi(frame)
    riga = json.loads((tmp_path / "comandi.log").read_text())
    assert riga["esito"] == "rifiutato"
    assert riga["cosa"] == {"id": None, "tipo": None}


def corpo_comando(programma=sys.executable, argomenti=None, tetto=5):
    return {"programma": programma, "argomenti": argomenti or ["-c", "print('sinergo-test')"],
            "cartella": "", "tetto_secondi": tetto}


def test_chiave_lavori_non_autorizza_comandi(banco, tmp_path):
    cliente, firma, _, _, sk_k, *_ = banco
    with pytest.raises(firma.MessaggioRifiutato):
        cliente.ricevi(json.dumps(firma.firma(sk_k, "comando", corpo_comando())))
    riga = json.loads((tmp_path / "comandi.log").read_text())
    assert riga["esito"] == "rifiutato"
    assert riga["cosa"]["tipo"] == "comando"


def test_comando_eseguito_senza_shell_e_registro_append(banco, tmp_path):
    cliente, firma, _, pk_m, _, _, sk_c, _ = banco
    argomenti = ["-c", "import sys; print(sys.argv[1])", "test; $(sinergo) & | >"]
    for _ in range(2):
        msg = firma.firma(sk_c, "comando", corpo_comando(argomenti=argomenti))
        risposta = firma.verifica(pk_m, json.loads(cliente.ricevi(json.dumps(msg))), firma.Visti())
        assert risposta["corpo"]["a"] == msg["id"]
        assert risposta["corpo"]["ok"] is True
        assert risposta["corpo"]["dati"]["uscita"] == 0
        assert risposta["corpo"]["dati"]["stdout"].strip() == argomenti[-1]
    righe = [json.loads(r) for r in (tmp_path / "comandi.log").read_text().splitlines()]
    assert len(righe) == 4
    assert [r["esito"] for r in righe] == ["avviato", "ok", "avviato", "ok"]
    assert all(r["durata"] >= 0 for r in righe)
    assert righe[0]["cosa"]["corpo"]["argomenti"] == argomenti


def test_comando_rispetta_tetto_e_registra_timeout(banco, tmp_path):
    cliente, firma, _, _, _, _, sk_c, _ = banco
    msg = firma.firma(sk_c, "comando", corpo_comando(argomenti=["-c", "import time; time.sleep(5)"], tetto=1))
    inizio = time.monotonic()
    risposta = json.loads(cliente.ricevi(json.dumps(msg)))["corpo"]
    assert time.monotonic() - inizio < 3
    assert risposta["ok"] is False
    assert "tempo" in risposta["errore"]
    assert json.loads((tmp_path / "comandi.log").read_text().splitlines()[-1])["esito"] == "timeout"


def test_output_comando_resta_sotto_limite_frame_anche_con_escape_json(banco, moduli):
    cliente, firma, _, pk_m, _, _, sk_c, _ = banco
    canale, _ = moduli
    script = "import sys; sys.stdout.buffer.write(b'\\x01'*8192); sys.stderr.buffer.write(b'\\x02'*8192)"
    msg = firma.firma(sk_c, "comando", corpo_comando(argomenti=["-c", script]))
    risposta = cliente.ricevi(json.dumps(msg))
    assert len(risposta.encode("utf-8")) <= canale.FRAME_MAX
    assert firma.verifica(pk_m, json.loads(risposta), firma.Visti())["corpo"]["ok"] is True


@pytest.mark.parametrize("campo,valore", [("argomenti", "-c print(1)"), ("tetto_secondi", 0),
                                        ("tetto_secondi", True), ("cartella", []), ("programma", "")])
def test_comando_malformato_non_parte(banco, campo, valore):
    cliente, firma, _, _, _, _, sk_c, _ = banco
    corpo = {**corpo_comando(), campo: valore}
    risposta = json.loads(cliente.ricevi(json.dumps(firma.firma(sk_c, "comando", corpo))))["corpo"]
    assert risposta["ok"] is False
    assert risposta["errore"] == "comando malformato"


def test_solo_lettura_usa_fonti_router_e_null_per_misure_ignote(banco, monkeypatch):
    cliente, firma, _, _, sk_k, *_ = banco
    canale = importlib.import_module("collegato.canale")
    fonti = {"/health": {"status": "degraded"}, "/v1/models": {"data": [{"id": "modello-reale"}]},
             "/metrics/system": {"memory": {"used_bytes": 2 * 2**30},
                                 "gpu": {"available": True, "gpus": [{"vram_used_mb": 1024}]}}}
    monkeypatch.setattr(canale, "_leggi_router", lambda percorso: fonti[percorso])
    monkeypatch.setattr(canale, "scheda", lambda: {"nome": "pc-test", "ram_gb": 32.0})
    def dati(tipo):
        return json.loads(cliente.ricevi(json.dumps(firma.firma(sk_k, tipo, {}))))["corpo"]["dati"]
    assert dati("scheda")["nome"] == "pc-test"
    assert dati("salute")["stato"] == "degraded"
    assert dati("modelli") == {"modelli": ["modello-reale"]}
    assert dati("misura") == {"token_s": None, "ram_gb": 2.0, "vram_gb": 1.0, "cache": None}
    monkeypatch.setattr(canale, "_leggi_router", lambda percorso: None)
    assert dati("salute") == {"stato": None}
    assert dati("modelli") == {"modelli": None}
    assert dati("misura") == {"token_s": None, "ram_gb": None, "vram_gb": None, "cache": None}


def test_http_router_solo_loopback_senza_proxy_o_redirect(moduli, monkeypatch):
    canale, _ = moduli
    richieste = []
    client_reale = httpx.Client
    def ricevi(request):
        richieste.append(request)
        return httpx.Response(200, json={"status": "ok"})
    def crea_client(**opzioni):
        assert opzioni["trust_env"] is False
        assert opzioni["follow_redirects"] is False
        return client_reale(transport=httpx.MockTransport(ricevi), **opzioni)
    monkeypatch.setattr(canale.httpx, "Client", crea_client)
    monkeypatch.setenv("PORT", "8088")
    monkeypatch.setenv("API_KEY", "chiave-del-test")
    assert canale._leggi_router("/health") == {"status": "ok"}
    assert str(richieste[0].url) == "http://127.0.0.1:8088/health"
    assert richieste[0].headers["Authorization"] == "Bearer chiave-del-test"


def test_json_router_troppo_annidato_restituisce_dato_sconosciuto(moduli, monkeypatch):
    canale, _ = moduli
    client_reale = httpx.Client
    profondo = '{"data":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    def ricevi(request):
        return httpx.Response(200, text=profondo)
    monkeypatch.setattr(canale.httpx, "Client", lambda **opzioni:
                        client_reale(transport=httpx.MockTransport(ricevi), **opzioni))
    assert canale._leggi_router("/v1/models") is None


def test_primo_frame_kairos_deve_essere_ciao_ok(moduli, tmp_path):
    canale, firma = moduli
    sk_m, _ = coppia()
    sk_k, pk_k = coppia()
    _, pk_c = coppia()
    cliente = canale.Cliente("m", sk_m, pk_k, pk_c, cartella_chiavi=tmp_path)
    cliente.ciao()
    with pytest.raises(firma.MessaggioRifiutato):
        cliente.ricevi(json.dumps(firma.firma(sk_k, "ping", {})))


@pytest.mark.asyncio
async def test_riconnessione_attende_da_uno_a_sessanta(moduli, monkeypatch, tmp_path):
    canale, _ = moduli
    from collegato import attesa, chiavi
    sk_m, _ = coppia()
    _, pk_k = coppia()
    _, pk_c = coppia()
    monkeypatch.setattr(chiavi, "carica", lambda d=None: (sk_m, pk_k, pk_c, "m"))
    monkeypatch.setattr(chiavi, "abbinata", lambda d=None: True)
    @asynccontextmanager
    async def connetti(*args, **kwargs):
        raise OSError("server spento")
        yield
    monkeypatch.setattr(attesa, "connetti", connetti)
    ritardi = []
    async def dormi(secondi):
        ritardi.append(secondi)
        if len(ritardi) == 8:
            raise asyncio.CancelledError
    monkeypatch.setattr(canale.asyncio, "sleep", dormi)
    with pytest.raises(asyncio.CancelledError):
        await canale.esegui("http://127.0.0.1:9999", tmp_path)
    assert ritardi == [1, 2, 4, 8, 16, 32, 60, 60]


@pytest.mark.asyncio
async def test_canale_replay_anche_dopo_riconnessione_e_revoca(moduli, monkeypatch, tmp_path):
    canale, firma = moduli
    from collegato import chiavi
    from websockets.asyncio.server import serve
    sk_m, pk_m = coppia()
    sk_k, pk_k = coppia()
    _, pk_c = coppia()
    monkeypatch.setattr(chiavi, "carica", lambda d=None: (sk_m, pk_k, pk_c, "m"))
    abbinata = True
    monkeypatch.setattr(chiavi, "abbinata", lambda d=None: abbinata)
    replay = firma.firma(sk_k, "ping", {})
    connessioni = 0
    respinto = asyncio.Event()
    async def kairos(ws):
        nonlocal connessioni
        connessioni += 1
        assert firma.verifica(pk_m, json.loads(await ws.recv()), firma.Visti())["tipo"] == "ciao"
        await ws.send(json.dumps(firma.firma(sk_k, "ciao_ok", {})))
        await ws.send(json.dumps(replay))
        if connessioni == 1:
            assert json.loads(await ws.recv())["tipo"] == "pong"
            await ws.close()
        else:
            await ws.wait_closed()
            assert ws.close_code == 1008
            respinto.set()
    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(canale.esegui(f"http://127.0.0.1:{porta}", tmp_path))
        try:
            await asyncio.wait_for(respinto.wait(), 5)
            abbinata = False
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert "id gia' visto" in (tmp_path / "comandi.log").read_text()


@pytest.mark.asyncio
async def test_revoca_locale_chiude_socket_senza_frame_nuovi(moduli, monkeypatch, tmp_path):
    canale, firma = moduli
    from collegato import chiavi
    from websockets.asyncio.server import serve
    sk_m, _ = coppia()
    sk_k, pk_k = coppia()
    _, pk_c = coppia()
    monkeypatch.setattr(chiavi, "carica", lambda d=None: (sk_m, pk_k, pk_c, "m"))
    abbinata = True
    monkeypatch.setattr(chiavi, "abbinata", lambda d=None: abbinata)
    salutata = asyncio.Event()
    chiusa = asyncio.Event()
    async def kairos(ws):
        await ws.recv()
        await ws.send(json.dumps(firma.firma(sk_k, "ciao_ok", {})))
        salutata.set()
        await ws.wait_closed()
        assert ws.close_code == 1000
        assert ws.close_reason == "scollega"
        chiusa.set()
    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(canale.esegui(f"http://127.0.0.1:{porta}", tmp_path))
        try:
            await asyncio.wait_for(salutata.wait(), 3)
            abbinata = False
            await asyncio.wait_for(chiusa.wait(), 3)
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
