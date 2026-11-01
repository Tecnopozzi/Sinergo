"""Conservazione chiavi Sinergo: Linux reale e API Windows simulate."""
import os
import stat
import sys
import types

import pytest

from collegato import chiavi


def salva(d):
    chiavi.salva(b'p' * 32, b'k' * 32, 'idm', d, pk_comandi=b'c' * 32,
                 indirizzo='http://127.0.0.1:9200')


def test_chiavi_complete_e_indirizzo(tmp_path):
    d = tmp_path / 'chiavi'
    salva(d)
    assert chiavi.abbinata(d)
    assert chiavi.carica(d) == (b'p' * 32, b'k' * 32, b'c' * 32, 'idm')
    assert chiavi.indirizzo(d) == 'http://127.0.0.1:9200'


@pytest.mark.skipif(sys.platform == 'win32', reason='bit POSIX')
def test_permessi_linux(tmp_path):
    d = tmp_path / 'chiavi'
    d.mkdir(mode=0o755)
    for nome in ('macchina.key', 'kairos.pub', 'comandi.pub', 'id_macchina', 'kairos.url'):
        (d / nome).write_text('vecchio')
        (d / nome).chmod(0o644)
    salva(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in d.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name


def test_abbinata_richiede_tutte_le_chiavi(tmp_path):
    (tmp_path / chiavi.ID).write_text('idm')
    assert not chiavi.abbinata(tmp_path)


def test_scollega_sposta_senza_cancellare_e_senza_sovrascrivere_storico(tmp_path):
    salva(tmp_path)
    prima = chiavi.revoca(tmp_path)
    salva(tmp_path)
    seconda = chiavi.revoca(tmp_path)
    assert len(prima) == len(seconda) == 5
    assert len(set(prima + seconda)) == 10
    assert all(f.name.endswith('.revocato') and f.exists() for f in prima + seconda)
    assert not chiavi.abbinata(tmp_path)


def test_windows_dpapi_e_acl_con_mock(tmp_path, monkeypatch):
    chiamate = []
    monkeypatch.setattr(chiavi, '_windows', lambda: True)
    monkeypatch.setattr(chiavi, '_dpapi', lambda dati, proteggi:
                        b'DPAPI:' + dati[::-1] if proteggi else dati[len(b'DPAPI:'):][::-1])
    monkeypatch.setattr(chiavi, '_acl_windows', lambda percorso, cartella=False:
                        chiamate.append((percorso, cartella)))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert chiavi.cartella() == tmp_path / 'LocalRouter'
    privata = os.urandom(32)
    chiavi.salva(privata, b'k' * 32, 'idm', pk_comandi=b'c' * 32)
    su_disco = (chiavi.cartella() / chiavi.PRIVATA).read_bytes()
    assert privata not in su_disco and su_disco.startswith(b'DPAPI:')
    assert chiavi.carica()[0] == privata
    assert chiamate[0] == (chiavi.cartella(), True)
    assert {percorso.name for percorso, cartella in chiamate if not cartella} == {
        chiavi.PRIVATA, chiavi.PUB_KAIROS, chiavi.PUB_COMANDI, chiavi.ID, chiavi.URL}


def test_dpapi_ctypes_usa_ambito_utente(monkeypatch):
    visti = {}

    def crypt(ingresso, d, e, r, p, flag, uscita):
        import ctypes
        visti['flag'] = flag
        buf = ctypes.create_string_buffer(b'cifrato')
        visti['buf'] = buf
        uscita._obj.cbData = 7
        uscita._obj.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        return 1

    import ctypes
    win = types.SimpleNamespace(crypt32=types.SimpleNamespace(CryptProtectData=crypt, CryptUnprotectData=crypt),
                                kernel32=types.SimpleNamespace(LocalFree=lambda p: 0))
    monkeypatch.setattr(ctypes, 'windll', win, raising=False)
    assert chiavi._dpapi(b'segreto', True) == b'cifrato'
    assert visti['flag'] == 0
