# HomeAssistant-EDistribuzione

Integrazione custom per Home Assistant, dedicata a **E-Distribuzione**.

Per ogni POD configurato importa **entrambe** le direzioni dell'energia come
statistiche esterne, visibili nella Energy Dashboard:

- **prelevata** (consumo dalla rete)
- **immessa** (immissione in rete / produzione fotovoltaica)

Il download automatico usa normalmente le API dati dell'app ufficiale. Se
quel backend risponde con un errore e la configurazione contiene un solo POD,
l'integrazione apre automaticamente una sessione del portale web usando lo
stesso token OAuth, recupera i dati usati dall'esportazione CSV della pagina
"Curve di carico" e li importa nella stessa pipeline. Non occorre scaricare o
caricare file a mano.

Il fallback web non viene attivato su configurazioni con più POD: il
controller Aura del portale non riceve il POD tra i parametri e attribuire la
risposta al contatore sbagliato corromperebbe le statistiche. In quel caso
l'API dell'app resta l'unico backend automatico finché non sarà identificata
e verificata la chiamata con cui il portale seleziona il POD.

Ogni POD ha un **ruolo** configurabile (contatore normale/scambio, oppure
fotovoltaico/produzione): influenza solo i nomi mostrati, non quali dati
vengono scaricati - entrambe le direzioni si acquisiscono sempre, per ogni
POD, a prescindere dal ruolo.

## Installazione

### Tramite HACS (consigliato)

Non è nello store predefinito di HACS: va aggiunto come repository custom.

1. HACS → menu (⋮ in alto a destra) → **Repository personalizzati**
2. URL: `https://github.com/maurobraggio/HomeAssistant-EDistribuzione`,
   categoria **Integrazione**
3. Cerca "**E-Distribuzione**" in HACS → **Scarica**
4. Riavvia Home Assistant

### Manuale (alternativa)

Copia `custom_components/edistribuzione/` nella cartella
`custom_components/` della tua istanza Home Assistant, poi riavvia.

### Configurazione

*Impostazioni → Dispositivi e servizi → Aggiungi integrazione → E-Distribuzione.*

Il metodo consigliato completa l'autorizzazione nel browser e non consegna la
password a Home Assistant:

1. accedi al Portale Clienti E-Distribuzione nel browser;
2. dall'integrazione apri il link di autorizzazione ufficiale;
3. al termine può rimanere una pagina bianca, perché Chrome desktop non
   gestisce il protocollo mobile `eneldist://`; apri la Console JavaScript
   (`⌥⌘J` su macOS), copia dall'errore il collegamento completo
   `eneldist://redirect?...` e incollalo nel modulo Home Assistant;
4. premi **Invia**. Access token e refresh token arrivano nello stesso
   redirect: non sono più necessari una seconda pagina XML o un secondo
   copia/incolla.

Il redirect è legato al tentativo corrente tramite `state` OAuth e viene
rifiutato se appartiene a un flusso precedente. I token vengono conservati
nella config entry di Home Assistant; password e OTP non vengono memorizzati.

Resta disponibile come alternativa il login diretto con email, password e
OTP. La protezione anti-bot di E-Distribuzione può però bloccare le richieste
eseguite dal container: in quel caso l'integrazione propone automaticamente
il percorso nel browser e crea una segnalazione in *Sistema → Riparazioni*.

Per aggiornare le credenziali senza cancellare POD, opzioni o storico:
*Impostazioni → Dispositivi e servizi → E-Distribuzione → Configura →
Riautentica account*.

Una volta configurata, l'acquisizione è automatica: Home Assistant rinnova
l'accesso, scarica le curve, normalizza i dati e aggiorna le statistiche. Non
è richiesto alcun download o caricamento manuale di CSV.

## Configurazione del ruolo POD

*Impostazioni → Dispositivi e servizi → E-Distribuzione → Configura → Tipo
di contatore per POD.* Modificabile in qualunque momento.

## Energy Dashboard

Per un impianto con un contatore di scambio (M1) e uno di produzione (M2):

| Sezione | Statistica |
|---|---|
| Rete → Consumo dalla rete | `edistribuzione:<pod_m1>_energia` |
| Rete → Ritorno alla rete | `edistribuzione:<pod_m1>_energia_immessa` |
| Pannelli solari → Produzione | `edistribuzione:<pod_m2>_energia_immessa` |

`edistribuzione:<pod_m2>_energia` (la prelevata del contatore di
produzione, tipicamente lo stand-by dell'inverter, qualche decimo di kWh al
mese) resta disponibile ma non va in nessuna sezione della dashboard.

Autoconsumo e consumo totale casa sono calcolati automaticamente da Home
Assistant a partire da produzione + immissione + prelievo - non servono
sensori aggiuntivi.

## Architettura: 15 minuti come source of truth

I campioni a 15 minuti restituiti da E-Distribuzione (96/giorno) non vengono
aggregati e scartati: finiscono per primi in un database SQLite proprio
dell'integrazione (`edistribuzione_curve.db`, nella cartella di
configurazione di Home Assistant - un file indipendente, mai lo stesso
database del Recorder). Solo dopo, dai campioni realmente memorizzati, si
ricalcolano i bucket orari e la somma cumulativa che vanno nella Energy
Dashboard:

```
API app / portale web (15')  ->  raw storage (upsert)  ->  bucket orari + sum  ->  Energy Dashboard
```

Questo rende ogni import **idempotente e capace di autocorreggersi**: se
E-Distribuzione rettifica in un secondo momento un campione già scaricato
(succede), un nuovo `recupera_storico` sullo stesso periodo sovrascrive
quel campione (stesso POD, stessa direzione, stesso istante) invece di
duplicarlo, e ricalcola da zero sia l'ora toccata sia tutte le somme
cumulative successive - il risultato finale non dipende dall'ordine in cui
storico, retry e rettifiche sono arrivati.

Per le stesse ragioni, il ciclo automatico giornaliero non richiede più solo
il giorno precedente: ricontrolla sempre gli ultimi `GIORNI_RICONTROLLO`
giorni (3 di default, in una sola richiesta per direzione, non una per
giorno), così una rettifica recente viene vista da sola senza dover lanciare
`recupera_storico` a mano.

## Recupero storico

Azione `edistribuzione.recupera_storico(device_id, data_da, data_a)`: una
sola richiesta per direzione per l'intero periodo (confermato funzionante
fino a 181 giorni in un'unica risposta). Scegliendo il dispositivo di un
singolo POD il recupero si limita a quello; scegliendo il dispositivo
"E-Distribuzione" (account) copre tutti i POD configurati.

Rilanciarlo sullo stesso periodo è sempre sicuro: aggiorna/corregge invece
di duplicare (vedi sopra).

## Verificare il protocollo prima di fidarsi dei dati

```bash
pip install -r requirements_test.txt
python scripts/verify_login.py
```

Lo script fa login (email/password/OTP), elenca i POD dell'account e sonda
diversi candidati per `magnitude` sull'endpoint dati, confrontando i totali
per scoprire quale restituisce l'energia immessa. Se un candidato risulta
corretto, aggiorna **solo** `MAGNITUDE_IMMESSA` in
`custom_components/edistribuzione/const.py`.

## Sviluppo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components/ tests/ scripts/
```

## Origine

Il protocollo (OAuth2 Salesforce, login OTP, client REST MuleSoft e fallback
Aura del portale per i dati) è stato reverse-engineered per l'integrazione
multi-distributore
[HomeAssistant-Contatore](https://github.com/riccardorossi92/HomeAssistant-Contatore),
che resta la scelta giusta per chi ha anche Duereti, Unareti o Areti. Questo
repository è dedicato solo a E-Distribuzione, con supporto nativo per
prelevata/immessa separate e un ruolo configurabile per POD.
