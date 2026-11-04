"""
Installa Sinergo collegato a Kairos oppure LocalRouter come SERVER o CLIENT.

    python installa.py --sinergo             installa Sinergo (gateway locale + collegato)
    python installa.py --server              genera la chiave se manca e la stampa
    python installa.py --rigenera-chiave     chiave nuova: la vecchia smette di valere
    python installa.py --client --nome NOME --indirizzo URL [--chiave K]
    python installa.py --client --elenco | --rimuovi NOME

Le modalita' server/client/sinergo scrivono nel .env accanto a questo file,
compreso LOCALROUTER_MODO: `python server.py` lo legge e avvia il modo scelto.
Sinergo prepara .env (chiave se manca, mai stampata), gateway solo su 127.0.0.1
(senza cloudflared) e un solo avvio automatico che lancia gateway + collegato.
"""

from __future__ import annotations

import argparse
import secrets
import getpass
import os
import platform
import subprocess
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"


def gateway_vivo(porta: int) -> bool:
    """True se http://127.0.0.1:<porta>/health risponde gia' (gateway riusabile)."""
    import httpx

    try:
        risposta = httpx.get(
            f"http://127.0.0.1:{int(porta)}/health",
            timeout=2,
            trust_env=False,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False
    return risposta.status_code == 200


def gateway_accetta_chiave(porta: int, chiave: str) -> bool:
    """True se GET /v1/models con Bearer chiave risponde 200; False su 401/403."""
    import httpx

    try:
        risposta = httpx.get(
            f"http://127.0.0.1:{int(porta)}/v1/models",
            headers={"Authorization": f"Bearer {chiave}"},
            timeout=2,
            trust_env=False,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False
    if risposta.status_code in (401, 403):
        return False
    return risposta.status_code == 200


def messaggio_gateway_altra_chiave(porta: int, cartella: Path) -> str:
    """Messaggio d'errore senza stampare la chiave."""
    return (
        f"sulla {porta} c'e' gia' un LocalRouter con un'altra chiave: "
        f"metti la sua API_KEY in {cartella}{os.sep}.env oppure "
        f"imposta SINERGO_GATEWAY_ENV=<percorso del suo .env>"
    )


def risolvi_env_gateway(cartella: Path) -> Path:
    """Percorso .env da cui leggere API_KEY/PORT (proprio o SINERGO_GATEWAY_ENV)."""
    import config

    esterno = (config.SINERGO_GATEWAY_ENV or "").strip()
    if not esterno:
        return cartella / ".env"
    percorso = Path(esterno)
    if not percorso.is_file():
        raise SystemExit(
            f"SINERGO_GATEWAY_ENV punta a un file assente: {percorso}"
        )
    return percorso


def prepara_env_sinergo(percorso: Path = ENV) -> None:
    """Chiave se manca (mai stampata), modo sinergo, ascolto solo in loopback."""
    attuale = leggi_env(percorso)
    valori: dict[str, str] = {
        "LOCALROUTER_MODO": "sinergo",
        "HOST": "127.0.0.1",
        "SINERGO_BIND_HOST": "127.0.0.1",
    }
    if not attuale.get("API_KEY"):
        valori["API_KEY"] = secrets.token_urlsafe(32)
    scrivi_env(percorso, valori)


def _python_venv(cartella: Path) -> Path:
    return cartella / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def _env_per_collegato(percorso: Path, gateway_env: Path | None = None) -> dict[str, str]:
    """Chiave e porta dal .env del gateway, senza chiederle all'utente."""
    ambiente = dict(os.environ)
    for nome, valore in leggi_env(percorso).items():
        ambiente[nome] = valore
    if gateway_env is not None and gateway_env != percorso:
        esterni = leggi_env(gateway_env)
        for nome in ("API_KEY", "PORT"):
            if nome in esterni:
                ambiente[nome] = esterni[nome]
    return ambiente


def lancia_gateway(cartella: Path, python: Path) -> None:
    """Avvia server.py in background. Nessun cloudflared."""
    logs = cartella / "logs"
    logs.mkdir(exist_ok=True)
    registro = open(logs / "server_sinergo.log", "a", encoding="utf-8")
    kwargs: dict = {
        "cwd": cartella,
        "stdout": registro,
        "stderr": subprocess.STDOUT,
        "env": _env_per_collegato(cartella / ".env"),
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(python), "server.py"], **kwargs)


def avvia_runtime_sinergo(cartella: Path | None = None) -> None:
    """Gateway se non c'e' gia', poi collegato. Una sola voce Run chiama qui."""
    cartella = cartella or Path(__file__).resolve().parent
    percorso_env = cartella / ".env"
    # Idempotente: crea chiave/host se mancano, non cambia una chiave gia' presente.
    prepara_env_sinergo(percorso_env)
    python = _python_venv(cartella)
    if not python.is_file():
        raise SystemExit("manca .venv: esegui prima python installa.py --sinergo")
    gateway_env = risolvi_env_gateway(cartella)
    valori = leggi_env(gateway_env)
    porta = int(valori.get("PORT") or "8088")
    chiave = valori.get("API_KEY", "")
    if gateway_vivo(porta):
        # Porta occupata: riusa solo se la chiave del collegato e' accettata.
        if not gateway_accetta_chiave(porta, chiave):
            raise SystemExit(messaggio_gateway_altra_chiave(porta, cartella))
    else:
        lancia_gateway(cartella, python)
    subprocess.run(
        [str(python), "-m", "collegato"],
        cwd=cartella,
        check=True,
        env=_env_per_collegato(percorso_env, gateway_env),
    )


def avvio_automatico_sinergo(cartella: Path) -> None:
    """Windows: voce dell'utente accanto agli avvii LocalRouter gia' presenti."""
    if platform.system() != "Windows":
        return
    # Linux non ha il registro Run: l'avvio automatico qui riguarda solo Windows.
    import winreg

    comando = f'wscript.exe "{cartella / "autostart.vbs"}" sinergo'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as chiave:
        winreg.SetValueEx(chiave, "Sinergo", 0, winreg.REG_SZ, comando)


def installa_sinergo(cartella: Path | None = None) -> None:
    """Dipendenze, .env Sinergo, avvio automatico, gateway (se serve) + collegato."""
    cartella = cartella or Path(__file__).resolve().parent
    python = _python_venv(cartella)
    print("Installazione Sinergo…")
    # Extra voce + chatterbox (codec.cpp: una sola ruota per CPU/AMD/NVIDIA).
    print("Dipendenze: uv sync --extra voce --extra chatterbox")
    subprocess.run(
        ["uv", "sync", "--extra", "voce", "--extra", "chatterbox"],
        cwd=cartella, check=True,
    )
    if not python.is_file():
        raise SystemExit("uv sync non ha creato .venv")
    prepara_env_sinergo(cartella / ".env")
    avvio_automatico_sinergo(cartella)
    print("Avvio Sinergo (gateway locale se serve, poi collegamento a Kairos).")
    avvia_runtime_sinergo(cartella)


def leggi_env(percorso: Path) -> dict[str, str]:
    """Le coppie CHIAVE=valore del .env (commenti e righe vuote ignorati)."""

    if not percorso.exists():
        return {}
    coppie = {}
    for riga in percorso.read_text(encoding="utf-8").splitlines():
        nome, uguale, valore = riga.partition("=")
        if uguale and not riga.lstrip().startswith("#"):
            coppie[nome.strip()] = valore.strip()
    return coppie


def scrivi_env(percorso: Path, valori: dict[str, str]) -> None:
    """Aggiorna o aggiunge le variabili, lasciando intatto il resto del file."""

    righe = percorso.read_text(encoding="utf-8").splitlines() if percorso.exists() else []
    restano = dict(valori)
    for i, riga in enumerate(righe):
        nome = riga.partition("=")[0].strip()
        if nome in restano and not riga.lstrip().startswith("#"):
            righe[i] = f"{nome}={restano.pop(nome)}"
    righe += [f"{nome}={valore}" for nome, valore in restano.items()]
    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")
    os.chmod(percorso, 0o600)


def installa_server(percorso: Path = ENV, rigenera: bool = False) -> str:
    chiave = leggi_env(percorso).get("API_KEY", "")
    if rigenera or not chiave:
        chiave = secrets.token_urlsafe(32)
    scrivi_env(percorso, {"LOCALROUTER_MODO": "server", "API_KEY": chiave})
    print(f"\nChiave del server (dalla a ogni client):\n\n    {chiave}\n")
    if rigenera:
        print("La chiave vecchia non vale piu' dal prossimo riavvio del server: riavvialo.")
    return chiave


def prova_server(indirizzo: str, chiave: str) -> None:
    """GET /v1/models con la chiave: esce con un messaggio chiaro se non da' 200."""

    import httpx

    url = indirizzo.rstrip("/") + "/v1/models"
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {chiave}", "User-Agent": "Mozilla/5.0"}, timeout=30)
    except httpx.HTTPError:
        raise SystemExit("Server non raggiungibile. Controlla l'indirizzo.") from None
    if r.status_code == 401:
        raise SystemExit("Il server risponde ma rifiuta la chiave (401). Chiedi la chiave giusta a chi lo gestisce.")
    if r.status_code != 200:
        raise SystemExit(f"Il server ha risposto HTTP {r.status_code} invece di 200: e' un LocalRouter acceso?")


def _salva_server(percorso: Path, elenco: dict) -> None:
    """Scrive atomicamente il registro con permessi riservati al proprietario."""
    import yaml

    with tempfile.NamedTemporaryFile(mode="w", dir=percorso.parent, prefix=".server-remoti-", encoding="utf-8", delete=False) as f:
        yaml.safe_dump(elenco, f, sort_keys=False, allow_unicode=True)
        temporaneo = f.name
    os.replace(temporaneo, percorso)


def installa_client(indirizzo: str, chiave: str, percorso: Path = ENV, nome: str | None = None) -> None:
    """Prova e aggiunge un server; conserva l'interfaccia singola precedente."""
    import httpx
    from remoti import leggi_server, valida_server

    registro = percorso.parent / "server_remoti.yaml"
    elenco = leggi_server(registro) if registro.exists() else {}
    if nome is not None:
        nome = str(nome)
        if nome in elenco:
            raise SystemExit(f"Il server {nome} esiste gia': nessuna modifica")
        try:
            nuovo = valida_server({nome: {"indirizzo": indirizzo, "chiave": chiave}})
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    prova_server(indirizzo, chiave)
    if nome is None:
        scrivi_env(percorso, {"LOCALROUTER_MODO": "client", "LOCALROUTER_SERVER": indirizzo.rstrip("/"), "LOCALROUTER_CHIAVE": chiave})
    else:
        # Conserva anche il server legacy quando si aggiunge il primo nodo nominato.
        vecchio = leggi_env(percorso)
        if not registro.exists() and vecchio.get("LOCALROUTER_SERVER"):
            try:
                r = httpx.get(vecchio["LOCALROUTER_SERVER"].rstrip("/") + "/health", headers={"Authorization": f"Bearer {vecchio.get('LOCALROUTER_CHIAVE', '')}", "User-Agent": "Mozilla/5.0"}, timeout=3)
                r.raise_for_status()
                precedente = str(r.json()["server_name"])
                elenco.update(valida_server({precedente: {"indirizzo": vecchio["LOCALROUTER_SERVER"], "chiave": vecchio.get("LOCALROUTER_CHIAVE", "")}}))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise SystemExit("Impossibile identificare il server legacy: registro non modificato") from None
            if nome in elenco:
                raise SystemExit(f"Il server {nome} esiste gia' nel .env: nessuna modifica")
        elenco.update(nuovo)
        _salva_server(registro, elenco)
        scrivi_env(percorso, {"LOCALROUTER_MODO": "client"})
    print("Server raggiunto, chiave accettata. Avvia con `python server.py` su http://127.0.0.1:8088/v1")


def rimuovi_server(nome: str, percorso: Path = ENV) -> None:
    """Rimuove soltanto la voce richiesta; un registro vuoto non usa il .env."""
    from remoti import leggi_server

    registro = percorso.parent / "server_remoti.yaml"
    elenco = leggi_server(registro) if registro.exists() else {}
    if nome not in elenco:
        raise SystemExit(f"Il server {nome} non esiste")
    del elenco[nome]
    _salva_server(registro, elenco)


def elenca_server(percorso: Path = ENV) -> None:
    """Mostra nomi e indirizzi, senza mostrare mai le chiavi."""
    from remoti import leggi_server

    registro = percorso.parent / "server_remoti.yaml"
    if registro.exists():
        for nome, dati in leggi_server(registro).items():
            print(f"{nome}: {dati['indirizzo']}")
    else:
        vecchio = leggi_env(percorso)
        if vecchio.get("LOCALROUTER_SERVER"):
            print(f"legacy: {vecchio['LOCALROUTER_SERVER']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modo = p.add_mutually_exclusive_group(required=True)
    modo.add_argument("--server", action="store_true")
    modo.add_argument("--rigenera-chiave", action="store_true")
    modo.add_argument("--client", action="store_true")
    modo.add_argument("--sinergo", action="store_true", help="installa Sinergo (gateway locale + collegamento a Kairos)")
    p.add_argument("--indirizzo", help="solo client: indirizzo del server, es. https://gpu.example.it")
    p.add_argument("--chiave", help="solo client: la chiave stampata dal server")
    p.add_argument("--nome", help="solo client: nome univoco del server, es. 5060")
    azione = p.add_mutually_exclusive_group()
    azione.add_argument("--rimuovi", metavar="NOME", help="solo client: rimuove un server")
    azione.add_argument("--elenco", action="store_true", help="solo client: mostra i server senza chiavi")
    a = p.parse_args()
    if a.sinergo:
        installa_sinergo()
    elif a.client:
        if a.elenco:
            elenca_server()
        elif a.rimuovi:
            rimuovi_server(a.rimuovi)
        else:
            installa_client(a.indirizzo or input("Indirizzo del server: ").strip(), a.chiave or getpass.getpass("Chiave: ").strip(), nome=a.nome or input("Nome del server: ").strip())
    else:
        installa_server(rigenera=a.rigenera_chiave)


if __name__ == "__main__":
    main()
