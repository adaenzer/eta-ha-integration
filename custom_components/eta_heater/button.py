"""Button to read the monitor list of the ETA heater again."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
    """Forget the cached monitor list and read it from the heater again."""

    _attr_has_entity_name = True
    _attr_translation_key = "reread_monitor_list"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: EtaConfigEntry) -> None:
        """Initialize the button."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reread_monitor_list"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Clear the cached list and reload the entry."""
        await self._entry.runtime_data.async_clear_monitor_list()
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
