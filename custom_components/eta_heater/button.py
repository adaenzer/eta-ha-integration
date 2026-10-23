"""Buttons to read the monitor list of the ETA heater."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import EtaConfigEntry
from .entity import device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities(
        [
            EtaMonitorListButton(entry, "reread_monitor_list", reset=False),
            EtaMonitorListButton(entry, "reset_monitor_list", reset=True),
        ]
    )


class EtaMonitorListButton(ButtonEntity):
    """Read the monitor list of the configured levels from the heater.

    Reading again merges the result into the current list, which fills gaps
    left by lost transmissions; reset starts over with an empty list.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: EtaConfigEntry, key: str, reset: bool) -> None:
        """Initialize the button."""
        self._entry = entry
        self._reset = reset
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Start reading; this takes minutes, so it runs in the background."""
        coordinator = self._entry.runtime_data
        if coordinator.rereading:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reread_in_progress"
            )
        coordinator.rereading = True
        self._entry.async_create_background_task(
            self.hass,
            coordinator.async_reread_monitor_list(reset=self._reset),
            "eta_heater_read_monitor_list",
        )
