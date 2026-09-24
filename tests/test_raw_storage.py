"""Test di raw_storage.py: parsing dei campioni a 15 minuti dalla risposta
API, e upsert/lettura sul file SQLite (source of truth per POD/direzione).

Nessuna fixture 'hass' qui: ogni funzione prende un db_path esplicito, un
semplice file in tmp_path basta per testarle in isolamento.
"""
from __future__ import annotations

import logging
from datetime import datetime

from custom_components.edistribuzione import raw_storage as rs


def _giorno(
    sample_date: str,
    initial_sample: str,
    valori: list[float],
    *,
    frequenza: int = 15,
) -> dict:
    """Un elemento della lista 'data' di querydailyloadprofile."""
    return {
        "readings": {
            "energyType": "A1",
            "sampleDate": sample_date,
            "sampleValues": [
                {"id": str(i), "val": str(v)} for i, v in enumerate(valori, start=1)
            ],
        },
        "sampleFrequency": frequenza,
        "timeType": "CONS",
        "initialSample": initial_sample,
    }


# 1 agosto 2026, ora legale (CEST, UTC+2): il campione id=1 cade a mezzanotte
# locale, quindi initialSample è il 31 luglio alle 22:00 UTC.
AGOSTO = ("20260801", "2026-07-31T22:00:00.000+00:00")


# --- estrai_campioni ---------------------------------------------------------

def test_estrae_un_campione_per_ogni_valore_con_timestamp_corretto():
    campioni = rs.estrai_campioni([_giorno(*AGOSTO, [0.1, 0.2, 0.3, 0.4])])
    assert len(campioni) == 4
    ts0, kwh0 = campioni[0]
    assert ts0.isoformat() == "2026-07-31T22:00:00+00:00"  # 00:00 locale
    assert kwh0 == 0.1
    ts3, kwh3 = campioni[3]
    assert ts3.isoformat() == "2026-07-31T22:45:00+00:00"  # 00:45 locale
    assert kwh3 == 0.4


def test_giornata_intera_produce_96_campioni():
    campioni = rs.estrai_campioni([_giorno(*AGOSTO, [0.25] * 96)])
    assert len(campioni) == 96
    assert sum(kwh for _, kwh in campioni) == 24.0


def test_piu_giorni_nella_stessa_risposta():
    giorni = [
        _giorno(*AGOSTO, [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [2.0] * 96),
    ]
    campioni = rs.estrai_campioni(giorni)
    assert len(campioni) == 192
    assert sum(kwh for _, kwh in campioni) == 96.0 + 192.0


# --- Cambio ora legale --------------------------------------------------------
#
# initialSample è un timestamp UTC assoluto, quindi il calcolo
# initialSample + (id-1)*frequenza non ha bisogno di sapere nulla del DST.

def test_giorno_lungo_di_ottobre_produce_100_campioni():
    # 25/10/2026, fine ora legale: 25 ore locali = 100 campioni da 15 minuti
    campioni = rs.estrai_campioni(
        [_giorno("20261025", "2026-10-24T22:00:00.000+00:00", [1.0] * 100)]
    )
    assert len(campioni) == 100
    assert sum(kwh for _, kwh in campioni) == 100.0


def test_giorno_corto_di_marzo_produce_92_campioni():
    # 29/03/2026, inizio ora legale: 23 ore locali = 92 campioni
    campioni = rs.estrai_campioni(
        [_giorno("20260329", "2026-03-28T23:00:00.000+00:00", [1.0] * 92)]
    )
    assert len(campioni) == 92
    assert sum(kwh for _, kwh in campioni) == 92.0


# --- Robustezza ---------------------------------------------------------------

def test_campione_corrotto_viene_scartato_senza_perdere_il_resto(caplog):
    giorno = _giorno(*AGOSTO, [1.0, 2.0])
    giorno["readings"]["sampleValues"].append({"id": "3", "val": "non-un-numero"})

    with caplog.at_level(logging.WARNING):
        campioni = rs.estrai_campioni([giorno])

    assert len(campioni) == 2
    assert sum(kwh for _, kwh in campioni) == 3.0
    assert "Campione con id/val non validi" in caplog.text


def test_initial_sample_non_parsabile_salta_il_giorno_e_non_solleva(caplog):
    giorni = [
        _giorno("20260801", "non-una-data", [1.0] * 96),
        _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [1.0] * 96),
    ]
    with caplog.at_level(logging.WARNING):
        campioni = rs.estrai_campioni(giorni)

    # Solo il secondo giorno è stato estratto
    assert len(campioni) == 96
    assert "initialSample non parsabile" in caplog.text


def test_giorno_senza_campioni_viene_ignorato():
    assert rs.estrai_campioni([{"readings": {}}, {}]) == []


def test_risposta_vuota():
    assert rs.estrai_campioni([]) == []


# --- upsert_campioni / leggi_campioni ----------------------------------------

def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_upsert_poi_lettura_ritorna_gli_stessi_campioni(tmp_path):
    db = str(tmp_path / "curve.db")
    campioni = [(_ts("2026-08-01T00:00:00+00:00"), 0.5), (_ts("2026-08-01T01:00:00+00:00"), 0.3)]

    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == campioni


def test_upsert_dello_stesso_timestamp_sovrascrive_non_duplica(tmp_path):
    """Rettifica: E-Distribuzione restituisce un valore diverso per un
    campione già importato - deve sostituirlo, non affiancarlo."""
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T14:15:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 300.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 0.3)])

    risultato = rs.leggi_campioni(db, "IT001E00000001", "A1")
    assert risultato == [(ts, 0.3)]


def test_upsert_ripetuto_con_dati_identici_e_idempotente(tmp_path):
    db = str(tmp_path / "curve.db")
    campioni = [(_ts("2026-08-01T00:00:00+00:00"), 1.0), (_ts("2026-08-01T00:15:00+00:00"), 2.0)]

    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)
    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)
    rs.upsert_campioni(db, "IT001E00000001", "A1", campioni)

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == campioni


def test_direzioni_diverse_sono_serie_indipendenti(tmp_path):
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T00:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 1.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A2", [(ts, 9.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(ts, 1.0)]
    assert rs.leggi_campioni(db, "IT001E00000001", "A2") == [(ts, 9.0)]


def test_pod_diversi_sono_serie_indipendenti(tmp_path):
    db = str(tmp_path / "curve.db")
    ts = _ts("2026-08-01T00:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(ts, 1.0)])
    rs.upsert_campioni(db, "ITP0AE00000002", "A1", [(ts, 9.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(ts, 1.0)]
    assert rs.leggi_campioni(db, "ITP0AE00000002", "A1") == [(ts, 9.0)]


def test_upsert_vuoto_non_fa_nulla(tmp_path):
    db = str(tmp_path / "curve.db")
    rs.upsert_campioni(db, "IT001E00000001", "A1", [])
    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == []


def test_lettura_e_ordinata_per_timestamp_indipendentemente_dall_ordine_di_scrittura(tmp_path):
    db = str(tmp_path / "curve.db")
    tardi = _ts("2026-08-01T10:00:00+00:00")
    presto = _ts("2026-08-01T05:00:00+00:00")

    rs.upsert_campioni(db, "IT001E00000001", "A1", [(tardi, 1.0)])
    rs.upsert_campioni(db, "IT001E00000001", "A1", [(presto, 2.0)])

    assert rs.leggi_campioni(db, "IT001E00000001", "A1") == [(presto, 2.0), (tardi, 1.0)]


def test_leggi_campioni_pod_inesistente_ritorna_lista_vuota(tmp_path):
    db = str(tmp_path / "curve.db")
    assert rs.leggi_campioni(db, "IT999E99999999", "A1") == []
