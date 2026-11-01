"""Attesa dell'abbinamento Sinergo: sempre una connessione in uscita verso Kairos."""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import socket
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from . import chiavi
from .codice import AbbinamentoFallito, Richiesta

_RETI_LAN = tuple(ipaddress.ip_network(rete) for rete in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "fc00::/7", "fe80::/10", "::1/128"))


def _privato(host: str) -> bool:
    ip = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == rete.version and ip in rete for rete in _RETI_LAN)


async def _destinazione(indirizzo: str, percorso: str) -> tuple[str, str, int]:
    try:
        u = urlsplit(indirizzo.strip())
        porta = u.port if u.port is not None else (443 if u.scheme in ("https", "wss") else 80)
        if (u.scheme not in ("http", "https", "ws", "wss") or not u.hostname or u.username is not None or
                u.password is not None or u.query or u.fragment or not 1 <= porta <= 65535):
            raise ValueError
        host = u.hostname.replace("%25", "%")
    except (ValueError, AttributeError) as e:
        raise AbbinamentoFallito("indirizzo di Kairos non valido") from e
    try:
        indirizzi = await asyncio.get_running_loop().getaddrinfo(host, porta, type=socket.SOCK_STREAM)
    except OSError as e:
        raise AbbinamentoFallito("indirizzo di Kairos non raggiungibile") from e
    if not indirizzi:
        raise AbbinamentoFallito("indirizzo di Kairos non raggiungibile")
    sicuro = u.scheme in ("https", "wss")
    if not sicuro and any(not _privato(voce[4][0]) for voce in indirizzi):
        raise AbbinamentoFallito("TLS obbligatorio fuori dalla rete privata: usa https://")
    destinazione = indirizzi[0][4]
    ip = destinazione[0]
    # getaddrinfo rende lo scope IPv6 separato su Windows e Linux; l'indice numerico
    # funziona su entrambi, mentre i nomi delle interfacce non coincidono fra sistemi.
    if len(destinazione) == 4 and destinazione[3] and "%" not in ip:
        ip += f"%{destinazione[3]}"
    url = urlunsplit(("wss" if sicuro else "ws", u.netloc,
                      u.path.rstrip("/") + "/" + percorso.lstrip("/"), "", ""))
    return url, ip, porta


async def url_websocket(indirizzo: str, percorso: str) -> str:
    """Valida anche tutti gli IP risolti; per connettersi usare `connetti`."""
    return (await _destinazione(indirizzo, percorso))[0]


class _ConnessioneDiretta(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # Nessun redirect: non si consegnano prove o messaggi firmati a un altro URL.
        return exc


@asynccontextmanager
async def connetti(indirizzo: str, percorso: str, **opzioni):
    """Fissa l'IP verificato, conserva Host/SNI e verifica normalmente i certificati.

    Il proxy di sistema e' escluso: un proxy potrebbe risolvere diversamente il nome
    dopo il controllo LAN. `host` cambia solo la destinazione TCP, non Host o TLS SNI.
    """
    url, ip, porta = await _destinazione(indirizzo, percorso)
    async with _ConnessioneDiretta(url, host=ip, port=porta, proxy=None, **opzioni) as ws:
        yield ws


async def _aggiorna(callback: Callable | None, stato: str, richiesta: Richiesta) -> None:
    if callback is not None:
        esito = callback(stato, richiesta)
        if inspect.isawaitable(esito):
            await esito


async def abbina(indirizzo: str, scheda: dict, *, d: Path | None = None,
                 aggiorna: Callable | None = None, richiesta: Richiesta | None = None) -> str:
    """Salva solo dopo prova_k valida e ritorna l'id; il chiamante avvia il canale.

    Ogni chiusura senza abbinamento invalida il codice e crea una nuova richiesta.
    """
    # La validazione iniziale fallisce subito per indirizzi pubblici senza TLS.
    await url_websocket(indirizzo, "/macchine/attesa")
    attesa_s = 1
    while True:
        richiesta = richiesta or Richiesta()
        await _aggiorna(aggiorna, "in_attesa", richiesta)
        try:
            async with connetti(indirizzo, "/macchine/attesa", max_size=16 * 1024, ping_interval=30,
                                ping_timeout=30, open_timeout=10) as ws:
                await ws.send(json.dumps(richiesta.frame(scheda), ensure_ascii=False))
                async for frame in ws:
                    try:
                        msg = json.loads(frame)
                        tipo = msg["tipo"]
                    except (ValueError, KeyError, TypeError, RecursionError) as e:
                        raise AbbinamentoFallito("messaggio di Kairos malformato") from e
                    if tipo == "abbinato":
                        pk_k, pk_c, id_macchina = richiesta.verifica_risposta(msg)
                        chiavi.salva(richiesta.privata, pk_k, id_macchina, d=d,
                                      pk_comandi=pk_c, indirizzo=indirizzo)
                        await _aggiorna(aggiorna, "abbinato", richiesta)
                        return id_macchina
                    if tipo not in ("in_attesa", "codice_errato", "bloccato", "scaduto"):
                        raise AbbinamentoFallito("messaggio di Kairos inatteso")
                    await _aggiorna(aggiorna, tipo, richiesta)
                    if tipo in ("bloccato", "scaduto"):
                        break
        except AbbinamentoFallito:
            await _aggiorna(aggiorna, "errore", richiesta)
            raise
        except (ConnectionClosed, InvalidHandshake, OSError, asyncio.TimeoutError):
            await _aggiorna(aggiorna, "errore", richiesta)
        await asyncio.sleep(attesa_s)
        attesa_s = min(attesa_s * 2, 60)
        richiesta = None
