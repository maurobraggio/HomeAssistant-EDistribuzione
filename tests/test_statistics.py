"""Test della logica di import statistiche: statistic_id per direzione
(prelevata/immessa) e aggregazione oraria dei campioni a 15 minuti.

Coperte solo le funzioni pure (_sanitize_statistic_id, _aggrega_per_ora): il
percorso completo async_import_curva_giornaliera richiede il recorder ed è
fuori da questi test.

Lo schema delle risposte usato qui è quello confermato su dati reali e
documentato in documentation/protocol.md.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from custom_components.edistribuzione import statistics as st


def _giorno(
    sample_date: str,
    initial_sample: str,
    valori: list[float],
    *,
    energy_type: str = "A1",
    frequenza: int = 15,
    time_type: str = "CONS",
) -> dict:
    """Un elemento della lista 'data' di querydailyloadprofile."""
    return {
        "readings": {
            "energyType": energy_type,
            "sampleDate": sample_date,
            "sampleValues": [
                {"id": str(i), "val": str(v)} for i, v in enumerate(valori, start=1)
            ],
        },
        "sampleFrequency": frequenza,
        "timeType": time_type,
        "initialSample": initial_sample,
    }


# 1 agosto 2026, ora legale (CEST, UTC+2): il campione id=1 cade a mezzanotte
# locale, quindi initialSample è il 31 luglio alle 22:00 UTC.
AGOSTO = ("20260801", "2026-07-31T22:00:00.000+00:00")


# --- _sanitize_statistic_id -------------------------------------------------

def test_statistic_id_prelevata():
    assert (
        st._sanitize_statistic_id("IT001E00000001")
        == "edistribuzione:it001e00000001_energia"
    )


def test_statistic_id_immessa_ha_suffisso_proprio():
    assert (
        st._sanitize_statistic_id("IT001E00000001", immessa=True)
        == "edistribuzione:it001e00000001_energia_immessa"
    )


def test_le_due_direzioni_sono_serie_distinte():
    pod = "ITP0AE00000002"
    assert st._sanitize_statistic_id(pod) != st._sanitize_statistic_id(pod, immessa=True)


def test_statistic_id_sostituisce_i_caratteri_strani():
    assert st._sanitize_statistic_id("IT-001/E 1") == "edistribuzione:it_001_e_1_energia"
    assert (
        st._sanitize_statistic_id("IT-001/E 1", immessa=True)
        == "edistribuzione:it_001_e_1_energia_immessa"
    )


# --- _aggrega_per_ora -------------------------------------------------------

def test_aggrega_quattro_campioni_in_una_sola_ora():
    risultato = st._aggrega_per_ora([_giorno(*AGOSTO, [0.1, 0.2, 0.3, 0.4])])
    assert len(risultato) == 1
    inizio, kwh = risultato[0]
    assert inizio.isoformat() == "2026-07-31T22:00:00+00:00"  # 00:00 locale
    assert kwh == pytest.approx(1.0)


def test_giornata_intera_da_96_campioni_diventa_24_ore():
    risultato = st._aggrega_per_ora([_giorno(*AGOSTO, [0.25] * 96)])
    assert len(risultato) == 24
    assert all(kwh == pytest.approx(1.0) for _, kwh in risultato)
    assert sum(kwh for _, kwh in risultato) == pytest.approx(24.0)


def test_piu_giorni_nella_stessa_risposta():
    giorni = [
        _giorno(*AGOSTO, [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [2.0] * 96),
    ]
    risultato = st._aggrega_per_ora(giorni)
    assert len(risultato) == 48
    assert sum(kwh for _, kwh in risultato) == pytest.approx(96.0 + 192.0)
    # Ordinato cronologicamente, indipendentemente dall'ordine di arrivo
    assert risultato == sorted(risultato)


def test_l_aggregazione_non_dipende_dall_energy_type():
    """La direzione la decide il chiamante (quale magnitude ha chiesto), non
    questo modulo: la stessa curva marcata come immessa deve produrre
    esattamente gli stessi bucket."""
    valori = [0.3] * 96
    prelevata = st._aggrega_per_ora([_giorno(*AGOSTO, valori, energy_type="A1")])
    immessa = st._aggrega_per_ora([_giorno(*AGOSTO, valori, energy_type="A2", time_type="PROD")])
    assert prelevata == immessa


def test_curva_fotovoltaica_lascia_a_zero_le_ore_notturne():
    """Forma attesa di una curva di produzione: nulla di notte, positiva
    nelle ore centrali. Verifica che i bucket cadano nell'ora locale giusta,
    non solo che i totali tornino."""
    # 20 campioni (00:00-04:45) a zero, 64 diurni, 12 serali (21:00-23:45) a zero
    valori = [0.0] * 20 + [1.0] * 64 + [0.0] * 12
    per_ora = dict(st._aggrega_per_ora([_giorno(*AGOSTO, valori)]))

    def kwh_alle(ora_utc: str) -> float:
        return per_ora[datetime.fromisoformat(ora_utc)]

    assert kwh_alle("2026-07-31T22:00:00+00:00") == 0.0  # 00:00 locale
    assert kwh_alle("2026-08-01T10:00:00+00:00") == pytest.approx(4.0)  # 12:00 locale
    assert kwh_alle("2026-08-01T21:00:00+00:00") == 0.0  # 23:00 locale


# --- Cambio ora legale ------------------------------------------------------
#
# initialSample è un timestamp UTC assoluto, quindi il calcolo
# initialSample + (id-1)*frequenza non ha bisogno di sapere nulla del DST:
# questi test verificano l'aritmetica del client sul numero di campioni che
# il server dichiara, non quanti ne mandi davvero nel giorno del cambio.

def test_giorno_lungo_di_ottobre_produce_25_ore():
    # 25/10/2026, fine ora legale: 25 ore locali = 100 campioni da 15 minuti
    risultato = st._aggrega_per_ora(
        [_giorno("20261025", "2026-10-24T22:00:00.000+00:00", [1.0] * 100)]
    )
    assert len(risultato) == 25
    assert sum(kwh for _, kwh in risultato) == pytest.approx(100.0)


def test_giorno_corto_di_marzo_produce_23_ore():
    # 29/03/2026, inizio ora legale: 23 ore locali = 92 campioni
    risultato = st._aggrega_per_ora(
        [_giorno("20260329", "2026-03-28T23:00:00.000+00:00", [1.0] * 92)]
    )
    assert len(risultato) == 23
    assert sum(kwh for _, kwh in risultato) == pytest.approx(92.0)


# --- Robustezza -------------------------------------------------------------

def test_campione_corrotto_viene_scartato_senza_perdere_il_resto(caplog):
    valori = [_giorno(*AGOSTO, [1.0, 2.0])]
    valori[0]["readings"]["sampleValues"].append({"id": "3", "val": "non-un-numero"})

    with caplog.at_level(logging.WARNING):
        risultato = st._aggrega_per_ora(valori)

    assert sum(kwh for _, kwh in risultato) == pytest.approx(3.0)
    assert "Campione con id/val non validi" in caplog.text


def test_initial_sample_non_parsabile_salta_il_giorno_e_non_solleva(caplog):
    giorni = [
        _giorno("20260801", "non-una-data", [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [1.0] * 96),
    ]
    with caplog.at_level(logging.WARNING):
        risultato = st._aggrega_per_ora(giorni)

    # Solo il secondo giorno è stato aggregato
    assert len(risultato) == 24
    assert "initialSample non parsabile" in caplog.text


def test_giorno_senza_campioni_viene_ignorato():
    assert st._aggrega_per_ora([{"readings": {}}, {}]) == []


def test_risposta_vuota():
    assert st._aggrega_per_ora([]) == []
