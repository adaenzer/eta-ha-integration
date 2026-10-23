"""Diagnostics for the ETA heater integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import EtaConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EtaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    selected = {e.key for e in coordinator.selected_entries()}
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "monitor_list": [
            {
                "name": e.name,
                "node": e.node,
                "index": e.index,
                "value_type": e.value_type,
                "divider": e.divider,
                "level": e.level,
                "read": e.key in selected,
                "header": list(e.header),
                "value": data.get(e.key),
            }
            for e in coordinator.monitor_list
        ],
    }
