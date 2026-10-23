"""Sensors for values of the ETA heater."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EtaConfigEntry, EtaCoordinator
from .entity import EtaValueEntity, entries_for_platform
from .protocol import MonitorEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors from the monitor list."""
    coordinator = entry.runtime_data
    async_add_entities(
        EtaSensor(coordinator, monitor_entry)
        for monitor_entry in entries_for_platform(coordinator, Platform.SENSOR)
    )


class EtaSensor(EtaValueEntity, SensorEntity):
    """A numeric value of the heater."""

    def __init__(self, coordinator: EtaCoordinator, entry: MonitorEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_device_class = self.value_type.device_class  # type: ignore[assignment]
        self._attr_native_unit_of_measurement = self.value_type.unit
        self._attr_state_class = self.value_type.state_class

    @property
    def native_value(self) -> float | int:
        """Return the value."""
        return self.raw_value
