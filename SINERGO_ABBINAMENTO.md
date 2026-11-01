# Sinergo: abbinamento nella forma finale (01/11/2026)

> Sostituisce il §1 di `docs/LOCALROUTER_COLLEGATO.md` (la «riga da incollare» con l'invito creato da Kairos).
> Canale (§2), messaggi firmati (§3), smistamento (§4), revoca (§5) restano come sono.
> Decisione di Luca del 31/10 (via FRONTEND). «Sinergo» è il nome per l'utente del LocalRouter; nel codice resta
> LocalRouter.

## Chi fa cosa
1. In Kairos, «Aggiungi un computer» mostra il link a github.com/Tecnopozzi/Sinergo, l'indirizzo di questo Kairos e
   l'elenco dei computer **in attesa**.
2. Sull'altro computer l'utente installa Sinergo. A fine installazione Sinergo si avvia e apre il browser su una sua
   pagina locale (solo `127.0.0.1`) che mostra un **CODICE**.
3. **Stessa rete**: Sinergo trova Kairos da solo (DNS-SD, servizio `_kairos._tcp.local.`, libreria `zeroconf`, va su
   Windows e Linux) e si presenta. **Reti diverse**: se in 10 s non trova niente, la pagina chiede «Dove si trova il
   tuo Kairos?» (campo indirizzo, es. `https://kairos.esempio.it`).
4. L'utente scrive in Kairos il codice mostrato da Sinergo. Scambio chiavi automatico, poi il canale normale (§2).
   Il router dell'utente non si tocca mai: la connessione è sempre da Sinergo verso Kairos.

## Il codice e le prove
- Sinergo genera: coppia Ed25519 della macchina (`pk_m`), `id_richiesta` = 16 byte casuali in hex, `codice` = 12
  caratteri Crockford base32 (alfabeto `0123456789ABCDEFGHJKMNPQRSTVWXYZ`), mostrato `XXXX-XXXX-XXXX`, 60 bit.
- `K = sha256(id_richiesta + "|" + codice_normalizzato)`. Normalizzazione (uguale ai due lati, già in
  `kairos/extensions/macchine/inviti.py:normalizza_codice`): maiuscolo, via spazi e trattini, O→0, I→1, L→1.
- `prova_m = HMAC-SHA256(K, "macchina|" + id_richiesta + "|" + pk_m_raw)`.
- Il codice resta di 12 caratteri perché `prova_m` viaggia prima che l'utente scriva il codice: chi la intercetta
  (un finto Kairos in rete locale) può provare i codici offline. 60 bit lo impediscono; 6 cifre no.
- Il codice non lascia mai Sinergo e non va nei log. Sinergo tiene `K` solo in memoria.

## Messaggi
Sinergo apre un WebSocket **in uscita** su `<kairos>/macchine/attesa` e manda un solo frame JSON:
```
{"tipo": "attesa", "id_richiesta": "<hex32>", "pk_macchina": "<hex64>", "scheda": {...}, "prova_m": "<hex64>"}
```
Kairos lo tiene **in memoria** (non nel DB) come «computer in attesa»:
- `trovato_in = "rete_locale"` se l'IP del peer è privato/loopback **e** la richiesta non ha header di proxy
  (`x-forwarded-for`, `cf-connecting-ip`, `forwarded`, `x-real-ip`); altrimenti `"internet"`.
- Scade 15 minuti dopo l'arrivo o alla chiusura del socket. Tetto: 16 in attesa insieme (il 17° viene chiuso subito),
  frame massimo 16 KB, scheda massimo 8 KB, primo frame entro 10 s. Un `id_richiesta` già in attesa è rifiutato.
- Kairos risponde subito `{"tipo": "in_attesa"}` e tiene il socket aperto (ping ogni 30 s).

Quando l'utente scrive il codice giusto in Kairos, Kairos registra la macchina (tabella `macchine` già esistente)
e manda sul socket d'attesa:
```
{"tipo": "abbinato", "id_macchina": "...", "pk_kairos": "<hex64>", "pk_comandi": "<hex64>",
 "prova_k": HMAC-SHA256(K, "kairos|" + id_richiesta + "|" + pk_m_raw + "|" + pk_k_raw + "|" + pk_comandi_raw)}
```
poi chiude. Sinergo verifica `prova_k` a tempo costante: se non torna non salva niente e mostra l'errore. Se torna
salva le chiavi (§1.6 del protocollo: 0700/0600 su Linux, DPAPI su Windows) e apre `/macchine/canale` col `ciao`.
**Novità**: `pk_comandi` è dentro `prova_k` (prima viaggiava fuori, protetta solo da TLS).

Codice sbagliato: Kairos manda `{"tipo": "codice_errato", "restano": n}`; al 5° errore `{"tipo": "bloccato"}` e chiude.
Scaduto: `{"tipo": "scaduto"}` e chiude. Sinergo, a socket chiuso senza `abbinato`, genera un codice nuovo e
riparte (la pagina si aggiorna da sola).

## API di Kairos per la pagina (confermate a FRONTEND il 31/10, risposta 117)
Stessa autorizzazione delle altre `/installazione/api/*` (`installazione._autorizzato`):
- `GET /installazione/api/macchine/in_attesa` -> `[{id, nome, scheda, trovato_in, da_quando}]` (`id` = id_richiesta)
- `POST /installazione/api/macchine/abbina` `{id, codice}` -> `{ok: true, id_macchina}` oppure
  `{ok: false, errore: "codice_errato"|"scaduto"|"bloccato"}`. Il codice arriva come l'ha scritto l'utente.
  Mai dire quale controllo è fallito oltre a questi tre. Tetto: 5 errori per richiesta, poi `bloccato` e la
  richiesta sparisce.
- `GET /installazione/api/macchine/indirizzo` -> `{pubblico: PUBLIC_BASE_URL o null, locale: "http://<ip-lan>:<porta>" o null}`
- Restano: `GET /installazione/api/macchine`, `PUT .../ruoli`, `GET .../proposta`, `DELETE .../{id}`.
- **Si tolgono**: `POST /installazione/api/macchine/invito` e la pubblica `POST /macchine/abbina` (meno superficie
  esposta). La logica di `inviti.py` che serve ai test per creare macchine vere può restare come funzione interna.

## Annuncio in rete
Kairos annuncia `_kairos._tcp.local.` con porta del server web e TXT `nome=<nome Kairos>`, `percorso=/`. Si
spegne con `KAIROS_ANNUNCIO_RETE=0` in config.py. Sul VPS l'annuncio non fa danni (nessuno in rete lo sente).
Sinergo cerca per 10 s; se trova più di un Kairos li mostra e l'utente sceglie.

## Rischi residui (da scrivere, non da risolvere ora)
- In rete locale senza TLS i messaggi del canale sono firmati ma non cifrati: chi è nella stessa rete può leggerli,
  non falsificarli. Sull'indirizzo pubblico TLS è obbligatorio (Sinergo rifiuta `http://` su un indirizzo non privato).
- Chi in rete locale finge di essere un Sinergo compare «in attesa», ma senza il codice mostrato dal vero Sinergo non
  si abbina: il codice è legato a `id_richiesta` e `pk_m`.
- Rimandato (Luca): punto d'incontro per «Kairos dietro un router e Sinergo dietro un altro».
