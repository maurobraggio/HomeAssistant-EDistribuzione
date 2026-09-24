"""_prossima_richiesta: quale intervallo chiedere per un POD in questo ciclo.

Copre due comportamenti distinti:

- Regressione trovata in test live con due POD sulla stessa entry: solo il
  primo POD della lista faceva il fetch immediato di verifica al primo
  avvio, perché quella decisione era gestita da un flag CONDIVISO per
  l'intera entry (CONF_DATA_INSTALLAZIONE) invece che da una condizione
  per-POD ("questo POD ha già dei dati?"). Con due POD entrambi senza dati,
  il secondo restava senza nulla fino all'orario configurato (default
  19:00), perché quando il ciclo arrivava a lui il flag era già stato
  impostato dal primo.

- Il ricontrollo periodico degli ultimi GIORNI_RICONTROLLO giorni (non solo
  il più recente): E-Distribuzione può rettificare un giorno già
  pubblicato. Il throttle "al massimo una richiesta al giorno per POD" resta
  quello di sempre (confronto tra ultima_disponibile e 'atteso', che non
  cambia finché non cambia il giorno) - cambia solo l'AMPIEZZA della
  finestra richiesta quando scatta.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.edistribuzione.const import CONF_DATA_INSTALLAZIONE, GIORNI_RICONTROLLO

from .conftest import POD_A, POD_B

OGGI = "2026-09-23 15:00:00"  # prima delle 19:00 (ORA_MINIMA_RICHIESTA di default)
OGGI_DOPO_ORARIO = "2026-09-23 20:00:00"  # dopo le 19:00
ATTESO = date(2026, 9, 22)  # oggi - RITARDO_DATI_GIORNI
INIZIO_RICONTROLLO = ATTESO - timedelta(days=GIORNI_RICONTROLLO - 1)  # 2026-09-20


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    """L'ora richiesta (_ora_richiesta) è confrontata contro adesso.hour nel
    fuso configurato: senza fissarlo a UTC, il fuso di default dei test
    farebbe scattare o saltare il controllo "dopo le 19:00" in modo
    imprevedibile a seconda della macchina/ambiente."""
    await hass.config.async_set_time_zone("UTC")


def _con_ultima_data(per_pod: dict[str, date | None]):
    async def _fake(hass, pod):
        return per_pod.get(pod)

    return patch(
        "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
        AsyncMock(side_effect=_fake),
    )


async def test_ogni_pod_senza_dati_fa_il_fetch_immediato_anche_prima_dell_ora(
    hass, make_edist_coordinator
):
    """Con due POD entrambi senza dati (primo avvio), ENTRAMBI devono
    chiedere subito la finestra di ricontrollo - non solo il primo della
    lista, e non solo il giorno più recente."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])

    with freeze_time(OGGI), _con_ultima_data({POD_A: None, POD_B: None}):
        richiesta_a = await coordinator._prossima_richiesta(POD_A)
        richiesta_b = await coordinator._prossima_richiesta(POD_B)

    assert richiesta_a == (INIZIO_RICONTROLLO, ATTESO)
    assert richiesta_b == (INIZIO_RICONTROLLO, ATTESO)


async def test_pod_con_dati_aspetta_l_orario_configurato(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta is None


async def test_pod_aggiunto_dopo_su_entry_gia_avviata_fa_comunque_il_fetch_immediato(
    hass, make_edist_coordinator
):
    """Un POD aggiunto dalle opzioni dopo che l'entry esiste già (quindi con
    CONF_DATA_INSTALLAZIONE già impostata da tempo) deve comunque partire
    subito, perché per LUI non c'è ancora nessun dato - questo era esattamente
    il caso che il bug rompeva."""
    coordinator = make_edist_coordinator(
        pods=[POD_A, POD_B], data={CONF_DATA_INSTALLAZIONE: "2026-09-01"}
    )

    with freeze_time(OGGI), _con_ultima_data({POD_A: date(2026, 9, 21), POD_B: None}):
        richiesta_a = await coordinator._prossima_richiesta(POD_A)
        richiesta_b = await coordinator._prossima_richiesta(POD_B)

    assert richiesta_a is None
    assert richiesta_b == (INIZIO_RICONTROLLO, ATTESO)


async def test_dopo_l_orario_richiede_la_finestra_di_ricontrollo_non_solo_atteso(
    hass, make_edist_coordinator
):
    """Passato l'orario configurato, un POD non ancora aggiornato per oggi
    chiede GIORNI_RICONTROLLO giorni fino ad 'atteso', in un'unica
    richiesta - non solo l'ultimo giorno."""
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (INIZIO_RICONTROLLO, ATTESO)


async def test_gia_ricontrollato_oggi_non_richiede_di_nuovo(hass, make_edist_coordinator):
    """Al massimo una richiesta al giorno per POD: se 'ultima_disponibile'
    è già arrivata ad 'atteso' (il ricontrollo di oggi è già avvenuto), i
    cicli orari successivi non richiedono di nuovo, fino al giorno dopo."""
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: ATTESO}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta is None


async def test_giorni_arretrati_piu_vecchi_della_finestra_allargano_l_inizio(
    hass, make_edist_coordinator
):
    """Un giorno arretrato più vecchio della finestra di ricontrollo (es.
    bloccato in coda da un errore precedente) allarga l'intervallo
    all'indietro per includerlo, sempre in un'unica richiesta."""
    from custom_components.edistribuzione.const import CONF_GIORNI_DA_RIPROVARE

    vecchio = (INIZIO_RICONTROLLO - timedelta(days=5)).isoformat()
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {vecchio: "2026-09-15"}}},
    )

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (date.fromisoformat(vecchio), ATTESO)


async def test_giorni_arretrati_dentro_la_finestra_non_la_allargano(
    hass, make_edist_coordinator
):
    """Un giorno arretrato che rientra già nella finestra di ricontrollo
    (es. ieri, rimasto in coda da un ciclo fallito) viene ricoperto dal
    ricontrollo periodico stesso - non serve trattarlo come arretrato."""
    from custom_components.edistribuzione.const import CONF_GIORNI_DA_RIPROVARE

    dentro_la_finestra = (INIZIO_RICONTROLLO + timedelta(days=1)).isoformat()
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {dentro_la_finestra: "2026-09-21"}}},
    )

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (INIZIO_RICONTROLLO, ATTESO)
