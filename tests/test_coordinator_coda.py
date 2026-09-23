"""Coda dei giorni da riprovare del coordinator (per-POD): abbandono a
tempo (non a conteggio tentativi) e "giorno ricevuto se almeno una
direzione lo ha restituito".
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from freezegun import freeze_time

from custom_components.edistribuzione.const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_GIORNI_DA_RIPROVARE,
)

from .conftest import POD_A, POD_B

OGGI = "2026-09-15 12:00:00"


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


async def test_abbandona_i_giorni_troppo_vecchi_per_pod(hass, make_edist_coordinator):
    vecchio = (date(2026, 9, 15) - timedelta(days=ABBANDONO_CODA_DOPO_GIORNI)).isoformat()
    recente = "2026-09-14"
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={
            CONF_GIORNI_DA_RIPROVARE: {
                POD_A: {"2026-08-20": vecchio, "2026-09-12": recente},
            }
        },
    )

    with freeze_time(OGGI):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 13))
        code = coordinator._leggi_code()

    assert set(code[POD_A]) == {"2026-09-12", "2026-09-13"}


async def test_rimuovi_dalla_coda_lascia_intatti_gli_altri_pod(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(
        pods=[POD_A, POD_B],
        data={
            CONF_GIORNI_DA_RIPROVARE: {
                POD_A: {"2026-09-10": "2026-09-10"},
                POD_B: {"2026-09-10": "2026-09-10"},
            }
        },
    )

    with freeze_time(OGGI):
        coordinator._rimuovi_dalla_coda(POD_A, [date(2026, 9, 10)])
        code = coordinator._leggi_code()

    assert POD_A not in code
    assert set(code[POD_B]) == {"2026-09-10"}


async def test_valore_non_data_degrada_a_oggi(hass, make_edist_coordinator):
    """Un valore corrotto nelle opzioni persistite non deve far esplodere la
    lettura della coda: si assume 'oggi' come data di primo inserimento."""
    coordinator = make_edist_coordinator(
        pods=[POD_A], data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {"2026-09-10": "non-una-data"}}}
    )

    with freeze_time(OGGI):
        code = coordinator._leggi_code()

    assert code == {POD_A: {"2026-09-10": date(2026, 9, 15)}}
