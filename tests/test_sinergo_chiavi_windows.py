"""DACL Windows simulate su Linux: sostituzione completa e nessun dato scritto se fallisce."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from collegato import chiavi


class Funzione:
    """Una funzione WinDLL sostituibile, con gli attributi delle firme ctypes."""

    def __init__(self, funzione):
        self.funzione = funzione

    def __call__(self, *argomenti):
        return self.funzione(*argomenti)


@pytest.fixture
def windows(monkeypatch):
    stato = SimpleNamespace(acl={}, chiamate=[], sddl={}, chiusi=[], liberati=[], errore=None,
                            sid="S-1-5-21-123-456-789-1001", buffer=[])

    def apri_token(processo, accesso, token):
        assert accesso == 8  # TOKEN_QUERY, senza privilegi aggiuntivi.
        token._obj.value = 51
        return stato.errore != "token"

    def informazioni(token, classe, buffer, dimensione, necessari):
        assert classe == 1  # TokenUser.
        necessari._obj.value = 32
        if buffer is None:
            return 0
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 71
        return stato.errore != "informazioni"

    def sid_testo(sid, uscita):
        assert sid == 71
        buffer = ctypes.create_unicode_buffer(stato.sid)
        stato.buffer.append(buffer)
        uscita._obj.value = ctypes.addressof(buffer)
        return stato.errore != "sid"

    def converti(sddl, revisione, uscita, dimensione):
        assert revisione == 1
        puntatore = 100 + len(stato.sddl)
        stato.sddl[puntatore] = sddl
        uscita._obj.value = puntatore
        return stato.errore != "descrittore"

    def dacl(descrittore, presente, acl, predefinita):
        presente._obj.value = stato.errore != "dacl_assente"
        acl._obj.value = descrittore.value
        return stato.errore != "dacl"

    def imposta(percorso, tipo, informazioni, proprietario, gruppo, acl, sacl):
        stato.chiamate.append((percorso, tipo, informazioni))
        if stato.errore == "imposta":
            return 5
        # Sostituisce tutte le ACE, anziche' aggiungerne una alla vecchia DACL.
        stato.acl[percorso] = stato.sddl[acl.value]
        return 0

    advapi = SimpleNamespace(**{nome: Funzione(f) for nome, f in {
        "OpenProcessToken": apri_token, "GetTokenInformation": informazioni,
        "ConvertSidToStringSidW": sid_testo,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": converti,
        "GetSecurityDescriptorDacl": dacl, "SetNamedSecurityInfoW": imposta,
    }.items()})
    kernel = SimpleNamespace(**{nome: Funzione(f) for nome, f in {
        "GetCurrentProcess": lambda: 31,
        "CloseHandle": lambda p: stato.chiusi.append(p.value) or 1,
        "LocalFree": lambda p: stato.liberati.append(p.value) or 0,
    }.items()})
    monkeypatch.setattr(ctypes, "WinDLL", lambda nome, **kw: advapi if nome == "advapi32" else kernel,
                        raising=False)
    monkeypatch.setattr(chiavi, "_windows", lambda: True)
    # Nessun eseguibile esterno deve partecipare alla protezione delle chiavi.
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("comando esterno inatteso"))
    return stato


def test_cartella_windows_sostituisce_anche_acl_esplicite_precedenti(tmp_path, windows):
    windows.acl[str(tmp_path)] = "D:(A;;FA;;;S-1-1-0)(A;;FA;;;S-1-5-32-545)"
    chiavi._riserva(tmp_path)
    assert windows.acl[str(tmp_path)] == f"D:P(A;OICI;FA;;;{windows.sid})"
    assert windows.chiamate == [(str(tmp_path), 1, 0x80000004)]
    assert windows.chiusi == [51]
    assert len(windows.liberati) == 2


def test_file_windows_precedente_ha_solo_utente_prima_della_scrittura(tmp_path, windows):
    file = tmp_path / "kairos.pub"
    file.write_bytes(b"precedente")
    windows.acl[str(file)] = "D:(A;;FA;;;S-1-1-0)"
    chiavi._scrivi(file, b"nuovo")
    assert windows.acl[str(file)] == f"D:P(A;;FA;;;{windows.sid})"
    assert windows.chiamate == [(str(file), 1, 0x80000004)]
    assert file.read_bytes() == b"nuovo"


@pytest.mark.parametrize("errore", ["token", "informazioni", "sid", "descrittore", "dacl", "dacl_assente", "imposta"])
def test_errore_api_windows_non_tronca_file_esistente(tmp_path, windows, errore):
    file = tmp_path / "macchina.key"
    file.write_bytes(b"precedente")
    windows.errore = errore
    with pytest.raises(OSError):
        chiavi._scrivi(file, b"nuovo-segreto")
    assert file.read_bytes() == b"precedente"


def test_errore_acl_cartella_non_scrive_chiavi(tmp_path, windows, monkeypatch):
    windows.errore = "imposta"
    monkeypatch.setattr(chiavi, "_dpapi", lambda *args: pytest.fail("DPAPI non deve ancora essere chiamata"))
    with pytest.raises(OSError):
        chiavi.salva(b"privata", b"pubblica", "id", tmp_path, pk_comandi=b"comandi")
    assert not (tmp_path / chiavi.PRIVATA).exists()
    assert not (tmp_path / chiavi.ID).exists()


def test_tutti_i_file_chiave_windows_ricevono_dacl_protetta(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(chiavi, "_dpapi", lambda dati, proteggi: b"DPAPI:" + dati)
    chiavi.salva(b"privata", b"pubblica", "id", tmp_path, pk_comandi=b"comandi", indirizzo="https://kairos.it")
    for nome in (chiavi.PRIVATA, chiavi.PUB_KAIROS, chiavi.PUB_COMANDI, chiavi.ID, chiavi.URL):
        assert windows.acl[str(tmp_path / nome)] == f"D:P(A;;FA;;;{windows.sid})"
