"""Esperienza di riautenticazione del coordinator."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.edistribuzione.api import ApiError
from custom_components.edistribuzione.auth import AuthError, BloccoAntibot, InvalidRefreshToken
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_INSTANCE_URL,
    DOMAIN,
)
from custom_components.edistribuzione.repairs import ISSUE_ANTIBOT_REFRESH


async def test_refresh_token_revocato_richiede_reauth(make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=InvalidRefreshToken("invalid_grant")
    )

    with pytest.raises(ConfigEntryAuthFailed, match="riautenticazione necessaria"):
        await coordinator._async_ensure_token()


async def test_refresh_periodico_avvia_davvero_il_flow_reauth(
    hass, make_edist_coordinator
):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=InvalidRefreshToken("invalid_grant")
    )

    with patch.object(
        coordinator.entry, "async_start_reauth_if_available"
    ) as start_reauth:
        await coordinator.async_refresh()

    start_reauth.assert_called_once_with(hass)


async def test_refresh_valido_aggiorna_token_api(make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(
            access_token="accesso-nuovo",
            refresh_token="rt",
            instance_url="https://edistribuzione.my.site.com/PortaleClienti",
        )
    )
    coordinator._api.update_token = Mock()
    coordinator._portal.update_token = Mock()

    await coordinator._async_ensure_token()

    coordinator._api.update_token.assert_called_once_with("accesso-nuovo")
    coordinator._portal.update_token.assert_called_once_with(
        "accesso-nuovo", "https://edistribuzione.my.site.com/PortaleClienti"
    )
    assert coordinator.entry.data[CONF_ACCESS_TOKEN] == "accesso-nuovo"
    assert coordinator.entry.data[CONF_INSTANCE_URL] == (
        "https://edistribuzione.my.site.com/PortaleClienti"
    )


async def test_access_token_browser_evitaria_refresh_immediato(make_edist_coordinator):
    coordinator = make_edist_coordinator(
        data={
            CONF_ACCESS_TOKEN: "accesso-browser",
            CONF_INSTANCE_URL: "https://edistribuzione.my.site.com/PortaleClienti",
        }
    )
    coordinator._auth.async_refresh_access_token = AsyncMock()
    coordinator._api.update_token = Mock()
    coordinator._portal.update_token = Mock()

    await coordinator._async_ensure_token()

    coordinator._auth.async_refresh_access_token.assert_not_awaited()
    coordinator._api.update_token.assert_called_once_with("accesso-browser")
    coordinator._portal.update_token.assert_called_once_with(
        "accesso-browser", "https://edistribuzione.my.site.com/PortaleClienti"
    )


async def test_401_forza_un_solo_refresh_e_riprova(make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "accesso-scaduto"})
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=[
            ApiError("401", status=401),
            [{"readings": {"sampleValues": []}}],
        ]
    )
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(
            access_token="accesso-rinnovato", refresh_token="rt", instance_url=""
        )
    )

    risultato = await coordinator._async_get_daily_load_profile(
        "IT001E10000001", date(2026, 10, 1), date(2026, 10, 1), "A1"
    )

    assert risultato == [{"readings": {"sampleValues": []}}]
    coordinator._auth.async_refresh_access_token.assert_awaited_once_with("rt")
    assert coordinator._api.async_get_daily_load_profile.await_count == 2


async def test_errore_temporaneo_refresh_non_chiede_reauth(make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AuthError("503 temporaneo")
    )

    with pytest.raises(UpdateFailed, match="503 temporaneo"):
        await coordinator._async_ensure_token()


async def test_antibot_refresh_crea_riparazione_senza_html(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=BloccoAntibot("<html>pagina firewall</html>")
    )

    with pytest.raises(UpdateFailed, match="Sistema > Riparazioni") as exc_info:
        await coordinator._async_ensure_token()

    assert "<html>" not in str(exc_info.value)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_ANTIBOT_REFRESH}_{coordinator.entry.entry_id}"
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is True


async def test_refresh_valido_rimuove_riparazione_antibot(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator()
    issue_id = f"{ISSUE_ANTIBOT_REFRESH}_{coordinator.entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ANTIBOT_REFRESH,
    )
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(
            access_token="accesso-nuovo", refresh_token="rt", instance_url=""
        )
    )

    await coordinator._async_ensure_token()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
