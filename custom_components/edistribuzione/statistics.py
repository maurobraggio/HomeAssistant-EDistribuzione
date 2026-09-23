"""Import delle curve E-Distribuzione come external statistics in Home Assistant.

Schema JSON di una risposta di ApiClient.async_get_daily_load_profile:

    [
      {
        "readings": {
          "energyType": "A1",
          "sampleDate": "20260801",
          "sampleValues": [
            {"id": "1", "val": "0.359"},
            ...
            {"id": "96", "val": "0.020"}
          ]
        },
        "sampleFrequency": 15,
        "timeType": "CONS",
        "initialSample": "2026-07-31T22:00:00.000+00:00"
      }
    ]

'initialSample' è un timestamp UTC assoluto e completo del campione id=1:
il timestamp di ogni campione si ottiene sommando (id-1) * sampleFrequency
minuti - deterministico, nessuna interpretazione di flag per il cambio ora
richiesta. Verificato aritmeticamente su dati reali: id=1 cade esattamente a
mezzanotte locale del giorno richiesto, id=96 sull'ultimo quarto d'ora dello
stesso giorno locale.

'val' è energia in kWh per intervallo di 15 minuti, non potenza media in kW -
verificato confrontando il totale della curva per un mese intero con il
delta di due letture ufficiali consecutive (async_get_reading), stessa cifra
fino alla terza cifra decimale.

Ogni POD ha DUE serie distinte, una per direzione dell'energia:

    edistribuzione:<pod>_energia            prelevata (MAGNITUDE_PRELEVATA)
    edistribuzione:<pod>_energia_immessa    immessa (MAGNITUDE_IMMESSA)

La direzione la decide chi chiama async_import_curva_giornaliera (quale
magnitude ha chiesto all'API), non questo modulo: qui non si interpreta
'energyType' per instradare i dati, solo per fidarsi di chi ci passa i dati
già separati per direzione - così l'aggregazione resta identica in entrambi i
casi e più facile da testare.

Le due direzioni NON vanno confuse con il RUOLO del POD scelto dall'utente
(contatore di scambio o di produzione): la direzione dice cosa ha misurato
E-Distribuzione, il ruolo dice cosa rappresenta quel contatore nell'impianto.
Il ruolo arriva dal chiamante tramite il parametro 'nome' e influenza solo
l'etichetta visibile, mai quali dati vengono scritti.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _sanitize_statistic_id(pod: str, *, immessa: bool = False) -> str:
    """Uno statistic_id per POD e direzione."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    suffisso = "_energia_immessa" if immessa else "_energia"
    return f"{DOMAIN}:{slug}{suffisso}"


def statistic_ids(pod: str) -> tuple[str, str]:
    """(prelevata, immessa) statistic_id per questo POD - punto di accesso
    pubblico per chi (es. energy_dashboard.py) deve sapere quali statistiche
    esistono per un POD senza replicare la logica di naming."""
    return _sanitize_statistic_id(pod), _sanitize_statistic_id(pod, immessa=True)


def _aggrega_per_ora(giorni: list[dict]) -> list[tuple[datetime, float]]:
    """Aggrega i campioni a 15 minuti di uno o più giorni in bucket orari.

    Riceve la lista grezza restituita da ApiClient.async_get_daily_load_profile
    (un dict per giorno richiesto, tutti della stessa direzione: vedi il
    docstring del modulo per lo schema atteso e come si calcola il timestamp
    di ogni campione).

    Righe con campi mancanti o non parsabili vengono scartate con un warning
    invece di far fallire l'intero import: un singolo campione corrotto non
    deve perdere il resto della giornata.

    Restituisce una lista di (inizio_ora_utc_aware, kwh_totali) ordinata.
    """
    bucket: dict[datetime, float] = defaultdict(float)

    for giorno in giorni:
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
            inizio_ora = ts_utc.replace(minute=0, second=0, microsecond=0)
            bucket[inizio_ora] += valore_kwh

    return sorted(bucket.items())


async def _leggi_serie_esistente(hass: HomeAssistant, statistic_id: str) -> dict:
    """Rilegge tutta la serie oraria già presente per uno statistic_id,
    {inizio_ora_utc: kwh_dell_ora}."""
    inizio_epoca = dt_util.utc_from_timestamp(0)
    esistenti = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        inizio_epoca,
        None,
        {statistic_id},
        "hour",
        None,
        {"state"},
    )

    serie: dict = {}
    for riga in esistenti.get(statistic_id, []):
        stato = riga.get("state")
        if stato is None:
            continue
        serie[dt_util.utc_from_timestamp(riga["start"])] = float(stato)
    return serie


async def async_import_curva_giornaliera(
    hass: HomeAssistant,
    pod: str,
    dati_grezzi: list[dict],
    *,
    immessa: bool = False,
    nome: str | None = None,
) -> date | None:
    """Importa i campioni a 15 minuti di una direzione come external
    statistics, aggregandoli in bucket orari.

    'immessa' sceglie la serie di destinazione (vedi _sanitize_statistic_id);
    'nome' è l'etichetta mostrata nella Energy Dashboard, che dipende dal
    ruolo assegnato al POD e arriva quindi dal chiamante.

    Rilegge la serie esistente, la fonde con i nuovi dati (quelli nuovi hanno
    la precedenza in caso di sovrapposizione) e ricalcola tutte le somme
    progressive da zero, così l'ordine di importazione (storico prima o dopo
    i dati recenti) non influisce sul risultato finale.

    Restituisce la data locale dell'ultimo punto della serie risultante, o
    None se non c'è nulla da importare.
    """
    if not dati_grezzi:
        _LOGGER.debug("Nessun dato curva da importare per POD %s (immessa=%s)", pod, immessa)
        return None

    statistic_id = _sanitize_statistic_id(pod, immessa=immessa)
    nuove_ore = dict(_aggrega_per_ora(dati_grezzi))

    if not nuove_ore:
        _LOGGER.warning(
            "POD %s (immessa=%s): nessun campione valido trovato nella "
            "risposta (schema cambiato?). Risposta grezza: %r",
            pod,
            immessa,
            dati_grezzi,
        )
        return None

    _LOGGER.debug(
        "POD %s (immessa=%s): %d campioni a 15 minuti aggregati in %d ore (%s -> %s)",
        pod,
        immessa,
        sum(len(g.get("readings", {}).get("sampleValues", [])) for g in dati_grezzi),
        len(nuove_ore),
        min(nuove_ore).isoformat(),
        max(nuove_ore).isoformat(),
    )

    serie = await _leggi_serie_esistente(hass, statistic_id)
    ore_gia_presenti = len(serie)
    serie.update(nuove_ore)

    running_sum = 0.0
    stats = []
    for inizio_ora in sorted(serie):
        running_sum += serie[inizio_ora]
        stats.append({"start": inizio_ora, "state": serie[inizio_ora], "sum": running_sum})

    metadata = {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        # Riscritto a ogni import: cambiare il ruolo del POD nelle opzioni si
        # propaga da solo al primo aggiornamento successivo, senza migrazioni.
        "name": nome or f"E-Distribuzione {pod}{' (immessa)' if immessa else ''}",
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }

    async_add_external_statistics(hass, metadata, stats)
    ultima_data = dt_util.as_local(stats[-1]["start"]).date()
    _LOGGER.info(
        "POD %s (%s): %d ore nuove/aggiornate, serie riscritta con %d ore totali "
        "(erano %d), ultimo punto %s",
        pod,
        statistic_id,
        len(nuove_ore),
        len(stats),
        ore_gia_presenti,
        ultima_data.isoformat(),
    )
    return ultima_data


async def _ultima_data_serie(hass: HomeAssistant, statistic_id: str) -> date | None:
    """Ultima data (locale) presente in una singola serie, o None se vuota."""
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entry = last_stats.get(statistic_id)
    if not entry:
        return None

    start = entry[0].get("start")
    if start is None:
        return None

    return dt_util.as_local(dt_util.utc_from_timestamp(start)).date()


async def async_get_ultima_data_disponibile(hass: HomeAssistant, pod: str) -> date | None:
    """Ultima data (locale) effettivamente presente nelle external statistics
    per il POD, o None se non c'è ancora nessun dato importato.

    Guarda ENTRAMBE le direzioni e restituisce la più avanzata: un POD di
    sola produzione può non avere niente nella serie prelevata, e guardando
    solo quella il coordinator ne concluderebbe che non è mai arrivato nulla,
    richiedendo ogni giorno un intervallo già importato. Le due direzioni
    arrivano dalla stessa richiesta, quindi normalmente avanzano insieme: il
    max serve per il caso in cui una delle due non esista.
    """
    date_per_direzione = [
        await _ultima_data_serie(hass, _sanitize_statistic_id(pod, immessa=immessa))
        for immessa in (False, True)
    ]
    presenti = [d for d in date_per_direzione if d is not None]
    return max(presenti) if presenti else None
