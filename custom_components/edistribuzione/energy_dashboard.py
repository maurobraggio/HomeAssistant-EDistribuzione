"""Configurazione automatica (idempotente) della Energy Dashboard.

Aggiunge alle sorgenti della Energy Dashboard le statistiche dei POD
configurati, in base al ruolo scelto per ciascuno (vedi
EdistribuzioneCoordinator.tipo_pod):

- scambio: una sorgente "grid" con la prelevata come import e l'immessa
  come export;
- produzione: una sorgente "solar" con l'immessa come produzione (la
  prelevata di un POD di produzione resta disponibile ma non va in Energy
  Dashboard - tipicamente lo stand-by dell'inverter, non un consumo reale).

Non sovrascrive né duplica: se esiste già una sorgente con lo stesso
statistic_id di import (grid) o di produzione (solar), viene lasciata
intatta - comprese eventuali configurazioni di costo che l'utente ha
aggiunto a mano nell'interfaccia. Richiamabile più volte in sicurezza.
"""
from __future__ import annotations

import logging

from homeassistant.components.energy.data import (
    EnergyPreferencesUpdate,
    SourceType,
    async_get_manager,
)
from homeassistant.core import HomeAssistant

from .const import TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .statistics import statistic_ids

_LOGGER = logging.getLogger(__name__)


async def async_configura_energy_dashboard(
    hass: HomeAssistant, coordinators: list[EdistribuzioneCoordinator]
) -> list[str]:
    """Aggiunge le sorgenti mancanti per tutti i POD dei coordinator passati.

    Ritorna le etichette (POD + ruolo) effettivamente aggiunte; vuota se non
    c'era nulla da aggiungere (già tutto configurato in precedenza).
    """
    manager = await async_get_manager(hass)
    sorgenti: list[SourceType] = list(manager.data["energy_sources"]) if manager.data else []

    # Un POD è "già configurato" se il suo statistic_id compare come import
    # (grid o solar) o come export (grid) di una sorgente esistente,
    # indipendentemente da chi l'abbia creata (questa azione o l'utente a
    # mano): non ha senso aggiungerne una seconda per lo stesso dato.
    from_esistenti = {
        s.get("stat_energy_from") for s in sorgenti if s.get("type") in ("grid", "solar")
    }
    to_esistenti = {s.get("stat_energy_to") for s in sorgenti if s.get("type") == "grid"}

    aggiunte: list[str] = []
    for coordinator in coordinators:
        for pod in coordinator.pods:
            prelevata, immessa = statistic_ids(pod)
            ruolo = coordinator.tipo_pod(pod)

            if ruolo == TIPO_POD_PRODUZIONE:
                if immessa in from_esistenti:
                    continue
                sorgenti.append({
                    "type": "solar",
                    "stat_energy_from": immessa,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(immessa)
                aggiunte.append(f"{pod} (produzione)")
            else:
                if prelevata in from_esistenti or immessa in to_esistenti:
                    continue
                sorgenti.append({
                    "type": "grid",
                    "stat_energy_from": prelevata,
                    "stat_energy_to": immessa,
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(prelevata)
                to_esistenti.add(immessa)
                aggiunte.append(f"{pod} (scambio)")

    if not aggiunte:
        _LOGGER.info(
            "Energy Dashboard: nessuna sorgente nuova da aggiungere, già tutto configurato"
        )
        return aggiunte

    update: EnergyPreferencesUpdate = {"energy_sources": sorgenti}
    await manager.async_update(update)
    _LOGGER.info("Energy Dashboard: aggiunte le sorgenti per %s", ", ".join(aggiunte))
    return aggiunte
