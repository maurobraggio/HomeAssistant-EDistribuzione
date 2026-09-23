"""_prossima_richiesta: quale intervallo chiedere per un POD in questo ciclo.

Regressione trovata in test live con due POD sulla stessa entry: solo il
primo POD della lista faceva il fetch immediato di verifica al primo avvio,
perché quella decisione era gestita da un flag CONDIVISO per l'intera entry
(CONF_DATA_INSTALLAZIONE) invece che da una condizione per-POD ("questo POD
ha già dei dati?"). Con due POD entrambi senza dati, il secondo restava
senza nulla fino all'orario configurato (default 19:00), perché quando il
ciclo arrivava a lui il flag era già stato impostato dal primo.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time

from custom_components.edistribuzione.const import CONF_DATA_INSTALLAZIONE

from .conftest import POD_A, POD_B

OGGI = "2026-09-23 15:00:00"  # prima delle 19:00 (ORA_MINIMA_RICHIESTA di default)
ATTESO = date(2026, 9, 22)  # oggi - RITARDO_DATI_GIORNI


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
    chiedere subito il giorno atteso - non solo il primo della lista."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])

    with freeze_time(OGGI), _con_ultima_data({POD_A: None, POD_B: None}):
        richiesta_a = await coordinator._prossima_richiesta(POD_A)
        richiesta_b = await coordinator._prossima_richiesta(POD_B)

    assert richiesta_a == (ATTESO, ATTESO)
    assert richiesta_b == (ATTESO, ATTESO)


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
    assert richiesta_b == (ATTESO, ATTESO)
