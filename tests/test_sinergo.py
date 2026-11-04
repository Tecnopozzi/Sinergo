"""Contratto completo Sinergo contro Kairos WebSocket locale indipendente."""
import asyncio
import hashlib
import hmac
import json
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import serve

from collegato import chiavi


def risposta_abbinato(frame, codice, sk_k, sk_c):
    pk_m = bytes.fromhex(frame['pk_macchina'])
    k = hashlib.sha256((frame['id_richiesta'] + '|' + codice.replace('-', '')).encode()).digest()
    attesa = hmac.new(k, b'macchina|' + frame['id_richiesta'].encode() + b'|' + pk_m,
                     hashlib.sha256).hexdigest()
    assert hmac.compare_digest(frame['prova_m'], attesa)
    pk_k, pk_c = sk_k.public_key().public_bytes_raw(), sk_c.public_key().public_bytes_raw()
    prova = hmac.new(k, b'kairos|' + frame['id_richiesta'].encode() + b'|' + pk_m + b'|' + pk_k + b'|' + pk_c,
                     hashlib.sha256).hexdigest()
    return {'tipo': 'abbinato', 'id_macchina': 'pc-prova', 'pk_kairos': pk_k.hex(),
            'pk_comandi': pk_c.hex(), 'prova_k': prova}


def firma_indipendente(sk, tipo, corpo):
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    ora = datetime.now(timezone.utc)
    msg = {'id': str(uuid4()), 'emesso': ora.isoformat(), 'scade': (ora + timedelta(seconds=120)).isoformat(),
           'tipo': tipo, 'corpo': corpo}
    dati = json.dumps(msg, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return {**msg, 'firma': sk.sign(dati).hex()}


@pytest.mark.asyncio
async def test_abbinamento_completo_e_canale(tmp_path, caplog):
    from collegato import attesa, canale
    caplog.set_level(logging.DEBUG)
    sk_k, sk_c = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    richieste, stati, frame_visti = [], [], []
    completato = asyncio.Event()

    def aggiorna(stato, richiesta):
        stati.append(stato)
        richieste.append(richiesta)

    async def kairos(ws):
        frame = json.loads(await ws.recv())
        frame_visti.append(frame)
        if ws.request.path == '/macchine/attesa':
            assert set(frame) == {'tipo', 'id_richiesta', 'pk_macchina', 'scheda', 'prova_m'}
            assert frame['tipo'] == 'attesa'
            await ws.send(json.dumps({'tipo': 'in_attesa'}))
            await ws.send(json.dumps(risposta_abbinato(frame, richieste[-1].codice, sk_k, sk_c)))
        else:
            assert ws.request.path == '/macchine/canale'
            pk_m = Ed25519PrivateKey.from_private_bytes(chiavi.carica(tmp_path)[0]).public_key()
            sig = bytes.fromhex(frame.pop('firma'))
            pk_m.verify(sig, json.dumps(frame, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
            assert frame['tipo'] == 'ciao' and frame['corpo'] == {'id_macchina': 'pc-prova'}
            await ws.send(json.dumps(firma_indipendente(sk_k, 'ciao_ok', {})))
            await ws.send(json.dumps(firma_indipendente(sk_k, 'ping', {})))
            pong = json.loads(await ws.recv())
            assert pong['tipo'] == 'pong'
            completato.set()
            # La revoca locale deve chiudere anche il canale gia' aperto.
            chiavi.revoca(tmp_path)
            await asyncio.wait_for(ws.wait_closed(), 4)

    async with serve(kairos, '127.0.0.1', 0) as server:
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        await asyncio.wait_for(attesa.abbina(url, {'nome': 'test'}, d=tmp_path, aggiorna=aggiorna), 4)
        assert chiavi.abbinata(tmp_path)
        assert chiavi.carica(tmp_path)[1:3] == (sk_k.public_key().public_bytes_raw(), sk_c.public_key().public_bytes_raw())
        assert chiavi.indirizzo(tmp_path) == url
        await asyncio.wait_for(canale.esegui(url, d=tmp_path), 6)
        assert completato.is_set()
    for richiesta in richieste:
        assert richiesta.codice not in caplog.text
        assert richiesta.codice.replace('-', '') not in caplog.text
    assert 'in_attesa' in stati
    assert len(frame_visti) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('sostituita', ['pk_kairos', 'pk_comandi'])
async def test_kairos_sostituisce_chiave_nessun_salvataggio(tmp_path, sostituita):
    from collegato import attesa
    from collegato.codice import AbbinamentoFallito
    sk_k, sk_c = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    richieste = []

    async def kairos(ws):
        frame = json.loads(await ws.recv())
        risposta = risposta_abbinato(frame, richieste[-1].codice, sk_k, sk_c)
        risposta[sostituita] = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        await ws.send(json.dumps(risposta))

    async with serve(kairos, '127.0.0.1', 0) as server:
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        with pytest.raises(AbbinamentoFallito):
            await asyncio.wait_for(attesa.abbina(url, {}, d=tmp_path,
                                   aggiorna=lambda stato, richiesta: richieste.append(richiesta)), 4)
    assert not chiavi.abbinata(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('motivo', ['bloccato', 'scaduto', 'chiuso'])
async def test_nuova_attesa_dopo_blocco_scadenza_o_chiusura(tmp_path, motivo):
    from collegato import attesa
    sk_k, sk_c = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    richieste, frames = [], []

    async def kairos(ws):
        frame = json.loads(await ws.recv())
        frames.append(frame)
        if len(frames) == 1:
            if motivo != 'chiuso':
                await ws.send(json.dumps({'tipo': motivo}))
        else:
            await ws.send(json.dumps(risposta_abbinato(frame, richieste[-1].codice, sk_k, sk_c)))

    async with serve(kairos, '127.0.0.1', 0) as server:
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        await asyncio.wait_for(attesa.abbina(url, {}, d=tmp_path,
                               aggiorna=lambda stato, richiesta: richieste.append(richiesta)), 6)
    assert len(frames) == 2
    assert frames[0]['id_richiesta'] != frames[1]['id_richiesta']
    codici = {r.codice for r in richieste}
    assert len(codici) == 2
    assert chiavi.abbinata(tmp_path)


def test_cli_vecchio_abbina_rifiutato(capsys):
    from collegato import __main__ as cli
    assert cli.main(['abbina', 'segreto-che-non-va-stampato']) == 2
    assert 'segreto-che-non-va-stampato' not in capsys.readouterr().err


def test_cli_abbinata_riapre_canale(tmp_path, monkeypatch):
    from collegato import __main__ as cli
    chiavi.salva(b'p'*32, b'k'*32, 'idm', tmp_path, pk_comandi=b'c'*32, indirizzo='http://127.0.0.1:9200')
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    chiamate = []

    async def esegui(indirizzo, d=None):
        chiamate.append((indirizzo, d))

    monkeypatch.setattr(cli.canale, 'esegui', esegui)
    assert cli.main([]) == 0
    assert chiamate == [('http://127.0.0.1:9200', tmp_path)]


def test_cli_scollega(tmp_path, monkeypatch, capsys):
    from collegato import __main__ as cli
    chiavi.salva(b'p'*32, b'k'*32, 'idm', tmp_path, pk_comandi=b'c'*32)
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    assert cli.main(['scollega']) == 0
    assert not chiavi.abbinata(tmp_path)
    assert 'Sinergo' in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize('trovati', [[], [{'nome': 'Kairos casa', 'indirizzo': 'http://127.0.0.1:9200'}],
    [{'nome': 'Kairos uno', 'indirizzo': 'http://127.0.0.1:9200'},
     {'nome': 'Kairos due', 'indirizzo': 'http://127.0.0.1:9201'}]])
async def test_avvio_pagina_ricerca_scelta_e_canale(tmp_path, monkeypatch, trovati):
    from collegato import __main__ as cli
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    stati = []
    visti = []
    abbinamenti = []

    async def cerca(durata=10):
        assert durata == 10
        return trovati

    async def servi(stato, sock):
        stati.append(stato)
        if len(trovati) != 1:
            # Simula la scelta effettuata nella pagina dopo la ricerca.
            while not stato.ricerca_finita:
                await asyncio.sleep(0)
            stato.indirizzo = trovati[-1]['indirizzo'] if trovati else 'https://kairos.esempio.it'
            stato.indirizzo_scelto.set()
        await asyncio.Event().wait()

    async def abbina(indirizzo, scheda, *, d, aggiorna, richiesta=None):
        assert richiesta is not None
        assert stati[0].codice == richiesta.codice
        abbinamenti.append(indirizzo)
        aggiorna('abbinato', richiesta)
        chiavi.salva(richiesta.privata, b'k'*32, 'idm', d, pk_comandi=b'c'*32, indirizzo=indirizzo)

    async def esegui(indirizzo, d=None):
        visti.append((indirizzo, d))

    monkeypatch.setattr(cli.trova, 'cerca', cerca)
    monkeypatch.setattr(cli.pagina, 'servi', servi)
    monkeypatch.setattr(cli.attesa, 'abbina', abbina)
    monkeypatch.setattr(cli.canale, 'esegui', esegui)
    monkeypatch.setattr(cli, 'scheda', lambda: {'nome': 'prova'})
    browser = []
    monkeypatch.setattr(cli.webbrowser, 'open', lambda url: browser.append(url))
    await asyncio.wait_for(cli.avvia(), 3)
    assert len(browser) == 1 and browser[0].startswith('http://127.0.0.1:')
    assert len(abbinamenti) == 1
    assert visti == [(abbinamenti[0], tmp_path)]
    assert stati[0].stato == 'abbinato'
    assert not stati[0].codice


@pytest.mark.asyncio
async def test_indirizzo_cambiato_nella_pagina_interrompe_attesa_precedente(tmp_path, monkeypatch):
    from collegato import __main__ as cli
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    attesa_avviata = asyncio.Event()
    chiamate, annullate, codici = [], [], []
    nuovo = 'https://kairos.nuovo.it'

    async def cerca(durata=10):
        return [{'nome': 'Vecchio', 'indirizzo': 'http://127.0.0.1:9200'}]

    async def servi(stato, sock):
        await attesa_avviata.wait()
        stato.indirizzo = nuovo
        stato.indirizzo_scelto.set()
        await asyncio.Event().wait()

    async def abbina(indirizzo, scheda, *, d, aggiorna, richiesta):
        chiamate.append(indirizzo)
        codici.append(richiesta.codice)
        if indirizzo != nuovo:
            aggiorna('errore', richiesta)
            attesa_avviata.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                annullate.append(indirizzo)
                raise
        else:
            chiavi.salva(richiesta.privata, b'k'*32, 'idm', d, pk_comandi=b'c'*32, indirizzo=indirizzo)
            aggiorna('abbinato', richiesta)

    collegata = []
    async def canale(indirizzo, d=None):
        collegata.append(indirizzo)

    monkeypatch.setattr(cli.trova, 'cerca', cerca)
    monkeypatch.setattr(cli.pagina, 'servi', servi)
    monkeypatch.setattr(cli.attesa, 'abbina', abbina)
    monkeypatch.setattr(cli.canale, 'esegui', canale)
    monkeypatch.setattr(cli, 'scheda', lambda: {})
    monkeypatch.setattr(cli.webbrowser, 'open', lambda url: None)
    await asyncio.wait_for(cli.avvia(), 1)
    assert annullate == ['http://127.0.0.1:9200']
    assert chiamate == ['http://127.0.0.1:9200', nuovo]
    assert codici[0] != codici[1]
    assert collegata == [nuovo]


@pytest.mark.asyncio
async def test_avvia_abbinata_riprova_dopo_oserror(tmp_path, monkeypatch, caplog):
    """Macchina già abbinata: OSError da esegui → log + backoff, poi riparte."""
    from collegato import __main__ as cli
    chiavi.salva(b'p' * 32, b'k' * 32, 'idm', tmp_path, pk_comandi=b'c' * 32,
                 indirizzo='http://127.0.0.1:9200')
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    chiamate = []
    dormite = []

    async def esegui(indirizzo, d=None):
        chiamate.append((indirizzo, d))
        if len(chiamate) == 1:
            raise OSError('collegamento caduto')

    async def dormi(secondi):
        dormite.append(secondi)

    monkeypatch.setattr(cli.canale, 'esegui', esegui)
    monkeypatch.setattr(cli.asyncio, 'sleep', dormi)
    caplog.set_level(logging.ERROR)
    await asyncio.wait_for(cli.avvia(), 2)
    assert chiamate == [('http://127.0.0.1:9200', tmp_path),
                        ('http://127.0.0.1:9200', tmp_path)]
    assert dormite == [1]
    assert 'collegamento caduto' in caplog.text


@pytest.mark.asyncio
async def test_avvia_abbinata_esce_dopo_revoca(tmp_path, monkeypatch):
    """Revoca durante l'errore: niente riprova infinita, avvia termina."""
    from collegato import __main__ as cli
    chiavi.salva(b'p' * 32, b'k' * 32, 'idm', tmp_path, pk_comandi=b'c' * 32,
                 indirizzo='http://127.0.0.1:9200')
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)
    chiamate = []
    dormite = []

    async def esegui(indirizzo, d=None):
        chiamate.append(1)
        chiavi.revoca(tmp_path)
        raise OSError('revocato')

    async def dormi(secondi):
        dormite.append(secondi)

    monkeypatch.setattr(cli.canale, 'esegui', esegui)
    monkeypatch.setattr(cli.asyncio, 'sleep', dormi)
    await asyncio.wait_for(cli.avvia(), 2)
    assert chiamate == [1]
    assert dormite == []
    assert not chiavi.abbinata(tmp_path)


def test_main_oserror_registrata_nel_log(tmp_path, monkeypatch, caplog):
    """Uscita da main: traceback in log, non solo la riga su stderr."""
    from collegato import __main__ as cli
    monkeypatch.setattr(chiavi, 'cartella', lambda: tmp_path)

    async def avvia_boom(d=None):
        raise OSError('silenzio mortale')

    monkeypatch.setattr(cli, 'avvia', avvia_boom)
    caplog.set_level(logging.ERROR)
    assert cli.main([]) == 1
    assert 'silenzio mortale' in caplog.text
    assert 'Traceback' in caplog.text
