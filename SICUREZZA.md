# SICUREZZA (01/11/2026, aggiornamento Sinergo)

Modello di minaccia: server esposto su Internet (tunnel o porta), installato in casa di altri, comandato da Kairos.
Chi ha la chiave API (Kairos, o chi lo compromette) e' l'avversario piu' realistico; poi chi non ha la chiave.

## Cosa blocca cosa

| Minaccia | Guardia | Test |
|---|---|---|
| Server senza chiave | fail-closed: `chiave_valida` falsa senza `API_KEY` (`auth.py`, `chiave_valida`); middleware su ogni rotta (`server.py:~625`) | `test_server_client.py::test_nessuna_route_aperta_salvo_health_e_pagina` |
| Timing sulla chiave | `hmac.compare_digest` (`auth.py`, chiave API e token pull-restart) | – (stdlib) |
| Indovinare la chiave | chiave >= 16 caratteri o il server non parte (`config.py:61`); `installa.py` ne genera una da 256 bit; tetto GLOBALE di 30 tentativi falliti/minuto, poi 429 (`auth.py:40`, usato in `server.py` middleware e nei due `require_*`). Una chiave giusta passa sempre. | `test_sicurezza.py::test_chiave_sbagliata_ripetuta...`, `::test_chiave_corta_rifiutata_all_avvio` |
| Fuga di topologia da `/health` senza chiave | senza chiave solo `{"status"}` (`server.py:2399`) | `::test_health_senza_chiave_non_espone_host_interni` |
| SSRF: `image_url`/`audio_url` http(s) scaricati da llama-server | rifiutati, passano solo `data:` (`server.py:1817`, chiamato da chat e chat/jobs) | `::test_url_remoti_nei_media_rifiutati` |
| Download di repo/percorsi arbitrari da `/whisper/download-model` | `model`, `device`, `compute_type` solo `[A-Za-z0-9._-]` (`server.py:4048`) | `::test_download_whisper_rifiuta_repo_e_percorsi` |
| Corpi enormi (RAM) | `LimiteCorpo` ASGI, 413 per Content-Length e conteggio sul flusso, `MAX_BODY_BYTES` (160 MB) (`server.py:594`) | `::test_corpo_troppo_grande_413`, `::test_corpo_senza_content_length...` |
| `.env` con la chiave leggibile da altri utenti | `chmod 600` (`installa.py:54`) | `::test_env_scritto_solo_per_il_proprietario` |
| Abbinamento a Kairos: uomo in mezzo sulle chiavi pubbliche | `prova_m`/`prova_k` HMAC-SHA256 con `K=sha256(id_richiesta\|codice)`; `prova_k` include anche `pk_comandi`, verifica con `compare_digest` prima di salvare (`collegato/codice.py`) | `test_sinergo.py::test_kairos_sostituisce_chiave_nessun_salvataggio`, `::test_abbinamento_completo_e_canale` |
| TLS assente fuori LAN o DNS rebinding | HTTPS/WSS con verifica ordinaria fuori LAN; tutte le risoluzioni controllate e IP fissato prima della connessione, nessun redirect/proxy implicito (`collegato/attesa.py`) | `test_sinergo_abbinamento.py::test_http_pubblico_rifiutato`, `::test_connessione_usa_ip_verificato_e_conserva_host`, `::test_redirect_non_invia_prove_ad_altro_endpoint` |
| Pagina del codice raggiungibile da rete o siti ostili | Bind solo127.0.0.1, Host/Origin controllati, risposte no-store, nessun asset esterno (`collegato/pagina.py`) | `test_sinergo_pagina.py::test_socket_prima_porta_libera_e_solo_loopback`, `::test_pagina_rifiuta_host_esterno_e_post_da_altra_origine` |
| Chiavi della macchina leggibili da altri | Linux: cartella0700, file0600 anche se preesistenti larghi. Windows: DPAPI ambito utente e DACL sostitutiva protetta con sola ACE dell'utente del processo, su cartella e tutti i file (`collegato/chiavi.py`) | `test_collegato.py::test_permessi_linux`, `::test_windows_dpapi_e_acl_con_mock`, `::test_dpapi_ctypes_usa_ambito_utente`, `test_sinergo_chiavi_windows.py` (Windows simulato) |
| `scollega` che perde o lascia le chiavi | sposta in `*.revocato` datato, nessuna cancellazione; la macchina torna non abbinata | `::test_scollega_*` |

Mutazioni Sinergo: `.venv/bin/python tests/mutate_sinergo.py`, esiti completi nel [rapporto](materiale/VERIFICA_SINERGO.md). Il vecchio abbinamento con stringa/impronta e i relativi test sono rimossi. Il Kairos finto dei nuovi test usa HMAC e firme indipendenti; il formato di `firma.py` e' verificato anche tramite SHA256 fissato dal riferimento, senza dipendere dai file esclusi dal commit.

Gia' a posto prima: nessun `shell=True` (unico `subprocess` col comando fisso: `_run_cmd` con lista statica; `whisper_admin` usa `create_subprocess_exec` con lista); `/v1/kv/slot` valida `nome` con regex e `azione` con Literal e quota il modello nel path; nessun percorso di file preso dal client (gli upload vanno in cartelle per job, il suffisso viene da `Path(filename).suffix`); bind di default `127.0.0.1`; nessun header di autorizzazione nel registro delle richieste; `pip-audit -r requirements.txt`: nessuna vulnerabilita' nota.

## Attacco locale del 31/10/2026 (ramo `attacco-3110`)
Provato su una copia di `main` su 127.0.0.1 (server e client). Trovato e corretto qui:

| Difetto (provato) | Correzione | Test |
|---|---|---|
| **ALTO - XSS memorizzato nella dashboard.** Un anonimo scrive `User-Agent`, `X-Forwarded-For` o `model` nel registro (anche con 401); `/dashboard/data` li restituisce e la pagina li metteva in `innerHTML`: script nella pagina con la chiave in `localStorage` | `esc()` su ogni campo del registro/host in `_DASHBOARD_HTML` | `test_dashboard_non_esegue_html_preso_dal_registro` (esegue lo script con node) |
| **ALTO - modalita' client aperta al browser.** Nessun controllo locale, `Origin`/`Host` qualsiasi, CORS `*` del server a monte: qualunque sito aperto sul PC legge `/debug/requests` e usa la GPU con la chiave del server | middleware `solo_locale` in `cliente.py`: 403 se `Host` non e' 127.0.0.1/localhost/::1 o se c'e' `Origin` | `test_client_rifiuta_host_e_origin_non_locali` |
| MEDIO - chiave in `?api_key=`/`?key=` scritta in chiaro nel registro richieste (e rileggibile da `/debug/requests`); la dashboard la leggeva anche dall'URL | query oscurata (`***`) nel registro; la dashboard non legge piu' la chiave dall'URL | `test_query_con_la_chiave_non_finisce_nel_registro` |
| BASSO - chiave o token non ASCII: `hmac.compare_digest` solleva `TypeError`, risposta 500 | confronto sui byte UTF-8 (`auth._uguali`) | `test_chiave_non_ascii_*` |

Provato e risultato a posto su `main`: nessuna rotta senza chiave salvo `/health` e `/dashboard` (pagina statica); `/docs` e `/openapi.json` chiusi; confronto chiave a tempo costante (mediana 523 us contro 480 us, nessuna differenza); path traversal sui parametri (404/422); verbi non previsti 405; input ostile a `/v1/kv/slot` respinto dalla regex.
Esiti storici: limite di 120/min aggirabile con `X-Forwarded-For` diversi (130 richieste su 130 passate; corretto in `revisione-codex-3110` insieme alla crescita permanente dei bucket); SSRF `image_url`/`audio_url` inoltrata tale e quale a llama-server (corretta in `sicurezza-3110`); 120 MB in un corpo = 560 MB di RAM (corretto da `LimiteCorpo` in `sicurezza-3110`, ma il tetto e' 160 MB).

## Revisione Codex del 31/10/2026

Worktree di revisione Codex, ramo `revisione-codex-3110`,
base `attacco-3110` (`7af2abb`). Letti `server.py`, `auth.py`, `cliente.py`,
`config.py`, `installa.py` e i moduli direttamente coinvolti nei percorsi esaminati.
`collegato/` **non esiste nella base richiesta**: nessuna verifica di quel protocollo
viene rivendicata. Nessun servizio reale interrogato, nessun bind pubblico, nessuna
lettura di `.env*` o file di chiavi. Il push del ramo e' l'unica operazione Git
remota richiesta dalla consegna.

Gia' corretto nella base, e quindi non contato come nuova scoperta:

- Autenticazione fail-closed su tutte le rotte salvo quelle esplicitamente aperte;
  chiave minima di 16 caratteri e token amministrativo separato.
- Confronto costante sui byte UTF-8, anche per token non ASCII; soglia globale
  sui fallimenti di autenticazione.
- `/health` anonimo restituisce solo lo stato, senza topologia interna.
- Blocco URL remoti nei messaggi media delle due rotte chat, validazione dei
  parametri del download Whisper e del nome degli slot KV.
- Limite ASGI sui corpi, bind locale predefinito, processi esterni senza shell.
- Dashboard con escape dei campi ostili gia' individuati, client con controllo
  Host/Origin, parametri query di autenticazione oscurati nel registro.
- L'installer imposta permessi 0600 sulla configurazione. La verifica storica
  dei permessi non e' stata rieseguita: il relativo test apre un file `.env`.

### Nuovi difetti e correzioni

Riferimenti alle righe della versione corretta in questo ramo. Nessun nuovo
difetto alto confermato; cinque difetti medi e uno basso riprodotti.

| ID / gravita' | File:riga | Scenario verificato | Correzione minima |
|---|---|---|---|
| M1 / MEDIO | `auth.py:37` | Un anonimo continua a ricevere 429 ma ogni tentativo aggiunge un timestamp: 2030 tentativi conservano 2030 elementi. Memoria proporzionale al traffico dell'ultimo minuto, anche quando respinto. | Deque limitata agli ultimi 31 tentativi; soglia 30/min e scadenza conservate. |
| M2 / MEDIO | `auth.py:87`, `auth.py:123` | Con la chiave valida, IP dichiarati sempre diversi creano bucket mai rimossi: crescita permanente della RAM. E' distinto dal solo aggiramento della frequenza, gia' noto. | Un bucket globale `api`, coerente con la chiave unica; secondo bucket fisso per health. Risolve anche l'aggiramento X-Forwarded-For gia' documentato. |
| M3 / MEDIO | `server.py:2846`, `server.py:2850`, `server.py:403` | Ogni submit chat crea un task libero; tutti i risultati e le code eventi restano per sempre. Cinque richieste simultanee vengono tutte accettate, e sei completamenti restano tutti in memoria. La cancellazione del backend lascia inoltre il job in `running`. | Prenotazione atomica sotto lock, massimo 16 job attivi e 100 conservati, espulsione dei soli conclusi insieme alle code eventi; stato finale `cancelled` se il task viene cancellato. |
| M4 / MEDIO | `config.py:347`, `config.py:410` | Le code OCR/media predefinite hanno `maxsize=0`: upload autenticati piu' veloci della GPU conservano byte senza tetto, fino all'esaurimento della RAM. | Massimo 4 job in attesa per coda di default; 0 e negativi diventano 1, input numerico errato usa 4. Il percorso QueueFull esistente restituisce 503. |
| M5 / MEDIO | `server.py:2342`, `auth.py:127` | `/health` anonimo esegue controlli dei backend a ogni chiamata senza quota: amplificazione verso i servizi interni anche con tutte le rotte operative protette. | Quota globale separata prima dei backend, con `RATE_LIMIT_PER_MINUTE`; oltre soglia 429, nessun accesso ai backend. |
| B1 / BASSO | `server.py:2583` | Un JSON valido ma non oggetto (`[]`) su `/debug/probe` produce AttributeError e HTTP 500. | Controllo del tipo, HTTP 400. |

Compatibilita': i client che condividono la chiave condividono ora anche la quota
API. Health ha una quota separata e puo' rispondere 429 sotto carico. Le code
upload non possono piu' essere rese illimitate con 0. `CHAT_JOB_MAX_ACTIVE` e
`CHAT_JOB_MAX_RETAINED` sono configurabili; il secondo e' almeno pari al primo.
Lo storico espulso restituisce 404; i job attivi non vengono espulsi. I limiti
sono per processo, non distribuiti fra piu' worker. Il numero di job e' limitato,
ma non viene aggiunto un limite in byte alle risposte dei backend.

### Prove rosso/verde e regressione

Test in `tests/test_revisione_3110.py`. Per ciascuna riga sotto e' stata
**annullata la sola correzione**, eseguito pytest (rosso), ripristinato il codice
in `finally` ed eseguito di nuovo pytest (verde). Output completi e comandi:
[`materiale/VERIFICA_REVISIONE_CODEX_3110.txt`](materiale/VERIFICA_REVISIONE_CODEX_3110.txt).

| Correzione | Test (prefisso `test_`) | Senza correzione | Ripristinata |
|---|---|---|---|
| M1 | `fallimenti_auth_memoria_limitata` | `1 failed` | `1 passed` |
| M2 | `bucket_auth_non_crescono_con_ip_dichiarati` | `1 failed` | `1 passed` |
| M3 attivi | `chat_job_limite_attivi_atomico` | `1 failed` | `1 passed` |
| M3 storico | `chat_job_storico_limitato` | `1 failed` | `1 passed` |
| M3 cancellazione | `chat_job_cancellato_libera_capacita` | `1 failed` | `1 passed` |
| M4 | `code_upload_sempre_limitate` (2 code x 4 configurazioni) | `8 failed` | `8 passed` |
| M5 | `health_anonimo_limitato_prima_dei_backend` | `1 failed` | `1 passed` |
| B1 | `debug_probe_rifiuta_json_non_oggetto` | `1 failed` | `1 passed` |

Runner `tests/verifica_offline_3110.py`: disabilita dotenv, blocca l'apertura di
file segreti, connessioni esterne e bind pubblici, esegue async con AnyIO gia'
installato. Nessuna dipendenza installata. La fixture auth azzera anche le quote
fra test, per evitare che richieste di test distinti consumino la stessa finestra.
La regressione esclude sette test dell'installer che leggono/scrivono `.env`.
Il comando e l'esito finale sono riportati nello stesso file di verifica.
Esito: **171 passed, 7 deselected, 1 warning** (deprecazione Starlette/httpx).

## Seconda revisione Codex del 31/10/2026

Base `e77883e7916abf25b26a1b331e472616b7ddc79d`, stesso worktree e ramo
`revisione-codex-3110`. I difetti della prima revisione e quelli gia' elencati
fra gli aperti non sono conteggiati di nuovo. Esame del flusso di autorizzazione
di tutte le rotte, parsing annidato, processi, temporanei, gare e controlli
Windows: nessun nuovo bypass di autenticazione o SSRF confermato.

### Elenco dei nuovi difetti

Sette difetti **MEDI** e uno **BASSO**, tutti corretti. I riferimenti nella
seconda colonna indicano il codice vulnerabile nella base `e77883e7`; quelli
nell'ultima indicano questa versione corretta.

| ID / gravita' | File:riga nella base | Scenario verificato | Correzione minima e riferimento attuale |
|---|---|---|---|
| S2-M1 / MEDIO | `media/media_pipeline.py:161`, `transcribe/whisper_pipeline.py:154`, `transcribe/whisper_admin.py:92` | La cancellazione del task che attende un worker lascia il processo vivo e libera il semaforo Whisper. Su timeout, cancellare `communicate` prima di `wait` lascia inoltre senza lettore le pipe, con possibile attesa bloccata. | Helper comune `subprocess_utils.py:7`: mantiene il lettore con `shield`, termina il figlio, drena le pipe e attende la sua uscita prima di liberare il worker. Tolto anche il `kill` media su un figlio gia' terminato. |
| S2-M2 / MEDIO | `pdf_utils.py:65` | Un PDF piccolo puo' dichiarare una pagina enorme: viene chiamato `render` senza verificare prima il raster, consentendo un'allocazione sproporzionata al corpo HTTP. | Dimensioni finite e positive, massimo `IMAGE_MAX_SIDE` al quadrato pixel prima del render (`pdf_utils.py:74`); chiusura garantita di documento, pagine, bitmap e immagini anche in errore. |
| S2-M3 / MEDIO | `transcribe/whisper_pipeline.py:101`, `transcribe/whisper_pipeline.py:146` | Il semaforo limita i figli attivi, ma ogni richiesta in attesa ha gia' salvato audio e configurazione: nessun tetto al numero di attese e relativi byte. | Prenotazione senza `await` prima dei temporanei (`transcribe/whisper_pipeline.py:82`): massimo `WHISPER_MAX_CONCURRENT_JOBS + 4` richieste ammesse, contatore liberato in `finally`. Tutte e tre le rotte audio locali rispondono 503 se la coda e' piena (`server.py:4130`). |
| S2-M4 / MEDIO | `media/media_pipeline.py:104`, `media/media_pipeline.py:129`, `transcribe/whisper_pipeline.py:101`, `transcribe/whisper_pipeline.py:161` | Media conserva per sempre input PNG e JSON `delete=False`; il TTL elimina solo il risultato. Whisper lascia i propri temporanei se spawn fallisce o il task viene cancellato. Upload ripetuti riempiono il disco e conservano contenuti degli utenti. | Scratch media con `TemporaryDirectory`, pulito dopo l'uscita del figlio (`media/media_pipeline.py:107`); cleanup Whisper in `finally` (`transcribe/whisper_pipeline.py:121`). Su errore media elimina anche output parziale e cartella vuota. Al TTL rimuove la sola cartella del job, se vuota e nel percorso atteso (`media/media_job_store.py:100`). |
| S2-M5 / MEDIO | `job_store.py:85`, `media/media_job_store.py:92` | Il TTL dalla creazione espelle anche job queued/running. Una consultazione o un altro upload durante un lavoro lungo lo fa scomparire; aggiornamenti successivi non trovano il job e il risultato media puo' restare orfano. | Espulsione dei soli stati terminali, contando il TTL dall'ultimo aggiornamento (`job_store.py:85`, `media/media_job_store.py:92`). |
| S2-M6 / MEDIO | `server.py:2933` | La coda eventi chat e' condivisa: un solo lettore consuma l'evento terminale, gli altri e le riconnessioni restano in heartbeat per sempre. Anche l'espulsione del job lascia aperto il lettore che conserva la coda. | Lo stream controlla lo stato persistito e termina anche per job concluso o espulso (`server.py:2934`), senza dipendere dal consumo della notifica in coda. |
| S2-M7 / MEDIO | `schemas.py:86` | La regex accetta `NUL`, `CON`, `AUX`, `PRN`, `COM1`, `LPT1` e varianti con estensione; `fotografia_slot` li inoltra come filename al backend. Su Windows sono dispositivi DOS, non file ordinari. Anche `.` e `..` passavano. | Validatore `PureWindowsPath.is_reserved()` su ogni piattaforma e rifiuto dei due nomi di directory (`schemas.py:90`), prima di qualsiasi chiamata al backend. |
| S2-B1 / BASSO | `server.py:702` | `NaN` e `+/-Infinity` in campi annidati restano numeri nel registro JSONL, che diventa incompatibile con parser JSON stretti. L'ipotesi di DoS di `/debug/requests` e' stata smentita nel runtime disponibile: risponde 200. | `_safe_json` converte i float non finiti in `null` anche nei campi annidati (`server.py:703`). |

### Prove annulla / rosso / ripristina / verde

Per ogni correzione e' stata tolta **soltanto la guardia interessata**, eseguito
pytest, ripristinato il codice in `finally` ed eseguito nuovamente pytest.
Gli output completi con comandi ed exit code sono nei tre referti collegati.

| ID | Prova | Correzione annullata | Ripristinata | Output |
|---|---|---|---|---|
| S2-M1 | cancellazione figli media / Whisper / download | `3 failed` | `3 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M2 | pagina PDF enorme, nessuna allocazione reale | `1 failed` | `1 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M3 | limite attesa prima dei temporanei | `1 failed` | `1 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M3 | risposta 503 sulle tre rotte audio | `3 failed` | `3 passed` | [audio](materiale/VERIFICA_SECONDA_EVENTI_AUDIO.txt) |
| S2-M4 | cleanup Whisper su cancellazione / errore spawn | `2 failed` | `2 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M4 | cleanup media su cancellazione / errore spawn | `2 failed` | `2 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M4 | cleanup cartella al TTL, preservando directory estranee | `1 failed, 1 passed` | `2 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M5 | TTL OCR / media, queued / running | `4 failed` | `4 passed` | [processi](materiale/VERIFICA_SECONDA_PROCESSI.txt) |
| S2-M6 | stream terminali / job espulso | `4 failed` | `4 passed` | [eventi](materiale/VERIFICA_SECONDA_EVENTI_AUDIO.txt) |
| S2-M7 | nomi slot riservati respinti prima del backend | `12 failed` | `12 passed` | [input](materiale/VERIFICA_SECONDA_INPUT.txt) |
| S2-B1 | numeri non finiti annidati nel JSONL | `3 failed` | `3 passed` | [input](materiale/VERIFICA_SECONDA_INPUT.txt) |

Sono inclusi anche percorsi positivi per PDF, figli e nomi slot ordinari,
timeout dei figli e gara fra uscita del processo e segnale di terminazione.
I test non avviano worker reali. La prova Windows riproduce l'inoltro non
validato e i nomi riservati con la stdlib, senza aprire dispositivi Windows.
La cancellazione dei figli e' provata sul task ASGI/worker, per esempio durante
shutdown; non viene attribuita automaticamente al semplice disconnect HTTP.

### Regressione e limiti della verifica

I vecchi server finti dei test sono stati sostituiti con ASGITransport e
MockTransport in memoria, mantenendo anche la prova che il primo chunk SSE
arrivi prima dello sblocco del secondo. Il runner offline ora vieta tutte le
connessioni TCP e i bind IP, incluso loopback; la fixture globale vieta il
trasporto HTTP reale. Nessun servizio avviato, nessuna richiesta a host esterni,
nessun `.env*` o file di chiavi aperto. Nessuna dipendenza installata o aggiornata.
Il solo push Git del ramo e' l'operazione remota richiesta dalla consegna.

Regressione dell'intera cartella `tests`, con le sole sette esclusioni storiche
che richiedono `.env`: **218 passed, 7 deselected, 1 warning**. Comando completo
e riga finale pytest: [referto](materiale/VERIFICA_SECONDA_REGRESSIONE.txt).
Il warning e' la deprecazione Starlette/httpx gia' presente.

I limiti restano per processo; Whisper ammette quattro attese oltre ai worker
configurati. Il limite PDF e' per pagina (default 16 milioni di pixel), non un
budget aggregato dell'intero documento. Un lettore SSE gia' sospeso sulla coda
condivisa puo' attendere al massimo l'intervallo heartbeat di 15 secondi prima
di vedere la chiusura. I casi Windows e PDF ostili sono simulati in memoria:
non si rivendica un collaudo su Windows o su una GPU reale. Gli aperti della
prima revisione restano indicati sotto, senza contarli come nuove scoperte.

## Se Kairos e' compromesso (chiave API rubata)
Puo': usare la GPU, caricare/scaricare modelli gia' presenti, leggere `/debug/requests` (prompt degli utenti, troncati a 4000 caratteri), salvare/ripristinare slot KV con nomi validati. NON puo': eseguire comandi, uscire dalle cartelle (non esistono rotte con percorsi), aggiornare il codice (token separato). Resta il rischio di consumo GPU (grammar/contesti enormi): oltre alla quota globale API e al corpo massimo, ora sono limitati job chat attivi/storico e code upload. Questi limiti non sostituiscono un budget di inferenza.

## Aperto (non corretto, e perche')
1. **ALTO - aggiornamento automatico non firmato.** `POST /admin/pull-restart` (token separato, off di default) esegue `git pull --rebase origin main` e riavvia (`scripts/pull_restart_10b.ps1`). Chi compromette il repo o l'account GitHub ottiene esecuzione di codice su ogni macchina con il token attivo. Serve `git verify-commit` con chiave pubblica fissata nello script (non testabile qui: PowerShell/Windows). Prima del rilascio: o firmare i commit e verificare, o togliere l'endpoint dalla versione pubblica.
2. **MEDIO - modalita' client senza autenticazione locale**: ora blocca le pagine web (`Host`/`Origin`, `attacco-3110`), ma qualunque PROCESSO locale dello stesso PC usa ancora la chiave del server remoto. Serve un token locale se il PC e' condiviso.
3. **Aggiornato 01/11/2026 - protocollo "collegato a Kairos"**: implementato da Sinergo; vedere la sezione dedicata sotto. La revisione precedente copriva il server a chiave unica.
2. **MEDIO - modalita' client (`cliente.py`) senza autenticazione locale**, in ascolto su 127.0.0.1 con la chiave del server remoto allegata: qualunque processo locale o pagina web (CSRF/DNS rebinding) puo' usarla. Serve controllo dell'header `Host` e di `Origin`.
3. **Aggiornato 01/11/2026 - Sinergo**: abbinamento finale, canale firmato, anti-replay, comandi con chiave distinta e registro locale implementati. Lo `scollega` chiude il WebSocket attivo (Kairos azzera il visto); il protocollo non definisce un messaggio firmato di cancellazione del registro remoto. DPAPI e ACL Windows sono verificati con simulazioni, non su un sistema Windows.
4. **BASSO - CORS `*` di default** (`config.ALLOW_ORIGINS`): senza credenziali, ma restringere nella versione pubblica.
5. **BASSO - dashboard**: carica Bootstrap e Chart.js da jsDelivr senza SRI e salva la chiave in localStorage (XSS e chiave nell'URL corretti in `attacco-3110`).
6. **BASSO - un solo livello di privilegio**: la stessa chiave apre `/debug/*`, `/admin/unload`, `/v1/models/unload`. Il rate limit tramite `require_api_key` e' ora globale; le rotte di sola consultazione protette solo dal middleware (es. stato/risultato OCR/media) restano senza quota dedicata.
7. **BASSO - registro richieste** (`REQUESTS_LOG_PATH`) salva i prompt in chiaro su disco, senza rotazione ne' permessi ristretti; il body di `/process` viene letto tutto in memoria (ora limitato da `MAX_BODY_BYTES`).
8. Nota test aggiornata: il fallimento storico di `tests/test_admin_unload.py::test_cooldown_prenotato_prima_della_risposta` nell'ambiente disponibile dipende da `pytest-asyncio` assente. Con il runner AnyIO offline il test passa, senza modificare quel test o il relativo codice.

## Storia git: scansione segreti
`gitleaks` 8.x (docker), 93 commit: 1 sospetto, tipo `generic-api-key`, `CONFIGURAZIONE.md:178`, commit `cdcfd55a` (06/05/2026): una riga `API_KEY=` con valore di 32 caratteri. Nel file attuale la stessa documentazione mostra `API_KEY=xK9...` (esempio), quindi probabilmente un esempio, ma non verificabile da fuori. Ruotare per prudenza la `API_KEY` di ogni macchina (`python installa.py --rigenera-chiave`) e il `PULL_RESTART_HTTP_KEY`. Per la repo pubblica: ripartire da una copia nuova senza storia; riscrivere la storia e' piu' fragile di una copia pulita.

## Sinergo collegato a Kairos (01/11/2026)

L'abbinamento segue `SINERGO_ABBINAMENTO.md`, che sostituisce il vecchio invito:
Sinergo genera il codice Crockford da 60 bit e una coppia Ed25519, mantiene il
codice e K in memoria e autentica con `prova_k` sia `pk_kairos` sia `pk_comandi`.
Le chiavi si salvano solo dopo la verifica a tempo costante. Il formato dei
messaggi firmati e la loro verifica sono copiati identici dal riferimento Kairos.
Le chiavi dei lavori e dei comandi sono distinte; un comando firmato con la
chiave dei lavori viene rifiutato e registrato. Gli id visti restano nella stessa
istanza del client anche quando cambia il socket: massimo 10.000 o 24 ore.

La ricerca DNS-SD dura 10 secondi; se il multicast non funziona si puo' indicare
manualmente Kairos. La pagina con il codice ascolta esclusivamente su 127.0.0.1
(porta 9310 o prima libera), verifica Host/Origin e non usa risorse esterne.
Il codice non e' stampato sul terminale o nei log. Per i WebSocket, HTTP/WS e'
consentito solo sulle reti private, loopback e link-local; tutti gli IP risolti
sono controllati e la connessione usa l'IP verificato. I redirect e i proxy
impliciti sono disabilitati. HTTPS/WSS usa la verifica ordinaria del certificato.
Non esiste piu' il percorso con impronta TLS copiata dall'invito.

I rischi residui della specifica restano:

- Sulla LAN senza TLS le firme impediscono la falsificazione, ma non cifrano
  scheda, lavori, risultati e comandi: un osservatore della rete puo' leggerli.
- Un falso Kairos sulla LAN puo' raccogliere `prova_m` e provare codici offline:
  la difesa e' l'entropia di 60 bit. Puo' inoltre interrompere l'attesa e forzare
  un codice nuovo; DNS-SD non autentica da solo il server.
- Un falso Sinergo puo' comparire tra i computer in attesa. Senza il codice del
  vero Sinergo non puo' usarne la prova, legata a id e chiave della macchina.
- Chi vede il codice sullo schermo durante l'attesa puo' tentare l'abbinamento.
  Anche un processo locale dello stesso utente puo' leggere la pagina loopback
  o la memoria; la pagina non e' una barriera fra utenti/processi locali ostili.
- La compromissione di Kairos **e della chiave comandi** consente programmi
  arbitrari con i permessi dell'utente Sinergo, senza ulteriori conferme. Usare
  un utente senza privilegi amministrativi. La separazione delle chiavi non
  protegge quando entrambe sono compromesse.
- Il registro `comandi.log` viene aperto solo in append e conserva avvio, esito e
  rifiuti. Non e' immutabile contro l'utente proprietario o un attaccante con
  esecuzione di codice come lui; non ha rotazione automatica. Il registro puo'
  contenere argomenti sensibili di comandi validamente autorizzati.
- Il tetto del comando termina il processo diretto, non garantisce la chiusura
  dei suoi discendenti. Un comando gia' avviato puo' continuare fino al proprio
  tetto dopo la revoca del canale; l'output e' troncato nella risposta ma raccolto
  in memoria prima del troncamento. I comandi di manutenzione di Kairos devono
  quindi restare brevi e controllati.
- L'anti-replay e' in memoria: si azzera al riavvio del processo; una cattura
  ancora valida nella finestra temporale puo' essere riproposta dopo un riavvio.
  Il limite di 10.000 id consente inoltre l'espulsione dei piu' vecchi.
- `scollega` rende le chiavi inattive e le conserva come file `.revocato` datati,
  senza cancellare lo storico. Un processo gia' collegato chiude il WebSocket,
  che Kairos interpreta come computer spento; per eliminare anche la voce dal
  registro remoto usare «Togli questo computer» in Kairos. Il protocollo non
  definisce un messaggio di cancellazione remoto dalla macchina.
- Il punto d'incontro per Kairos e Sinergo entrambi dietro router differenti e'
  rinviato: serve un indirizzo di Kairos raggiungibile da Sinergo.

Le letture interrogano soltanto le API LocalRouter locali gia' presenti, con
porta e chiave eventualmente disponibili nell'ambiente del processo, senza
leggere `.env`. Un router assente o non autorizzato produce `null`, cosi' come
le misure di token/s e cache per cui non esiste una fonte di misura. Non si
attribuiscono prestazioni di GPU a questo banco di prova.

Verifiche e mutazioni riproducibili: [rapporto Sinergo](materiale/VERIFICA_SINERGO.md).
I test girano su Linux con server WebSocket loopback, TestClient e programmi
creati dal test. Su Windows sono simulati DPAPI, DACL e registro di avvio;
non si rivendica un collaudo nativo Windows. Il venv e i riferimenti Kairos
restano fuori dal commit. `.env` non e' stato modificato.
