# HomeAssistant-EDistribuzione

Integrazione custom per Home Assistant, dedicata a **E-Distribuzione**.

Per ogni POD configurato importa **entrambe** le direzioni dell'energia come
statistiche esterne, visibili nella Energy Dashboard:

- **prelevata** (consumo dalla rete)
- **immessa** (immissione in rete / produzione fotovoltaica)

Ogni POD ha un **ruolo** configurabile (contatore normale/scambio, oppure
fotovoltaico/produzione): influenza solo i nomi mostrati, non quali dati
vengono scaricati - entrambe le direzioni si acquisiscono sempre, per ogni
POD, a prescindere dal ruolo.

> [!NOTE]
> **Validato su un account reale (23/09/2026).** `MAGNITUDE_IMMESSA = "A2"`
> è confermato: `recupera_storico` su agosto 2026 ha restituito 626.551 kWh
> di immessa e 0.437 kWh di prelevata sul POD di produzione, esattamente i
> valori noti per quel mese. Se il tuo account restituisse numeri
> incoerenti, `scripts/verify_login.py` sonda anche altri candidati.

## Installazione

Copia `custom_components/edistribuzione/` nella cartella
`custom_components/` della tua istanza Home Assistant, poi riavvia.

*Impostazioni → Dispositivi e servizi → Aggiungi integrazione → E-Distribuzione.*

Serve: email e password dell'area clienti E-Distribuzione, e il codice OTP
che ricevi via email o SMS durante la configurazione.

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

## Recupero storico

Azione `edistribuzione.recupera_storico(device_id, data_da, data_a)`: una
sola richiesta per direzione per l'intero periodo (confermato funzionante
fino a 181 giorni in un'unica risposta). Scegliendo il dispositivo di un
singolo POD il recupero si limita a quello; scegliendo il dispositivo
"E-Distribuzione" (account) copre tutti i POD configurati.

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

Il protocollo (login OAuth2+PKCE+OTP via Salesforce, client REST MuleSoft
per i dati) è stato reverse-engineered per l'integrazione multi-distributore
[HomeAssistant-Contatore](https://github.com/maurobraggio/HomeAssistant-Contatore),
che resta la scelta giusta per chi ha anche Duereti, Unareti o Areti. Questo
repository è dedicato solo a E-Distribuzione, con supporto nativo per
prelevata/immessa separate e un ruolo configurabile per POD.
