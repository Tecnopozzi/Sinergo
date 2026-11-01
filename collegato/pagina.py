"""Pagina di abbinamento Sinergo: codice in memoria, accesso solo da questo computer."""
from __future__ import annotations

import asyncio
import errno
import socket
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

LOOPBACK = "127.0.0.1"

TESTI = {
    "it": {
        "titolo": "Collega questo computer a Kairos",
        "istruzioni": "In Kairos apri «Aggiungi un computer», scegli questo computer e scrivi il codice.",
        "codice": "Il codice di questo computer",
        "cerco": "Cerco Kairos…",
        "trovato": "Trovato {nome}",
        "in_attesa": "In attesa del codice in Kairos",
        "abbinato": "Abbinato a Kairos",
        "errore": "Errore: collegamento a Kairos non riuscito. Controlla l'indirizzo e riprova.",
        "codice_errato": "Il codice scritto in Kairos non corrisponde. Riprova con quello mostrato qui.",
        "bloccato": "Troppi tentativi: preparo un codice nuovo…",
        "scaduto": "Il codice è scaduto: preparo un codice nuovo…",
        "dove": "Dove si trova il tuo Kairos?",
        "indirizzo": "Indirizzo di Kairos",
        "scegli": "Scegli il tuo Kairos",
        "collega": "Collega",
        "attendi": "La ricerca nella rete locale può richiedere 10 secondi.",
        "manuale": "Copia l'indirizzo dalla pagina «Aggiungi un computer» di Kairos.",
        "locale": "Questa pagina è visibile solo su questo computer.",
        "finito": "Puoi chiudere questa pagina. Lascia Sinergo in esecuzione per mantenere il collegamento.",
        "errore_indirizzo": "Controlla l'indirizzo di Kairos e riprova.",
    },
    "en": {
        "titolo": "Connect this computer to Kairos",
        "istruzioni": "In Kairos open “Add a computer”, select this computer and enter the code.",
        "codice": "This computer's code",
        "cerco": "Looking for Kairos…",
        "trovato": "Found {nome}",
        "in_attesa": "Waiting for the code in Kairos",
        "abbinato": "Connected to Kairos",
        "errore": "Error: could not connect to Kairos. Check the address and try again.",
        "codice_errato": "The code entered in Kairos does not match. Try the code shown here.",
        "bloccato": "Too many attempts: preparing a new code…",
        "scaduto": "The code expired: preparing a new code…",
        "dove": "Where is your Kairos?",
        "indirizzo": "Kairos address",
        "scegli": "Choose your Kairos",
        "collega": "Connect",
        "attendi": "Searching the local network can take 10 seconds.",
        "manuale": "Copy the address from Kairos’s “Add a computer” page.",
        "locale": "This page is only available on this computer.",
        "finito": "You can close this page. Keep Sinergo running to stay connected.",
        "errore_indirizzo": "Check the Kairos address and try again.",
    },
}


@dataclass
class Stato:
    # Il repr non contiene il codice: anche un registro accidentale dello stato non lo espone.
    codice: str = field(repr=False)
    stato: str = "cerco"
    kairos: list[dict] = field(default_factory=list)
    indirizzo: str | None = None
    ricerca_finita: bool = False
    errore: str | None = None
    indirizzo_scelto: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


def _lingua(richiesta: Request) -> str:
    """Rispetta l'ordine e i pesi del browser, con italiano come ripiego."""
    preferite = []
    for parte in richiesta.headers.get("accept-language", "it").split(","):
        nome, _, peso = parte.strip().partition(";")
        try:
            priorita = float(peso.split("=", 1)[1]) if peso else 1.0
        except (ValueError, IndexError):
            continue
        if priorita > 0:
            preferite.append((priorita, nome.split("-", 1)[0].lower()))
    for _, lingua in sorted(preferite, key=lambda voce: -voce[0]):
        if lingua in TESTI:
            return lingua
    return "it"


def _contenuto(stato: Stato, lingua: str) -> str:
    t = TESTI[lingua]
    nome = next((k.get("nome", "Kairos") for k in stato.kairos
                 if k.get("indirizzo") == stato.indirizzo), None)
    nome = nome or (stato.kairos[0].get("nome", "Kairos") if stato.kairos else "Kairos")
    messaggio = t.get(stato.stato, t["cerco"]).format(nome=nome)
    codice = "" if stato.stato == "abbinato" else (
        f'<p class="etichetta">{t["codice"]}</p>'
        f'<output id="codice" aria-label="{escape(t["codice"], quote=True)}">{escape(stato.codice)}</output>')
    campi = ""
    if not stato.indirizzo and len(stato.kairos) > 1 and stato.stato != "errore":
        opzioni = "".join(f'<option value="{escape(k["indirizzo"], quote=True)}">'
                          f'{escape(k.get("nome", "Kairos"))}</option>' for k in stato.kairos)
        campi = f'<label for="indirizzo">{t["scegli"]}</label><select id="indirizzo" name="indirizzo">{opzioni}</select>'
    elif stato.stato == "errore" or (not stato.indirizzo and stato.ricerca_finita and not stato.kairos):
        campi = (f'<label for="indirizzo">{t["dove"]}</label>'
                 f'<p class="aiuto" id="aiuto-indirizzo">{t["manuale"]}</p>'
                 f'<input id="indirizzo" name="indirizzo" type="url" required autocomplete="url" '
                 f'aria-describedby="aiuto-indirizzo" placeholder="https://kairos.esempio.it">')
    if campi:
        campi = (f'<form id="indirizzo-form">{campi}<button type="submit">{t["collega"]}</button>'
                 f'<p id="errore-indirizzo" class="errore" role="alert" hidden>{t["errore_indirizzo"]}</p></form>')
    nota = t["finito"] if stato.stato == "abbinato" else t["istruzioni"]
    attesa = f'<p class="aiuto">{t["attendi"]}</p>' if not stato.ricerca_finita and stato.stato == "cerco" else ""
    return (f'<h1>{t["titolo"]}</h1><p class="intro">{nota}</p>{codice}'
            f'<p id="stato" role="status" aria-live="polite">{escape(messaggio)}</p>{attesa}{campi}')


_STILE = """
:root{color-scheme:dark;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:#0d1117;color:#f0f3f6;font-size:16px}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;padding:36px 24px}
.pagina{max-width:660px;margin:0 auto}header{padding:0 0 36px;border-bottom:1px solid #30363d}
.marchio{font-size:1.25rem;font-weight:700}main{padding:36px 0 32px}h1{font-size:1.75rem;
font-weight:650;line-height:1.25;margin:0 0 16px;text-wrap:balance;letter-spacing:-.02em}
p{line-height:1.6;max-width:65ch}.intro{color:#c9d1d9;margin:0 0 32px}.etichetta{font-size:.875rem;
color:#c9d1d9;margin:0 0 8px}output{display:block;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
font-size:2rem;font-weight:650;letter-spacing:.04em;white-space:nowrap;font-variant-numeric:tabular-nums}
#stato{display:flex;gap:10px;align-items:baseline;margin:24px 0 0;font-weight:600}
#stato::before{content:"";width:8px;height:8px;background:#3fb950;border-radius:50%;flex:none}
.aiuto,footer{color:#c9d1d9;font-size:.875rem}.aiuto{margin:10px 0 0}form{margin-top:32px;
padding-top:24px;border-top:1px solid #30363d}label{display:block;font-weight:600;margin-bottom:10px}
input,select,button{font:inherit;border-radius:8px;min-height:44px}input,select{display:block;width:100%;
background:#161b22;color:#f0f3f6;border:1px solid #8b949e;padding:10px 12px;margin-top:14px}
input::placeholder{color:#b1bac4}button{background:#238636;color:#fff;border:0;padding:10px 24px;
font-weight:650;margin-top:14px;cursor:pointer}button:hover{background:#2ea043}button:active{background:#238636}
button:disabled{opacity:.6;cursor:wait}:focus-visible{outline:3px solid #58a6ff;outline-offset:4px}
.errore{color:#ff9b93}footer{border-top:1px solid #30363d;padding-top:24px}
@media(max-width:420px){body{padding:24px 18px}header{padding-bottom:24px}main{padding-top:28px}
h1{font-size:1.5rem}output{font-size:1.5rem;letter-spacing:.02em}}
"""

_SCRIPT = """
let ultimo = null;
async function aggiorna() {
  const main = document.querySelector('main');
  try {
    const r = await fetch('/stato', {cache: 'no-store'});
    if (!r.ok) return;
    const dati = await r.json();
    if (dati.html !== ultimo && !main.contains(document.activeElement)) {
      main.innerHTML = dati.html;
      ultimo = dati.html;
    }
  } catch (_) { /* Il prossimo aggiornamento riprova senza cambiare il codice. */ }
}
document.addEventListener('submit', async (evento) => {
  if (evento.target.id !== 'indirizzo-form') return;
  evento.preventDefault();
  const form = evento.target;
  const bottone = form.querySelector('button');
  bottone.disabled = true;
  try {
    const r = await fetch('/scegli', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({indirizzo: new FormData(form).get('indirizzo')})});
    if (!r.ok) throw new Error();
    document.activeElement.blur();
    await aggiorna();
  } catch (_) {
    form.querySelector('[role="alert"]').hidden = false;
  } finally { bottone.disabled = false; }
});
setInterval(aggiorna, 1000);
"""


def crea_app(stato: Stato) -> FastAPI:
    app = FastAPI(title="Sinergo", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def solo_locale(richiesta: Request, prossimo):
        # Il bind limita la rete; Host e Origin impediscono DNS rebinding e richieste da altri siti.
        origine = richiesta.headers.get("origin")
        if (richiesta.url.hostname != LOOPBACK
                or (origine and origine != f"http://{richiesta.headers.get('host', '')}")):
            return Response(status_code=403)
        risposta = await prossimo(richiesta)
        risposta.headers["Cache-Control"] = "no-store"
        risposta.headers["Referrer-Policy"] = "no-referrer"
        risposta.headers["X-Content-Type-Options"] = "nosniff"
        risposta.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        return risposta

    @app.get("/", response_class=HTMLResponse)
    async def pagina(richiesta: Request):
        lingua = _lingua(richiesta)
        return (f'<!doctype html><html lang="{lingua}"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sinergo</title>'
                f'<style>{_STILE}</style></head><body><div class="pagina"><header>'
                '<span class="marchio">Sinergo</span></header>'
                f'<main>{_contenuto(stato, lingua)}</main><footer>{TESTI[lingua]["locale"]}</footer>'
                f'</div><script>{_SCRIPT}</script></body></html>')

    @app.get("/stato")
    async def stato_corrente(richiesta: Request):
        return {"html": _contenuto(stato, _lingua(richiesta))}

    @app.post("/scegli")
    async def scegli(richiesta: Request):
        try:
            if richiesta.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise ValueError()
            dati = await richiesta.json()
            indirizzo = dati["indirizzo"].strip().rstrip("/")
            u = urlsplit(indirizzo)
            if (u.scheme not in ("http", "https", "ws", "wss") or not u.hostname
                    or u.username or u.password or u.query or u.fragment or not (u.port or 1)):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse({"ok": False}, status_code=400)
        stato.indirizzo = indirizzo
        stato.stato = "trovato"
        stato.indirizzo_scelto.set()
        return {"ok": True}

    return app


def prenota_socket(porta: int = 9310) -> socket.socket:
    """Riserva la prima porta libera senza mai aprire un'interfaccia LAN."""
    for candidata in range(porta, 65536):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Windows permette bind condivisi con SO_REUSEADDR: qui si impone l'esclusiva.
            # Linux ha gia' il bind esclusivo e non espone SO_EXCLUSIVEADDRUSE.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((LOOPBACK, candidata))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError as errore:
            sock.close()
            if errore.errno not in (errno.EADDRINUSE, 10048):  # WSAEADDRINUSE su Windows.
                raise
    raise OSError("Sinergo: nessuna porta locale disponibile")


async def servi(stato: Stato, sock: socket.socket) -> None:
    if sock.getsockname()[0] != LOOPBACK:
        raise ValueError("Sinergo: la pagina deve ascoltare solo su 127.0.0.1")
    config = uvicorn.Config(crea_app(stato), host=LOOPBACK, port=sock.getsockname()[1],
                            access_log=False, log_level="warning")
    await uvicorn.Server(config).serve(sockets=[sock])
