"""Ricerca DNS-SD di Kairos in rete locale, per Windows e Linux."""
from __future__ import annotations

import asyncio
import threading
import time

from zeroconf import Error as ErroreZeroconf
from zeroconf import ServiceBrowser, Zeroconf

SERVIZIO = "_kairos._tcp.local."


def _cerca(durata: float) -> list[dict[str, str]]:
    trovati: dict[str, dict[str, str]] = {}
    lock = threading.Lock()

    class Ascoltatore:
        def add_service(self, zc, tipo, nome):
            info = zc.get_service_info(tipo, nome, timeout=1000)
            if info is None:
                return
            indirizzi = info.parsed_scoped_addresses()
            if not indirizzi:
                return
            host = indirizzi[0]
            # Gli indirizzi IPv6 richiedono parentesi nell'URL su entrambi i sistemi.
            if ":" in host:
                host = "[" + host.replace("%", "%25") + "]"
            proprieta = info.properties
            titolo = proprieta.get(b"nome", b"")
            titolo = titolo.decode("utf-8", "replace") if isinstance(titolo, bytes) else str(titolo or "")
            percorso = proprieta.get(b"percorso", b"/")
            percorso = percorso.decode("utf-8", "replace") if isinstance(percorso, bytes) else str(percorso or "/")
            if not percorso.startswith("/") or percorso.startswith("//") or "?" in percorso or "#" in percorso:
                percorso = "/"
            with lock:
                trovati[nome] = {"nome": titolo or nome.removesuffix("." + SERVIZIO),
                                  "indirizzo": f"http://{host}:{info.port}{percorso}"}

        def update_service(self, zc, tipo, nome):
            self.add_service(zc, tipo, nome)

        def remove_service(self, zc, tipo, nome):
            with lock:
                trovati.pop(nome, None)

    zc = Zeroconf()
    browser = None
    try:
        browser = ServiceBrowser(zc, SERVIZIO, Ascoltatore())
        time.sleep(max(0, durata))
        with lock:
            return sorted(trovati.values(), key=lambda voce: (voce["nome"], voce["indirizzo"]))
    finally:
        if browser is not None:
            browser.cancel()
        zc.close()


async def cerca(durata: float = 10) -> list[dict[str, str]]:
    """Non blocca la pagina locale mentre Zeroconf usa i propri thread di rete."""
    try:
        return await asyncio.to_thread(_cerca, durata)
    except (OSError, ErroreZeroconf):
        # Multicast assente o vietato: la pagina offre comunque l'indirizzo manuale.
        return []
