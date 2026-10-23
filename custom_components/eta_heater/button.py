"""Button to read the monitor list of the ETA heater again."""

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
    """Set up the button."""
    async_add_entities([EtaRereadMonitorListButton(entry)])


class EtaRereadMonitorListButton(ButtonEntity):
    """Read the monitor list of the configured levels from the heater again."""

    _attr_has_entity_name = True
    _attr_translation_key = "reread_monitor_list"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: EtaConfigEntry) -> None:
        """Initialize the button."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reread_monitor_list"
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
            coordinator.async_reread_monitor_list(),
            "eta_heater_reread_monitor_list",
        )
