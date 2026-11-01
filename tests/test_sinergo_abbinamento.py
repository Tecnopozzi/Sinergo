"""Abbinamento Sinergo: prove compatibili, scoperta e WebSocket reali su loopback."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import socket
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import serve


def risposta(richiesta, *, cambia=None):
    """Kairos finto: usa solo il codice letto dalla pagina, mai dal frame di rete."""
    pk_k = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    pk_c = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    prova = hmac.new(richiesta.K, b"kairos|" + richiesta.id_richiesta.encode() + b"|" +
                     richiesta.pk + b"|" + pk_k + b"|" + pk_c, hashlib.sha256).hexdigest()
    r = {"tipo": "abbinato", "id_macchina": "macchina-prova", "pk_kairos": pk_k.hex(),
         "pk_comandi": pk_c.hex(), "prova_k": prova}
    if cambia:
        r[cambia] = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    return r


def test_codice_formato_normalizzazione_e_frame():
    from collegato.codice import Richiesta, normalizza_codice

    richiesta = Richiesta()
    assert re.fullmatch(r"[0-9a-f]{32}", richiesta.id_richiesta)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){2}", richiesta.codice)
    assert normalizza_codice(" oilx - oilx\t oilx ") == "011X011X011X"
    assert richiesta.K == hashlib.sha256((richiesta.id_richiesta + "|" +
                                          normalizza_codice(richiesta.codice)).encode()).digest()
    frame = richiesta.frame({"nome": "prova"})
    assert set(frame) == {"tipo", "id_richiesta", "pk_macchina", "scheda", "prova_m"}
    assert frame["tipo"] == "attesa"
    assert frame["pk_macchina"] == richiesta.pk.hex()
    assert frame["prova_m"] == hmac.new(richiesta.K, b"macchina|" + richiesta.id_richiesta.encode() +
                                        b"|" + richiesta.pk, hashlib.sha256).hexdigest()
    assert richiesta.codice not in repr(richiesta)
    assert richiesta.codice not in json.dumps(frame)


@pytest.mark.parametrize("s", ["", "0" * 11, "0" * 13, "U" * 12, "!" * 12])
def test_normalizzazione_rifiuta_codici_invalidi(s):
    from collegato.codice import normalizza_codice

    with pytest.raises(ValueError, match="codice non valido"):
        normalizza_codice(s)


def test_prova_k_verifica_entrambe_chiavi_a_tempo_costante(monkeypatch):
    from collegato import codice

    richiesta = codice.Richiesta()
    attesa = risposta(richiesta)
    chiamate = []
    confronta = hmac.compare_digest
    monkeypatch.setattr(codice.hmac, "compare_digest", lambda a, b: chiamate.append((a, b)) or confronta(a, b))
    assert richiesta.verifica_risposta(attesa) == (bytes.fromhex(attesa["pk_kairos"]),
                                                 bytes.fromhex(attesa["pk_comandi"]), "macchina-prova")
    assert len(chiamate) == 1


@pytest.mark.parametrize("campo", ["pk_kairos", "pk_comandi"])
def test_prova_k_rifiuta_sostituzione_chiavi(campo):
    from collegato.codice import AbbinamentoFallito, Richiesta

    richiesta = Richiesta()
    with pytest.raises(AbbinamentoFallito, match="prova di Kairos"):
        richiesta.verifica_risposta(risposta(richiesta, cambia=campo))


@pytest.mark.parametrize("campo,valore", [("pk_kairos", "00"), ("pk_comandi", "zz"),
                                         ("id_macchina", ""), ("prova_k", None)])
def test_risposta_malformata_rifiutata(campo, valore):
    from collegato.codice import AbbinamentoFallito, Richiesta

    richiesta = Richiesta()
    msg = risposta(richiesta)
    msg[campo] = valore
    with pytest.raises(AbbinamentoFallito):
        richiesta.verifica_risposta(msg)


@pytest.mark.asyncio
async def test_attesa_salva_solo_dopo_prova_verificata(tmp_path, caplog):
    from collegato import attesa, chiavi

    stati = []
    richiesta_corrente = None

    def aggiorna(stato, richiesta):
        nonlocal richiesta_corrente
        richiesta_corrente = richiesta
        stati.append(stato)

    async def kairos(ws):
        assert ws.request.path == "/macchine/attesa"
        frame = json.loads(await ws.recv())
        assert frame == richiesta_corrente.frame({"nome": "prova"})
        await ws.send(json.dumps({"tipo": "in_attesa"}))
        await ws.send(json.dumps({"tipo": "codice_errato", "restano": 4}))
        await ws.send(json.dumps(risposta(richiesta_corrente)))

    caplog.set_level(logging.DEBUG)
    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        id_m = await asyncio.wait_for(attesa.abbina(f"http://127.0.0.1:{porta}", {"nome": "prova"},
                                                   d=tmp_path / "chiavi", aggiorna=aggiorna), 3)
    assert id_m == "macchina-prova"
    assert chiavi.abbinata(tmp_path / "chiavi")
    assert chiavi.carica(tmp_path / "chiavi")[0] == richiesta_corrente.privata
    assert "in_attesa" in stati and "codice_errato" in stati and stati[-1] == "abbinato"
    assert richiesta_corrente.codice not in caplog.text
    assert richiesta_corrente.codice.replace("-", "") not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("campo", ["pk_kairos", "pk_comandi"])
async def test_attesa_chiavi_sostituite_non_salvate(tmp_path, campo):
    from collegato import attesa, chiavi
    from collegato.codice import AbbinamentoFallito

    richieste = []

    async def kairos(ws):
        await ws.recv()
        await ws.send(json.dumps(risposta(richieste[-1], cambia=campo)))

    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        with pytest.raises(AbbinamentoFallito):
            await asyncio.wait_for(attesa.abbina(f"http://127.0.0.1:{porta}", {}, d=tmp_path / "chiavi",
                                                 aggiorna=lambda stato, r: richieste.append(r)), 3)
    assert not chiavi.abbinata(tmp_path / "chiavi")
    assert not (tmp_path / "chiavi").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("evento", ["bloccato", "scaduto", "chiuso"])
async def test_attesa_riparte_con_codice_nuovo(tmp_path, evento):
    from collegato import attesa

    correnti = []
    frame = []

    def aggiorna(stato, richiesta):
        if not correnti or correnti[-1] is not richiesta:
            correnti.append(richiesta)

    async def kairos(ws):
        frame.append(json.loads(await ws.recv()))
        if len(frame) == 1:
            if evento != "chiuso":
                await ws.send(json.dumps({"tipo": evento}))
            await ws.close()
        else:
            await ws.send(json.dumps(risposta(correnti[-1])))

    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(attesa.abbina(f"http://127.0.0.1:{porta}", {}, d=tmp_path / "chiavi",
                                             aggiorna=aggiorna), 4)
    assert len(frame) == 2
    assert frame[0]["id_richiesta"] != frame[1]["id_richiesta"]
    assert correnti[0].codice != correnti[1].codice
    assert frame[0]["pk_macchina"] != frame[1]["pk_macchina"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://8.8.8.8", "ws://1.1.1.1", "http://0.0.0.0", "http://[::]"])
async def test_http_pubblico_rifiutato(url):
    from collegato.attesa import url_websocket
    from collegato.codice import AbbinamentoFallito

    with pytest.raises(AbbinamentoFallito, match="TLS"):
        await url_websocket(url, "/macchine/attesa")


@pytest.mark.asyncio
async def test_dns_con_un_indirizzo_pubblico_rifiutato(monkeypatch):
    from collegato.attesa import url_websocket
    from collegato.codice import AbbinamentoFallito

    async def risolvi(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.3", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", risolvi)
    with pytest.raises(AbbinamentoFallito, match="TLS"):
        await url_websocket("http://kairos.local", "/macchine/attesa")


@pytest.mark.asyncio
async def test_url_tls_e_lan():
    from collegato.attesa import url_websocket

    assert await url_websocket("http://127.0.0.1:9200/", "/macchine/attesa") == "ws://127.0.0.1:9200/macchine/attesa"
    assert await url_websocket("https://8.8.8.8", "/macchine/canale") == "wss://8.8.8.8/macchine/canale"


@pytest.mark.asyncio
async def test_redirect_non_invia_prove_ad_altro_endpoint(tmp_path):
    from collegato import attesa

    visti = []

    async def altrove(ws):
        visti.append(await ws.recv())

    from websockets.http11 import Response
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus

    async with serve(altrove, "127.0.0.1", 0) as finale:
        porta_finale = finale.sockets[0].getsockname()[1]

        def redirect(conn, request):
            return Response(302, "Found", Headers({"Location": f"ws://127.0.0.1:{porta_finale}/rubato"}))

        async with serve(altrove, "127.0.0.1", 0, process_request=redirect) as iniziale:
            porta = iniziale.sockets[0].getsockname()[1]
            with pytest.raises(InvalidStatus):
                async with attesa.connetti(f"http://127.0.0.1:{porta}", "/macchine/attesa"):
                    pass
    assert visti == []


@pytest.mark.asyncio
async def test_scoperta_zeroconf_finto(monkeypatch):
    from collegato import trova

    eventi = []
    info = types.SimpleNamespace(port=9200, properties={b"nome": "Kairos casa".encode(), b"percorso": b"/"},
                                 parsed_scoped_addresses=lambda: ["192.168.1.2"])

    class Zeroconf:
        def get_service_info(self, tipo, nome, **kwargs):
            eventi.append((tipo, nome))
            return info

        def close(self):
            eventi.append("chiuso")

    class Browser:
        def __init__(self, zc, tipo, listener):
            listener.add_service(zc, tipo, "Kairos._kairos._tcp.local.")
            listener.add_service(zc, tipo, "Kairos._kairos._tcp.local.")

        def cancel(self):
            eventi.append("annullato")

    monkeypatch.setattr(trova, "Zeroconf", Zeroconf)
    monkeypatch.setattr(trova, "ServiceBrowser", Browser)
    assert await trova.cerca(durata=0) == [{"nome": "Kairos casa", "indirizzo": "http://192.168.1.2:9200/"}]
    assert eventi[0] == ("_kairos._tcp.local.", "Kairos._kairos._tcp.local.")
    assert eventi[-2:] == ["annullato", "chiuso"]


@pytest.mark.asyncio
async def test_attesa_riusa_codice_gia_mostrato_nella_pagina(tmp_path):
    from collegato import attesa
    from collegato.codice import Richiesta

    iniziale = Richiesta()
    richieste = []

    async def kairos(ws):
        assert json.loads(await ws.recv())["id_richiesta"] == iniziale.id_richiesta
        await ws.send(json.dumps(risposta(iniziale)))

    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(attesa.abbina(f"http://127.0.0.1:{porta}", {}, d=tmp_path / "chiavi",
                                             richiesta=iniziale, aggiorna=lambda stato, r: richieste.append(r)), 3)
    assert all(r is iniziale for r in richieste)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://127.0.0.1", "http://u:p@127.0.0.1", "http://127.0.0.1:0",
                                 "http://127.0.0.1?x=y", "http://127.0.0.1#segreto"])
async def test_url_ambiguo_rifiutato(url):
    from collegato.attesa import url_websocket
    from collegato.codice import AbbinamentoFallito

    with pytest.raises(AbbinamentoFallito, match="non valido"):
        await url_websocket(url, "/macchine/attesa")


@pytest.mark.asyncio
async def test_connessione_usa_ip_verificato_e_conserva_host(monkeypatch):
    from collegato import attesa

    risoluzioni = []
    richieste_host = []

    async def kairos(ws):
        richieste_host.append(ws.request.headers["Host"])
        await ws.send("ok")

    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]

        async def risolvi(host, port, **kwargs):
            risoluzioni.append(host)
            # Una seconda risoluzione cambia peer; l'alias loopback evita traffico esterno anche nei mutanti.
            ip = "127.0.0.1" if len(risoluzioni) == 1 else "127.0.0.2"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", risolvi)
        async with attesa.connetti(f"http://kairos.prova:{porta}", "/macchine/attesa") as ws:
            assert await ws.recv() == "ok"
    assert risoluzioni == ["kairos.prova"]
    assert richieste_host == [f"kairos.prova:{porta}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("eccezione", [OSError("rete non disponibile"), "zeroconf"])
async def test_scoperta_multicast_non_disponibile_consente_indirizzo_manuale(monkeypatch, eccezione):
    from collegato import trova
    from zeroconf import Error

    errore = Error("multicast non disponibile") if eccezione == "zeroconf" else eccezione

    def non_disponibile():
        raise errore

    monkeypatch.setattr(trova, "Zeroconf", non_disponibile)
    assert await trova.cerca(durata=0) == []


@pytest.mark.asyncio
async def test_scoperta_finestra_predefinita_dieci_secondi(monkeypatch):
    from collegato import trova

    durate = []
    eventi = []

    class Zeroconf:
        def close(self):
            eventi.append("chiuso")

    class Browser:
        def __init__(self, zc, tipo, listener):
            eventi.append(tipo)

        def cancel(self):
            eventi.append("annullato")

    monkeypatch.setattr(trova, "Zeroconf", Zeroconf)
    monkeypatch.setattr(trova, "ServiceBrowser", Browser)
    monkeypatch.setattr(trova.time, "sleep", durate.append)
    assert await trova.cerca() == []
    assert durate == [10]
    assert eventi == ["_kairos._tcp.local.", "annullato", "chiuso"]


@pytest.mark.asyncio
async def test_scoperta_non_nasconde_errori_di_programmazione(monkeypatch):
    from collegato import trova

    def errore():
        raise RuntimeError("errore interno")

    monkeypatch.setattr(trova, "Zeroconf", errore)
    with pytest.raises(RuntimeError, match="errore interno"):
        await trova.cerca(durata=0)


@pytest.mark.asyncio
async def test_attesa_json_troppo_annidato_rifiutato(tmp_path, monkeypatch):
    from collegato import attesa
    from collegato.codice import AbbinamentoFallito

    # Il decoder C tollera profondita' diverse fra versioni Python; quello Python
    # della stessa libreria standard rende riproducibile il limite di ricorsione.
    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    monkeypatch.setattr(attesa.json, "loads", decoder.decode)

    async def kairos(ws):
        await ws.recv()
        await ws.send("[" * 5000 + "]" * 5000)

    async with serve(kairos, "127.0.0.1", 0) as server:
        porta = server.sockets[0].getsockname()[1]
        with pytest.raises(AbbinamentoFallito, match="malformato"):
            await asyncio.wait_for(attesa.abbina(f"http://127.0.0.1:{porta}", {}, d=tmp_path / "chiavi"), 3)
    assert not (tmp_path / "chiavi").exists()
