"""Sensors for values of the ETA heater."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EtaConfigEntry, EtaCoordinator
from .entity import EtaValueEntity, entries_for_platform
from .protocol import MonitorEntry

_LOGGER = logging.getLogger(__name__)

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
    """A numeric or enum value of the heater."""

    def __init__(self, coordinator: EtaCoordinator, entry: MonitorEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_device_class = self.value_type.device_class  # type: ignore[assignment]
        self._attr_native_unit_of_measurement = self.value_type.unit
        self._attr_state_class = self.value_type.state_class
        if self.value_type.options is not None:
            self._attr_options = list(self.value_type.options)

    @property
    def native_value(self) -> float | int | str | None:
        """Return the value, mapped to an option for enum sensors."""
        value = self.raw_value
        if self.device_class != SensorDeviceClass.ENUM:
            return value
        options = self.value_type.options or ()
        if isinstance(value, int) and 0 <= value < len(options):
            return options[value]
        _LOGGER.debug("Unknown state %s for %s", value, self.monitor_entry.name)
        return None
