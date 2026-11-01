"""Sinergo: python -m collegato [scollega]."""
from __future__ import annotations

import asyncio
import contextlib
import sys
import webbrowser
from pathlib import Path

from . import attesa, canale, chiavi, pagina, trova
from .codice import AbbinamentoFallito, Richiesta
from .scheda import scheda


async def avvia(d: Path | None = None) -> None:
    d = d or chiavi.cartella()
    if chiavi.abbinata(d):
        await canale.esegui(chiavi.indirizzo(d), d=d)
        return

    richiesta = Richiesta()
    stato = pagina.Stato(codice=richiesta.codice)
    sock = pagina.prenota_socket()
    server = asyncio.create_task(pagina.servi(stato, sock))
    try:
        # Il socket e' gia' riservato; lasciamo partire il server prima del browser.
        await asyncio.sleep(0)
        await asyncio.to_thread(webbrowser.open, f'http://127.0.0.1:{sock.getsockname()[1]}/')
        try:
            stato.kairos = await trova.cerca(durata=10)
        except OSError:
            stato.kairos = []
        stato.ricerca_finita = True
        if len(stato.kairos) == 1:
            stato.indirizzo = stato.kairos[0]['indirizzo']
            stato.stato = 'trovato'

        def aggiorna(evento: str, corrente: Richiesta) -> None:
            stato.stato = evento
            # Dopo l'abbinamento il segreto non serve piu' neppure nella pagina.
            stato.codice = '' if evento == 'abbinato' else corrente.codice

        scheda_locale = await asyncio.to_thread(scheda)
        while not chiavi.abbinata(d):
            if not stato.indirizzo:
                await stato.indirizzo_scelto.wait()
            stato.indirizzo_scelto.clear()
            try:
                collegamento = asyncio.create_task(attesa.abbina(
                    stato.indirizzo, scheda_locale, d=d, aggiorna=aggiorna, richiesta=richiesta))
                cambio = asyncio.create_task(stato.indirizzo_scelto.wait())
                try:
                    conclusi, _ = await asyncio.wait((collegamento, cambio), return_when=asyncio.FIRST_COMPLETED)
                    if collegamento in conclusi:
                        await collegamento
                    else:
                        # Cambiare destinazione chiude la vecchia attesa e ne invalida il codice.
                        collegamento.cancel()
                        await asyncio.gather(collegamento, return_exceptions=True)
                        richiesta = Richiesta()
                        stato.codice = richiesta.codice
                finally:
                    collegamento.cancel()
                    cambio.cancel()
                    await asyncio.gather(collegamento, cambio, return_exceptions=True)
            except AbbinamentoFallito:
                stato.stato = 'errore'
                stato.indirizzo = None
                # Il dettaglio remoto non si riflette nel browser o nei log.
                richiesta = Richiesta()
                stato.codice = richiesta.codice
                continue
        await canale.esegui(chiavi.indirizzo(d), d=d)
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server
        sock.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['scollega']:
        fatti = chiavi.revoca()
        print(f'Sinergo scollegato ({len(fatti)} file conservati come *.revocato).')
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        asyncio.run(avvia())
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, AbbinamentoFallito):
        print('Sinergo: collegamento non disponibile. Riapri Sinergo per riprovare.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
