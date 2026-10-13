"""Segnalazioni nella dashboard Riparazioni di Home Assistant."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_ANTIBOT_REFRESH = "antibot_refresh_blocked"


def _antibot_issue_id(entry_id: str) -> str:
    return f"{ISSUE_ANTIBOT_REFRESH}_{entry_id}"


@callback
def async_create_antibot_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Segnala che Imperva sta negando anche il rinnovo del token."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _antibot_issue_id(entry.entry_id),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ANTIBOT_REFRESH,
        translation_placeholders={"entry_title": entry.title},
    )


@callback
def async_delete_antibot_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Rimuove l'avviso appena il rinnovo torna a funzionare."""
    ir.async_delete_issue(hass, DOMAIN, _antibot_issue_id(entry_id))


class AntibotRepairFlow(RepairsFlow):
    """Offre dalla riparazione un collegamento diretto alla riautenticazione."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None:
            self._entry.async_start_reauth(self.hass)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            description_placeholders={"entry_title": self._entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Crea il flusso di correzione per la segnalazione anti-bot."""
    if not issue_id.startswith(f"{ISSUE_ANTIBOT_REFRESH}_") or not data:
        raise ValueError(f"Segnalazione E-Distribuzione sconosciuta: {issue_id}")
    entry_id = data.get("entry_id")
    if not isinstance(entry_id, str) or (entry := hass.config_entries.async_get_entry(entry_id)) is None:
        raise ValueError("Configurazione E-Distribuzione non trovata")
    return AntibotRepairFlow(entry)
