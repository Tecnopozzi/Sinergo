"""Canale uscente di Sinergo: messaggi firmati, letture locali e comandi di Kairos."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from . import chiavi, firma
from .scheda import scheda

logger = logging.getLogger("sinergo.canale")
FRAME_MAX = 6 * 1024 * 1024  # dopo autenticazione: audio base64 (~4/3)
LAVORI = ("scheda", "salute", "modelli", "misura", "prepara_ruolo", "ascolta", "parla", "prova_motore")
LAVORI_VOCE = frozenset({"prepara_ruolo", "ascolta", "parla", "prova_motore"})


def _leggi_router(percorso: str) -> dict | None:
    """Legge le API esistenti; non importa server/config e non apre il file .env."""
    try:
        porta = int(os.environ.get("PORT", "8088"))
        if not 1 <= porta <= 65535:
            return None
        chiave = os.environ.get("API_KEY", "")
        headers = {"Authorization": f"Bearer {chiave}"} if chiave else {}
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            risposta = client.get(f"http://127.0.0.1:{porta}{percorso}", headers=headers)
            risposta.raise_for_status()
            dati = risposta.json()
        return dati if isinstance(dati, dict) else None
    except (OSError, ValueError, RecursionError, httpx.HTTPError):
        return None


def _numero(valore) -> float | None:
    if isinstance(valore, (int, float)) and not isinstance(valore, bool) and math.isfinite(valore) and valore >= 0:
        return float(valore)
    return None


class Cliente:
    """Un'istanza per abbinamento: gli id visti sopravvivono alle riconnessioni."""

    def __init__(self, id_macchina: str, privata: bytes, pk_kairos: bytes, pk_comandi: bytes,
                 cartella_chiavi: Path | None = None) -> None:
        self.id_macchina = id_macchina
        self._privata, self._pk_kairos, self._pk_comandi = privata, pk_kairos, pk_comandi
        self._cartella = cartella_chiavi if cartella_chiavi is not None else chiavi.cartella()
        self._visti = firma.Visti()
        self._salutato = False
        self._registro_lock = threading.Lock()

    def ciao(self) -> str:
        self._salutato = False
        return json.dumps(firma.firma(self._privata, "ciao", {"id_macchina": self.id_macchina}))

    def _registro(self, msg: dict, esito: str, durata: float, motivo: str = "", verificato: bool = True) -> None:
        # Di un messaggio non autenticato conserviamo solo tipo/id, mai il corpo arbitrario.
        cosa = {"id": msg.get("id"), "tipo": msg.get("tipo")}
        if verificato:
            cosa["corpo"] = msg.get("corpo", {})
        riga = {"chi": {"id_macchina": self.id_macchina,
                        "chiave": "comandi" if msg.get("tipo") == "comando" else "lavori"},
                "quando": datetime.now(timezone.utc).isoformat(), "cosa": cosa,
                "esito": esito, "durata": round(durata, 6)}
        if motivo:
            riga["motivo"] = motivo
        with self._registro_lock:
            if not self._cartella.exists():
                chiavi._riserva(self._cartella)
            percorso = self._cartella / "comandi.log"
            fd = os.open(percorso, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as registro:
                # Su Windows la protezione e' l'ACL della cartella, non i bit POSIX.
                if sys.platform != "win32":
                    percorso.chmod(0o600)
                registro.write(json.dumps(riga, ensure_ascii=False) + "\n")
                registro.flush()

    def _verifica(self, frame: str) -> dict:
        grezzo: dict = {}
        try:
            if not isinstance(frame, str) or len(frame.encode("utf-8")) > FRAME_MAX:
                raise firma.MessaggioRifiutato("frame non testuale o troppo grande")
            grezzo = json.loads(frame)
            if not isinstance(grezzo, dict):
                grezzo = {}
                raise firma.MessaggioRifiutato("messaggio malformato")
            pubblica = self._pk_comandi if grezzo.get("tipo") == "comando" else self._pk_kairos
            msg = firma.verifica(pubblica, grezzo, self._visti)
            if not self._salutato and msg["tipo"] != "ciao_ok":
                raise firma.MessaggioRifiutato("primo frame non e' ciao_ok")
            if msg["tipo"] == "ciao_ok":
                self._salutato = True
            return msg
        except (ValueError, TypeError, RecursionError, firma.MessaggioRifiutato) as exc:
            motivo = str(exc) if isinstance(exc, firma.MessaggioRifiutato) else "messaggio malformato"
            self._registro(grezzo, "rifiutato", 0, motivo, verificato=False)
            logger.warning("Sinergo: messaggio del canale rifiutato (%s)", motivo)
            raise firma.MessaggioRifiutato(motivo) from exc

    def ricevi(self, frame: str) -> str | None:
        """Come il riferimento: verifica, risponde, solleva se il canale va chiuso."""
        return self._risposta(self._verifica(frame))

    def _lettura(self, tipo: str) -> dict:
        if tipo == "scheda":
            return scheda(cartella=self._cartella)
        if tipo == "salute":
            stato = _leggi_router("/health") or {}
            return {"stato": stato.get("status")}
        if tipo == "modelli":
            catalogo = _leggi_router("/v1/models")
            elenco = catalogo.get("data") if catalogo else None
            return {"modelli": [m["id"] for m in elenco if isinstance(m, dict) and isinstance(m.get("id"), str)]
                    if isinstance(elenco, list) else None}
        misure = _leggi_router("/metrics/system") or {}
        memoria = misure.get("memory") or {}
        ram = _numero(memoria.get("used_bytes")) if isinstance(memoria, dict) else None
        gpu = misure.get("gpu") or {}
        schede = gpu.get("gpus") if isinstance(gpu, dict) else None
        valori = [_numero(g.get("vram_used_mb")) for g in schede if isinstance(g, dict)] if isinstance(schede, list) else []
        vram = sum(valori) / 1024 if valori and all(v is not None for v in valori) else None
        # Nessuna inferenza di prova inventata: queste API non misurano token/s o cache.
        return {"token_s": None, "ram_gb": ram / 2**30 if ram is not None else None,
                "vram_gb": vram, "cache": None}

    def _comando(self, msg: dict) -> dict:
        corpo = msg["corpo"]
        programma, argomenti = corpo.get("programma"), corpo.get("argomenti")
        cartella, tetto = corpo.get("cartella"), corpo.get("tetto_secondi")
        valido = (isinstance(programma, str) and bool(programma) and "\0" not in programma
                  and isinstance(argomenti, list) and all(isinstance(a, str) and "\0" not in a for a in argomenti)
                  and isinstance(cartella, str) and "\0" not in cartella
                  and isinstance(tetto, int) and not isinstance(tetto, bool) and tetto > 0)
        if not valido:
            self._registro(msg, "rifiutato", 0, "comando malformato")
            return {"ok": False, "errore": "comando malformato"}
        # Il registro deve essere scrivibile PRIMA di avviare qualsiasi programma.
        self._registro(msg, "avviato", 0)
        inizio = time.monotonic()
        try:
            # POSIX usa exec, Windows CreateProcess; in entrambi i casi niente shell o stringa composta.
            risultato = subprocess.run([programma, *argomenti], cwd=cartella or None, shell=False,
                                       timeout=tetto, capture_output=True)
            esito = "ok" if risultato.returncode == 0 else "errore"
            # Anche i caratteri di controllo espansi come \\u0001 devono rientrare nei 64 KiB del canale.
            risposta = {"ok": risultato.returncode == 0, "dati": {"uscita": risultato.returncode,
                        "stdout": risultato.stdout[:2048].decode("utf-8", errors="replace"),
                        "stderr": risultato.stderr[:2048].decode("utf-8", errors="replace")}}
        except subprocess.TimeoutExpired:
            esito = "timeout"
            risposta = {"ok": False, "errore": "tempo massimo del comando superato"}
        except OSError as exc:
            esito = "errore"
            risposta = {"ok": False, "errore": f"avvio del comando fallito: {type(exc).__name__}"}
        self._registro(msg, esito, time.monotonic() - inizio)
        return risposta

    def _voce(self, tipo: str, corpo: dict) -> dict:
        from .voce import gestisci
        return gestisci(tipo, corpo, cartella=self._cartella)

    def _risposta(self, msg: dict) -> str | None:
        tipo = msg["tipo"]
        if tipo == "ciao_ok":
            return None
        if tipo == "ping":
            return json.dumps(firma.firma(self._privata, "pong", {}))
        if tipo == "comando":
            risposta = self._comando(msg)
        elif tipo in LAVORI_VOCE:
            risposta = self._voce(tipo, msg.get("corpo") or {})
        elif tipo in LAVORI:
            risposta = {"ok": True, "dati": self._lettura(tipo)}
        else:
            risposta = {"ok": False, "errore": "lavoro sconosciuto"}
        return json.dumps(firma.firma(self._privata, "risposta", {"a": msg["id"], **risposta}))


async def _sessione(ws, cliente: Cliente, d: Path | None) -> None:
    async def revocata() -> None:
        while chiavi.abbinata(d):
            await asyncio.sleep(0.5)
        # La chiusura prevista dal protocollo avvisa Kairos senza aggiungere tipi di messaggio.
        await ws.close(code=1000, reason="scollega")

    async def rispondi(msg: dict) -> None:
        risposta = await asyncio.to_thread(cliente._risposta, msg)
        if risposta is not None:
            await ws.send(risposta)

    lavori: set[asyncio.Task] = set()
    errori: list[BaseException] = []

    def finito(task: asyncio.Task) -> None:
        lavori.discard(task)
        if not task.cancelled() and task.exception() is not None:
            errori.append(task.exception())
            asyncio.create_task(ws.close(code=1011, reason="errore locale"))

    controllo = asyncio.create_task(revocata())
    try:
        await ws.send(cliente.ciao())
        cliente.ricevi(await asyncio.wait_for(ws.recv(), timeout=10))
        async for frame in ws:
            # Verifica seriale nel loop; programmi e letture lente non bloccano ping/pong.
            msg = cliente._verifica(frame)
            if msg["tipo"] in ("ping", "ciao_ok"):
                risposta = cliente._risposta(msg)
                if risposta is not None:
                    await ws.send(risposta)
            else:
                task = asyncio.create_task(rispondi(msg))
                lavori.add(task)
                task.add_done_callback(finito)
        if errori:
            raise errori[0]
    except firma.MessaggioRifiutato:
        await ws.close(code=1008, reason="messaggio rifiutato")
        raise
    finally:
        controllo.cancel()
        for task in lavori:
            task.cancel()
        await asyncio.gather(controllo, *lavori, return_exceptions=True)


async def esegui(indirizzo: str, d: Path | None = None) -> None:
    """Riconnette da 1 a 60 secondi; una revoca locale termina anche il canale aperto."""
    from .attesa import connetti

    privata, pk_kairos, pk_comandi, id_macchina = chiavi.carica(d)
    cliente = Cliente(id_macchina, privata, pk_kairos, pk_comandi, cartella_chiavi=d)
    ritardo = 1
    while chiavi.abbinata(d):
        try:
            # connetti verifica TLS/indirizzi e fissa il peer: nessun redirect o DNS rebinding.
            async with connetti(indirizzo, "/macchine/canale", max_size=FRAME_MAX, open_timeout=10) as ws:
                await _sessione(ws, cliente, d)
        except (OSError, InvalidHandshake, ConnectionClosed, asyncio.TimeoutError, firma.MessaggioRifiutato) as exc:
            logger.warning("Sinergo: canale interrotto (%s)", type(exc).__name__)
        if not chiavi.abbinata(d):
            return
        await asyncio.sleep(ritardo)
        ritardo = min(ritardo * 2, 60)
