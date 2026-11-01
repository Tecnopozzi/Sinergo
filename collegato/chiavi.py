"""Dove stanno le chiavi sulla macchina (§1.6). Linux: ~/.config/localrouter/ 0700/0600.
Windows: %LOCALAPPDATA%\\LocalRouter\\, privata cifrata con DPAPI (ambito utente), ACL al solo utente.
Mai nei log. Il `scollega` sposta in `*.revocato` datati, non cancella."""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

PRIVATA, PUB_KAIROS, PUB_COMANDI, ID, URL = "macchina.key", "kairos.pub", "comandi.pub", "id_macchina", "kairos.url"


def _windows() -> bool:
    return sys.platform == "win32"


def cartella() -> Path:
    if _windows():
        return Path(os.environ["LOCALAPPDATA"]) / "LocalRouter"
    return Path.home() / ".config" / "localrouter"


def _dpapi(dati: bytes, proteggi: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(dati, len(dati))
    ingresso = Blob(len(dati), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    uscita = Blob()
    f = ctypes.windll.crypt32.CryptProtectData if proteggi else ctypes.windll.crypt32.CryptUnprotectData
    # flag 0 = ambito utente (CRYPTPROTECT_LOCAL_MACHINE non e' impostato)
    if not f(ctypes.byref(ingresso), None, None, None, None, 0, ctypes.byref(uscita)):
        raise OSError("DPAPI non riuscita")
    try:
        return ctypes.string_at(uscita.pbData, uscita.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(uscita.pbData)


def _acl_windows(percorso: Path, cartella: bool = False) -> None:
    """DACL protetta con una sola ACE: utente del processo, anche su file gia' esistenti."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    puntatore = ctypes.c_void_p
    pp = ctypes.POINTER(puntatore)
    pd = ctypes.POINTER(wintypes.DWORD)
    pb = ctypes.POINTER(wintypes.BOOL)
    # Le firme esplicite evitano il troncamento dei puntatori su Windows a 64 bit.
    for libreria, nome, ritorno, argomenti in (
        (kernel, "GetCurrentProcess", wintypes.HANDLE, []),
        (kernel, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
        (kernel, "LocalFree", puntatore, [puntatore]),
        (advapi, "OpenProcessToken", wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, pp]),
        (advapi, "GetTokenInformation", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, puntatore, wintypes.DWORD, pd]),
        (advapi, "ConvertSidToStringSidW", wintypes.BOOL, [puntatore, pp]),
        (advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
         [wintypes.LPCWSTR, wintypes.DWORD, pp, pd]),
        (advapi, "GetSecurityDescriptorDacl", wintypes.BOOL, [puntatore, pb, pp, pb]),
        (advapi, "SetNamedSecurityInfoW", wintypes.DWORD,
         [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, puntatore, puntatore, puntatore, puntatore]),
    ):
        funzione = getattr(libreria, nome)
        funzione.restype, funzione.argtypes = ritorno, argomenti

    token, sid_testo, descrittore = puntatore(), puntatore(), puntatore()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):  # TOKEN_QUERY
            raise OSError("Impossibile leggere l'utente per proteggere le chiavi")
        lunghezza = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(lunghezza))  # TokenUser
        if not lunghezza.value:
            raise OSError("Impossibile leggere il SID dell'utente")
        dati = ctypes.create_string_buffer(lunghezza.value)
        if not advapi.GetTokenInformation(token, 1, dati, lunghezza, ctypes.byref(lunghezza)):
            raise OSError("Impossibile leggere il SID dell'utente")
        # TOKEN_USER comincia con SID_AND_ATTRIBUTES, il cui primo membro e' il puntatore SID.
        sid = ctypes.cast(dati, pp)[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_testo)):
            raise OSError("Impossibile convertire il SID dell'utente")
        eredita = "OICI" if cartella else ""
        sddl = f"D:P(A;{eredita};FA;;;{ctypes.wstring_at(sid_testo.value)})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descrittore), None):
            raise OSError("Impossibile costruire la protezione delle chiavi")
        presente, predefinita, acl = wintypes.BOOL(), wintypes.BOOL(), puntatore()
        if (not advapi.GetSecurityDescriptorDacl(descrittore, ctypes.byref(presente), ctypes.byref(acl),
                                               ctypes.byref(predefinita)) or not presente.value or not acl.value):
            raise OSError("DACL delle chiavi assente: scrittura rifiutata")
        # SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        # Sostituisce tutte le ACE, incluse quelle esplicite: aggiungere grant:r non basta.
        errore = advapi.SetNamedSecurityInfoW(str(percorso), 1, 0x80000004, None, None, acl, None)
        if errore:
            raise OSError(errore, "Impossibile applicare la protezione delle chiavi")
    finally:
        if descrittore.value:
            kernel.LocalFree(descrittore)
        if sid_testo.value:
            kernel.LocalFree(sid_testo)
        if token.value:
            kernel.CloseHandle(token)


def _riserva(d: Path) -> None:
    if not _windows():
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
        return
    d.mkdir(parents=True, exist_ok=True)
    # Windows non applica i bit POSIX: sostituiamo la DACL, oltre alla cifratura DPAPI.
    _acl_windows(d, cartella=True)


def _scrivi(f: Path, dati: bytes) -> None:
    # Su Windows il file puo' avere ACE esplicite anche se la cartella e' gia' protetta.
    # Si apre senza troncare: prima si applica la DACL, poi si scrive. Su Linux basta chmod.
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | (0 if _windows() else os.O_TRUNC), 0o600)
    with os.fdopen(fd, "wb") as fh:
        if _windows():
            _acl_windows(f)
            fh.truncate(0)
        fh.write(dati)
    if not _windows():
        f.chmod(0o600)


def abbinata(d: Path | None = None) -> bool:
    return all(((d or cartella()) / nome).is_file() for nome in (ID, PRIVATA, PUB_KAIROS, PUB_COMANDI))


def salva(privata: bytes, pk_kairos: bytes, id_macchina: str, d: Path | None = None, *,
          pk_comandi: bytes, indirizzo: str = "") -> None:
    """Chiamare solo dopo `verifica_risposta`."""
    d = d or cartella()
    _riserva(d)
    _scrivi(d / PRIVATA, _dpapi(privata, True) if _windows() else privata)
    _scrivi(d / PUB_KAIROS, pk_kairos)
    _scrivi(d / PUB_COMANDI, pk_comandi)
    _scrivi(d / URL, indirizzo.encode())
    # Il marcatore si scrive per ultimo: un salvataggio incompleto non e' un abbinamento.
    _scrivi(d / ID, id_macchina.encode())


def carica(d: Path | None = None) -> tuple[bytes, bytes, bytes, str]:
    d = d or cartella()
    p = (d / PRIVATA).read_bytes()
    return ((_dpapi(p, False) if _windows() else p), (d / PUB_KAIROS).read_bytes(),
            (d / PUB_COMANDI).read_bytes(), (d / ID).read_text())


def indirizzo(d: Path | None = None) -> str:
    return ((d or cartella()) / URL).read_text()


def revoca(d: Path | None = None) -> list[Path]:
    """Sposta chiavi e id in `<nome>.<data>.revocato` (niente cancellazioni). -> file creati."""
    d = d or cartella()
    ts = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    fatti = []
    # Prima l'id: il canale attivo osserva la revoca e avvisa Kairos chiudendo il socket.
    for nome in (ID, PRIVATA, PUB_KAIROS, PUB_COMANDI, URL):
        if (d / nome).exists():
            dest = d / f"{nome}.{ts}.revocato"
            (d / nome).replace(dest)
            fatti.append(dest)
    return fatti
