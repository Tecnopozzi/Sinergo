# Localrouter — l'intelligenza artificiale che gira in casa

> Documento di presentazione, aggiornato al 16 agosto 2026.
> Descrive che cos'è Localrouter, a che cosa serve, come è fatto e quali sono i
> suoi limiti reali. Tutti i numeri riportati sono misurati sulla macchina, non
> stimati.

---

## 1. In una frase

Localrouter è un server che gira su un normale PC e mette a disposizione, via
internet e in modo sicuro, gli stessi servizi di intelligenza artificiale che
di solito si comprano a consumo da fornitori esterni: conversazione con modelli
linguistici, lettura di documenti scansionati, trascrizione di audio.

La differenza è dove avviene il lavoro. Non su server altrui, ma su una singola
scheda grafica da 16 GB che sta in ufficio.

---

## 2. Il problema che risolve

Le applicazioni che usano l'intelligenza artificiale parlano quasi tutte lo
stesso linguaggio, quello introdotto da OpenAI. Sono abituate a chiedere "dammi
l'elenco dei modelli disponibili" e "rispondi a questa conversazione", e si
aspettano risposte in un formato preciso.

Sul PC di casa, però, i programmi che fanno girare i modelli hanno ciascuno le
proprie abitudini, i propri indirizzi e i propri limiti. Metterli in rete uno
per uno significherebbe modificare ogni applicazione che li usa.

Localrouter risolve questo con un principio semplice: **una sola porta d'ingresso
che parla il linguaggio standard**. Le applicazioni non sanno, e non hanno
bisogno di sapere, che dietro c'è un PC di casa invece di un fornitore
internazionale. Cambiano un indirizzo nella configurazione e tutto il resto
resta identico.

Tre conseguenze pratiche:

- **I dati non escono.** Documenti riservati, registrazioni, conversazioni: tutto
  resta sul disco di casa.
- **Non si paga a consumo.** Il costo è la corrente elettrica, non il numero di
  parole elaborate.
- **Si sceglie liberamente il modello.** Nove modelli diversi, sostituibili in
  qualunque momento, senza dipendere dal catalogo di un fornitore.

---

## 3. Come è fatto: quattro strati

Localrouter si capisce meglio immaginandolo come quattro strati sovrapposti,
dall'esterno verso l'interno.

```
   Le applicazioni che chiedono            (ovunque nel mondo)
              │
              │  HTTPS + chiave segreta
              ▼
   1.  IL TUNNEL                           porta di casa, sempre chiusa
       Cloudflare Tunnel
              │
              ▼
   2.  IL PORTIERE                         controlla, smista, registra
       Gateway Localrouter  (porta 8088)
              │
              ▼
   3.  IL MAGAZZINIERE                     tiene in memoria un modello per volta
       llama-swap  (porta 8093)
              │
              ▼
   4.  IL MOTORE                           calcola davvero
       llama.cpp su GPU NVIDIA 16 GB
```

### Strato 1 — Il tunnel

Il PC non espone nessuna porta su internet: nessun indirizzo pubblico, nessuna
regola aperta sul router. È il PC stesso che apre una connessione verso
l'esterno e la tiene aperta, e le richieste viaggiano dentro quel canale
cifrato. Se il PC si spegne, il portone semplicemente non esiste più.

### Strato 2 — Il portiere

È il cuore del progetto: un programma che riceve ogni richiesta, verifica che
chi chiama abbia la chiave giusta, capisce a quale servizio è destinata, la
inoltra, e annota tutto su un registro. Traduce anche fra dialetti diversi, in
modo che chi chiama veda sempre e solo il formato standard.

### Strato 3 — Il magazziniere

Qui sta l'idea che rende possibile avere nove modelli con una sola scheda
grafica. I modelli sono grandi: da 1,3 a 14,6 GB ciascuno, per un totale di
91,5 GB. Nella memoria della scheda ce ne sta **uno solo alla volta**.

Il magazziniere tiene in memoria il modello richiesto e, quando ne arriva uno
diverso, scarica il precedente e carica il nuovo. Chi chiama non se ne accorge:
vede solo una risposta un po' più lenta, quella volta.

### Strato 4 — Il motore

Il programma che esegue davvero i calcoli sulla scheda grafica, trasformando la
domanda in una risposta parola per parola.

---

## 4. Che cosa sa fare

### Conversazione con modelli linguistici

Il servizio principale. Nove modelli diversi, ciascuno con la sua specialità:
scrivere codice, ragionare su un problema, descrivere immagini, rispondere in
fretta. Le applicazioni possono anche chiedere al modello di **usare strumenti
esterni**: invece di rispondere a parole, il modello dice "chiama questa
funzione con questi dati", e il programma che l'ha interrogato esegue.

### Lettura di documenti scansionati

Si carica un PDF e si riottiene il testo. Il documento viene diviso in pagine,
ogni pagina trasformata in immagine e data in pasto a un modello specializzato
nel leggere. È un lavoro lungo, quindi funziona a commesse: si consegna il
documento, si riceve un numero di pratica, e si torna più tardi a ritirare il
risultato. Si può anche annullare una lavorazione in corso.

### Trascrizione di audio

Registrazioni parlate trasformate in testo, con il sistema Whisper nella sua
versione più accurata, eseguito anch'esso sulla scheda grafica.

### Un cruscotto per capire cosa succede

Una pagina web mostra quali servizi sono raggiungibili, quali modelli ci sono,
quali richieste sono arrivate nell'ultima ora, da chi, per quale modello e
quanto sono durate, con grafici su finestre di un'ora, un giorno e una
settimana.

---

## 5. I nove modelli

Ogni modello dichiara le proprie capacità, e chi chiama le legge per decidere a
chi affidare un compito. Queste capacità sono state **verificate una per una
sul campo**, non dedotte dal nome: un modello che si chiama "coder" potrebbe
non saper chiamare funzioni, e uno il cui nome contiene "qwen3" potrebbe non
ragionare affatto.

| Modello | Peso | Sa chiamare funzioni | Ragiona | Vede immagini | Ruolo |
|---|---:|:---:|:---:|:---:|---|
| gemma-4-26b-a4b-it | 12,8 GB | sì | sì | sì | **produzione**: il modello di tutti i giorni |
| qwen3.6-35b-a3b | 12,4 GB | sì | sì | sì | ragionamento e uso di strumenti |
| qwen3.8-27b | 12,6 GB | sì | sì | sì | alternativa recente |
| qwen3-coder-30b | 13,6 GB | sì | no | no | scrittura di codice |
| gpt-oss-20b | 11,3 GB | sì | sì | no | modello aperto di OpenAI |
| ornith-1.0-9b | 8,9 GB | sì | sì | no | leggero e veloce |
| gemma-4-12b-qat | 6,7 GB | sì | sì | sì | compatto e multimodale |
| qwen3-vl-8b | 5,8 GB | sì | no | **sì** | specialista delle immagini |
| glm-ocr | 1,3 GB | — | no | sì | lettura di documenti |

Totale su disco: **91,5 GB**.

---

## 6. Il vincolo che decide tutto: 16 gigabyte

Ogni scelta tecnica di Localrouter discende da un numero: la scheda grafica ha
**16 GB di memoria** (16.311 MB), e il desktop di Windows ne occupa già circa
1.170 solo per disegnare finestre e icone. Restano circa 15 GB per il modello.

Con un modello caldo in memoria, lo spazio libero misurato è fra **600 e 825
MB**: meno del 5% del totale. È un equilibrio sottile, e quando il margine
scende sotto quella soglia il processo può morire — è successo davvero, la
notte del 15 agosto.

Conseguenze concrete:

**Un modello alla volta.** Non è una limitazione software ma aritmetica: due
modelli da 13 GB in 16 GB non ci stanno.

**Cambiare modello costa da 47 a 64 secondi.** È il tempo di svuotare la memoria
e caricare da disco i gigabyte del modello nuovo. Una volta caldo, lo stesso
modello risponde in meno di un secondo. Chi progetta un'applicazione deve
saperlo: alternare due modelli a ogni domanda significa pagare un minuto ogni
volta.

**La memoria del contesto è limitata.** I modelli ricordano la conversazione
fino a 32.768 parole-simbolo, circa cinquanta pagine di testo. Il modello per
il codice, che è il più grosso, deve accontentarsi di 8.192: alzarlo lasciava
solo 103 MB liberi, troppo pochi per lavorare in sicurezza.

**Velocità.** A modello caldo si producono circa 92-101 parole-simbolo al
secondo, e la domanda viene letta a circa 646 al secondo. Sono numeri
paragonabili a un servizio online, purché non si cambi modello di continuo.

---

## 7. Il ragionamento: potente e costoso

I modelli recenti sanno "pensare prima di parlare": prima della risposta
producono un ragionamento interno, invisibile all'utente ma tutt'altro che
gratuito, perché va calcolato parola per parola.

Il costo misurato è impressionante. Sulla stessa domanda banale:

| | Parole-simbolo prodotte | Tempo |
|---|---:|---:|
| Con il ragionamento acceso | 266 | 3,6 secondi |
| Con il ragionamento spento | 4 | 0,2 secondi |

**Diciotto volte più veloce**, con la stessa risposta esatta. Su una batteria di
prove reale, una categoria di test è passata da 111 secondi a 24.

Ma spegnerlo non è sempre giusto: su compiti difficili il ragionamento fa
guadagnare accuratezza. E soprattutto va spento nel modo corretto. Troncarlo a
metà — dare al modello un tetto di parole per pensare, e interromperlo lì — è la
strada sbagliata: il modello resta a metà del suo processo e smette di chiamare
gli strumenti, con crolli di precisione dal 61% al 3% in una categoria di test.

La via giusta è dirgli in partenza di non pensare affatto. Localrouter fa
passare questa istruzione fino al motore, per ogni singola richiesta: la stessa
applicazione può chiedere una risposta ragionata quando serve accuratezza, e una
immediata quando serve velocità.

C'è un'insidia da conoscere: se si spegne il ragionamento e si concede poco
spazio alla risposta, il pensiero può consumare tutto lo spazio disponibile e
lasciare una risposta **vuota**. Una risposta vuota che arriva senza errori è la
trappola più comune di questo sistema.

---

## 8. Sicurezza

- **Nessuna porta aperta sul router.** Il canale è aperto dall'interno.
- **Chiave segreta obbligatoria** su ogni richiesta.
- **Filtro sui chiamanti automatici**: le richieste che non si presentano come un
  browser vengono rifiutate prima ancora di arrivare al server.
- **Limite di frequenza**: 120 richieste al minuto per chiamante.
- **Registro completo**: ogni richiesta viene annotata con chi l'ha fatta, da
  quale indirizzo, con quale applicazione, quale modello, quanto è durata e come
  è finita.
- **I dati restano in casa.** Nessun documento, audio o conversazione lascia il
  disco locale.

---

## 9. Affidabilità

Un server domestico non ha un reparto tecnico dietro, quindi deve sapersi
rialzare da solo.

- **Riavvio automatico**: quando il PC si accende, i servizi ripartono nell'ordine
  giusto, prima il magazziniere e poi il portiere.
- **Sorveglianza continua**: un controllo periodico verifica che il motore
  risponda, e in caso contrario lo riavvia.
- **Modello sempre caldo**: all'accensione il modello di produzione viene
  precaricato, così il primo utente della giornata non aspetta un minuto.
- **Registro su disco**: gli eventi del magazziniere vengono scritti su file e
  conservati per le ultime dieci sessioni, così un guasto notturno può essere
  ricostruito il giorno dopo.
- **Allineamento verificabile**: uno strumento confronta i modelli installati con
  quelli pubblicati e segnala le differenze, perché i due elenchi possono
  scollarsi in silenzio — è già successo, con cinque modelli su nove invisibili
  dall'esterno.

---

## 10. I numeri da ricordare

| | |
|---|---|
| Modelli disponibili | **9** |
| Spazio occupato dai modelli | **91,5 GB** |
| Memoria della scheda grafica | **16 GB** |
| Memoria libera con un modello caricato | **600-825 MB** |
| Modelli caricati contemporaneamente | **1** |
| Tempo per cambiare modello | **47-64 secondi** |
| Risposta con modello già caricato | **meno di 1 secondo** |
| Velocità di generazione | **92-101 parole-simbolo al secondo** |
| Memoria di conversazione | **32.768 parole-simbolo** (~50 pagine) |
| Guadagno spegnendo il ragionamento | fino a **18 volte** più veloce |
| Limite di frequenza | **120 richieste al minuto** |
| Porte aperte sul router | **0** |

---

## 11. Come si usa, in pratica

Un'applicazione che oggi paga un servizio esterno deve cambiare due righe di
configurazione: l'indirizzo del servizio e la chiave. Poi chiede l'elenco dei
modelli disponibili, sceglie quello adatto leggendo le capacità dichiarate, e
invia le domande esattamente come faceva prima.

Le regole d'oro per chi progetta l'integrazione:

1. **Non alternare i modelli** senza motivo: ogni cambio costa fino a un minuto.
2. **Spegnere il ragionamento** quando serve velocità e il compito è semplice.
3. **Concedere spazio sufficiente alla risposta**, o si rischia di riceverla vuota.
4. **Leggere le capacità dichiarate** invece di indovinare dal nome del modello.
5. **Per lavori lunghi usare le commesse** invece di aspettare al telefono.

---

## 12. Glossario

**Modello linguistico** — Un programma addestrato a prevedere la parola
successiva, capace di conversare, riassumere e scrivere codice.

**GPU / scheda grafica** — Il processore specializzato che esegue i calcoli. La
sua memoria, molto più veloce e molto più piccola di quella del PC, è la risorsa
critica di tutto il sistema.

**Memoria di contesto** — Quanta conversazione il modello riesce a tenere a mente
in una volta sola.

**Parola-simbolo (token)** — L'unità con cui i modelli misurano il testo: circa
tre quarti di una parola italiana.

**Caricare un modello** — Copiare i suoi gigabyte dal disco alla memoria della
scheda grafica. È l'operazione lenta.

**Modello caldo** — Un modello già in memoria, pronto a rispondere subito.

**Chiamata di funzione (tool)** — La capacità di un modello di richiedere
l'esecuzione di un programma esterno invece di rispondere a parole.

**Ragionamento (thinking)** — Il monologo interno che alcuni modelli producono
prima della risposta: migliora l'accuratezza sui problemi difficili, ma costa
tempo.

**Tunnel** — Un canale cifrato aperto dal PC verso l'esterno, che permette di
raggiungerlo da internet senza aprire porte sul router.

---

## 13. Materiale per la presentazione

### Il messaggio centrale

Un PC con una sola scheda grafica da 16 GB, con l'architettura giusta, sostituisce
un abbonamento a un servizio di intelligenza artificiale — e in più tiene i dati
in casa. Il prezzo da pagare non è la qualità, ma un vincolo di memoria che va
rispettato: un modello alla volta.

### Le tre immagini da usare

1. **La casa con una sola porta.** Nessuna finestra aperta sul router: un solo
   passaggio cifrato, sorvegliato da una chiave.
2. **Il magazzino con un banco solo.** Novanta gigabyte di modelli sugli
   scaffali, ma un solo banco da lavoro: per usare un attrezzo diverso bisogna
   riporre quello precedente, e ci vuole un minuto.
3. **Pensare prima di parlare.** Chi riflette a lungo risponde meglio alle
   domande difficili e spreca tempo su quelle facili. Il sistema permette di
   decidere, domanda per domanda.

### Scaletta suggerita per le slide

1. Copertina — Localrouter: l'intelligenza artificiale che gira in casa
2. Il problema — dati fuori casa e costi a consumo
3. La soluzione in una frase
4. L'architettura a quattro strati (schema)
5. I quattro servizi: conversazione, documenti, audio, cruscotto
6. Il catalogo dei nove modelli (tabella)
7. Il vincolo dei 16 GB e la regola del modello unico
8. Il costo di cambiare modello: 47-64 secondi
9. Il ragionamento: 18 volte più lento, quando conviene
10. Sicurezza: zero porte aperte, tutto registrato
11. Affidabilità: si rialza da solo
12. I numeri da ricordare (infografica)
13. Le cinque regole per chi integra

### Dati per l'infografica

Grandezze che rendono bene visivamente: 9 modelli e 91,5 GB contro 16 GB di
memoria disponibile (il rapporto è quasi 6 a 1, ed è il motivo per cui serve il
magazziniere); 600 MB liberi su 16.000 (il margine sottile); 1 secondo contro 60
(modello caldo contro cambio di modello); 0,2 secondi contro 3,6 (ragionamento
spento contro acceso); 0 porte aperte sul router.
