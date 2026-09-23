"""Fixture condivise per i test.

pytest_plugins/enable_custom_integrations è il pattern standard richiesto da
pytest-homeassistant-custom-component perché Home Assistant, nei test,
carichi davvero questa integrazione custom invece di ignorarla.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione.const import CONF_PODS, CONF_REFRESH_TOKEN, DOMAIN
from custom_components.edistribuzione.coordinator import EdistribuzioneCoordinator

pytest_plugins = "pytest_homeassistant_custom_component"

POD_A = "IT001E10000001"
POD_B = "IT001E10000002"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Applicato automaticamente a tutti i test che usano il fixture 'hass'."""
    yield


@pytest.fixture
def make_edist_coordinator(hass):
    """Factory: EdistribuzioneCoordinator con una MockConfigEntry agganciata a hass."""

    def _make(*, data=None, options=None, pods=None):
        pods = pods if pods is not None else [POD_A]
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: pods, **(data or {})},
            options=options or {},
        )
        entry.add_to_hass(hass)
        return EdistribuzioneCoordinator(hass, entry)

    return _make
