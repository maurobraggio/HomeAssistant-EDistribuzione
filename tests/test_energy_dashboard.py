"""Configurazione idempotente della Energy Dashboard.

async_configura_energy_dashboard non deve mai sovrascrivere né duplicare
sorgenti esistenti - né quelle create da lei in una chiamata precedente, né
quelle che l'utente ha configurato a mano per altri scopi (es. il gas).
"""
from __future__ import annotations

from homeassistant.components.energy.data import async_get_manager

from custom_components.edistribuzione.const import TIPO_POD_PRODUZIONE, TIPO_POD_SCAMBIO
from custom_components.edistribuzione.energy_dashboard import async_configura_energy_dashboard


class _CoordinatorFinto:
    """Solo ciò che async_configura_energy_dashboard usa davvero: pods e
    tipo_pod - non serve un EdistribuzioneCoordinator vero, con sessioni
    aiohttp e config entry, per testare questa sola funzione."""

    def __init__(self, pods: list[str], ruoli: dict[str, str]) -> None:
        self.pods = pods
        self._ruoli = ruoli

    def tipo_pod(self, pod: str) -> str:
        return self._ruoli.get(pod, TIPO_POD_SCAMBIO)


async def test_aggiunge_grid_per_scambio_e_solar_per_produzione(hass):
    coordinator = _CoordinatorFinto(
        ["IT001", "ITP0A"], {"IT001": TIPO_POD_SCAMBIO, "ITP0A": TIPO_POD_PRODUZIONE}
    )

    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])
    assert len(aggiunte) == 2

    manager = await async_get_manager(hass)
    sorgenti = manager.data["energy_sources"]
    assert {s["type"] for s in sorgenti} == {"grid", "solar"}

    grid = next(s for s in sorgenti if s["type"] == "grid")
    assert grid["stat_energy_from"] == "edistribuzione:it001_energia"
    assert grid["stat_energy_to"] == "edistribuzione:it001_energia_immessa"

    solar = next(s for s in sorgenti if s["type"] == "solar")
    assert solar["stat_energy_from"] == "edistribuzione:itp0a_energia_immessa"


async def test_idempotente_non_duplica_se_richiamata_due_volte(hass):
    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})

    prima = await async_configura_energy_dashboard(hass, [coordinator])
    seconda = await async_configura_energy_dashboard(hass, [coordinator])

    assert len(prima) == 1
    assert seconda == []

    manager = await async_get_manager(hass)
    assert len(manager.data["energy_sources"]) == 1


async def test_non_tocca_sorgenti_esistenti_non_correlate(hass):
    """Una sorgente gas configurata a mano dall'utente deve restare intatta
    dopo aver aggiunto quelle di questa integrazione."""
    manager = await async_get_manager(hass)
    await manager.async_update({
        "energy_sources": [{"type": "gas", "stat_energy_from": "sensor.gas_casa"}]
    })

    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})
    await async_configura_energy_dashboard(hass, [coordinator])

    manager = await async_get_manager(hass)
    tipi = [s["type"] for s in manager.data["energy_sources"]]
    assert "gas" in tipi
    assert "grid" in tipi
    gas = next(s for s in manager.data["energy_sources"] if s["type"] == "gas")
    assert gas["stat_energy_from"] == "sensor.gas_casa"


async def test_non_riaggiunge_una_sorgente_grid_configurata_a_mano(hass):
    """Se l'utente ha già collegato la prelevata di questo POD a una
    sorgente grid a mano (magari con un costo configurato), l'azione non
    deve crearne una seconda identica."""
    manager = await async_get_manager(hass)
    await manager.async_update({
        "energy_sources": [{
            "type": "grid",
            "stat_energy_from": "edistribuzione:it001_energia",
            "stat_energy_to": None,
            "stat_cost": "sensor.costo_luce",
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
        }]
    })

    coordinator = _CoordinatorFinto(["IT001"], {"IT001": TIPO_POD_SCAMBIO})
    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])

    assert aggiunte == []
    manager = await async_get_manager(hass)
    assert len(manager.data["energy_sources"]) == 1
    assert manager.data["energy_sources"][0]["stat_cost"] == "sensor.costo_luce"


async def test_piu_pod_sullo_stesso_coordinator(hass):
    coordinator = _CoordinatorFinto(
        ["IT001", "IT002"], {"IT001": TIPO_POD_SCAMBIO, "IT002": TIPO_POD_SCAMBIO}
    )

    aggiunte = await async_configura_energy_dashboard(hass, [coordinator])

    assert len(aggiunte) == 2
    manager = await async_get_manager(hass)
    assert len(manager.data["energy_sources"]) == 2
