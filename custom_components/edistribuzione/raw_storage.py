"""Storage dei campioni a 15 minuti (source of truth) per POD e direzione.

Un file SQLite indipendente dal database del Recorder - mai lo stesso file:
vogliamo poter ricostruire le external statistics anche se il Recorder perde
i suoi dati (già successo una volta, DB corrotto), senza dover richiedere di
nuovo lo storico a E-Distribuzione, e senza nessuna contesa sul file del
Recorder. Schema minimo, una tabella:

    campioni(pod, direzione, timestamp_utc, kwh)
    PRIMARY KEY (pod, direzione, timestamp_utc)

L'upsert su questa chiave è la source of truth per le correzioni: se
E-Distribuzione rettifica un campione già importato, la stessa riga viene
sovrascritta, non duplicata.

Ogni funzione qui è sincrona (bloccante, sqlite3 stdlib) - il chiamante
(statistics.py) la esegue via hass.async_add_executor_job, mai direttamente
nell'event loop. Nessuna dipendenza da Home Assistant se non dt_util per il
calcolo del timestamp (stessa aritmetica già usata altrove nel progetto),
così resta testabile con un semplice tmp_path, senza fixture 'hass'.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campioni (
    pod TEXT NOT NULL,
    direzione TEXT NOT NULL,
    timestamp_utc INTEGER NOT NULL,
    kwh REAL NOT NULL,
    PRIMARY KEY (pod, direzione, timestamp_utc)
)
"""

NOME_FILE_DEFAULT = "edistribuzione_curve.db"


def percorso_predefinito(hass: HomeAssistant) -> str:
    """File dedicato dentro la cartella di configurazione, a fianco (non
    dentro) home-assistant_v2.db - un file nostro, un solo scrittore
    (questa integrazione), nessuna condivisione col Recorder."""
    return hass.config.path(NOME_FILE_DEFAULT)


def _connetti(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5)
    # Difesa standard contro "database is locked" se mai due chiamate si
    # sovrappongono (ogni chiamata apre/chiude la propria connessione breve,
    # non ne condivide una tra thread): attende invece di fallire subito.
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute(_SCHEMA)
    return con


def estrai_campioni(dati_grezzi: list[dict]) -> list[tuple[datetime, float]]:
    """Estrae (timestamp_utc, kwh) da una risposta di
    ApiClient.async_get_daily_load_profile - un dict per giorno, ciascuno
    con fino a 96 campioni da 15 minuti.

    Il timestamp di ogni campione è initialSample (UTC assoluto, id=1) +
    (id-1) * sampleFrequency minuti - nessuna logica manuale di ora legale,
    il server gestisce già il cambio nel timestamp assoluto (vedi il
    docstring di statistics.py per la verifica aritmetica su dati reali).

    Righe con campi mancanti o non parsabili vengono scartate con un
    warning invece di far fallire l'intero import: un singolo campione
    corrotto non deve perdere il resto della giornata.
    """
    campioni: list[tuple[datetime, float]] = []

    for giorno in dati_grezzi:
        readings = giorno.get("readings", {})
        sample_values = readings.get("sampleValues", [])
        frequenza_minuti = giorno.get("sampleFrequency")
        initial_sample = giorno.get("initialSample")

        if not sample_values or not frequenza_minuti or not initial_sample:
            _LOGGER.debug(
                "Giorno senza dati utilizzabili (sampleValues/sampleFrequency/"
                "initialSample mancanti o vuoti): %r",
                {
                    "sampleValues": bool(sample_values),
                    "sampleFrequency": frequenza_minuti,
                    "initialSample": initial_sample,
                },
            )
            continue

        try:
            inizio_campione_1 = datetime.fromisoformat(initial_sample)
        except ValueError:
            _LOGGER.warning(
                "initialSample non parsabile come data ISO, giorno saltato: %r",
                initial_sample,
            )
            continue

        for campione in sample_values:
            try:
                indice = int(campione["id"])
                valore_kwh = float(campione["val"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Campione con id/val non validi, saltato: %r", campione)
                continue

            ts_utc = dt_util.as_utc(
                inizio_campione_1 + timedelta(minutes=(indice - 1) * frequenza_minuti)
            )
            campioni.append((ts_utc, valore_kwh))

    return campioni


def upsert_campioni(
    db_path: str, pod: str, direzione: str, campioni: list[tuple[datetime, float]]
) -> None:
    """Scrive i campioni, sovrascrivendo il valore se (pod, direzione,
    timestamp) esiste già - è così che una rettifica successiva di
    E-Distribuzione sostituisce il valore vecchio invece di affiancarlo."""
    if not campioni:
        return
    righe = [(pod, direzione, int(ts.timestamp()), kwh) for ts, kwh in campioni]
    con = _connetti(db_path)
    try:
        with con:
            con.executemany(
                """
                INSERT INTO campioni (pod, direzione, timestamp_utc, kwh)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pod, direzione, timestamp_utc) DO UPDATE SET kwh = excluded.kwh
                """,
                righe,
            )
    finally:
        con.close()


def leggi_campioni(db_path: str, pod: str, direzione: str) -> list[tuple[datetime, float]]:
    """Tutti i campioni mai importati per questo POD/direzione, ordinati per
    timestamp - è la source of truth da cui statistics.py ricostruisce da
    zero l'intera serie oraria e la sum cumulativa ad ogni import, così il
    risultato non dipende dall'ordine di arrivo di storico/rettifiche/retry."""
    con = _connetti(db_path)
    try:
        righe = con.execute(
            "SELECT timestamp_utc, kwh FROM campioni WHERE pod = ? AND direzione = ? "
            "ORDER BY timestamp_utc",
            (pod, direzione),
        ).fetchall()
    finally:
        con.close()
    return [(datetime.fromtimestamp(ts, tz=UTC), kwh) for ts, kwh in righe]
