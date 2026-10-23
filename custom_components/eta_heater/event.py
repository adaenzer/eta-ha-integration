"""Event entity for error messages sent by the ETA heater."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPE_ERROR
from .coordinator import EtaConfigEntry
from .entity import device_info
from .protocol import Frame

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the error event entity."""
    async_add_entities([EtaErrorEvent(entry)])


class EtaErrorEvent(EventEntity):
    """Fires whenever the heater sends an error message."""

    _attr_has_entity_name = True
    _attr_translation_key = "error"
    _attr_event_types = [EVENT_TYPE_ERROR]

    def __init__(self, entry: EtaConfigEntry) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_error"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Listen for error frames."""
        self.async_on_remove(
            self._entry.runtime_data.client.add_error_listener(self._handle_error)
        )

    @callback
    def _handle_error(self, frame: Frame) -> None:
        self._trigger_event(
            EVENT_TYPE_ERROR, {"error_type": frame.service, "message": frame.text}
        )
        self.async_write_ha_state()
