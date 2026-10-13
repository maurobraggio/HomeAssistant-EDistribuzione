"""Ordine di inizializzazione della config entry."""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import async_setup_entry
from custom_components.edistribuzione.const import CONF_PODS, CONF_REFRESH_TOKEN, DOMAIN


async def test_primo_refresh_precede_setup_sensori(hass):
    """Un retry dopo un refresh fallito non deve duplicare i sensori."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: ["IT001E10000001"]},
    )
    entry.add_to_hass(hass)
    ordine: list[str] = []

    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=lambda: ordine.append("refresh")
    )

    async def _forward(*args, **kwargs):
        ordine.append("platform")

    with (
        patch(
            "custom_components.edistribuzione.EdistribuzioneCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.edistribuzione._async_registra_servizi",
            AsyncMock(return_value=None),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=_forward),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert ordine == ["refresh", "platform"]
