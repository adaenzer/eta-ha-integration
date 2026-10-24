"""Sensors for values of the ETA heater."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, Platform, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import unit_from_name
from .coordinator import EtaConfigEntry, EtaCoordinator
from .entity import EtaValueEntity, device_info, entries_for_platform
from .naming import EntityName
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
        [
            EtaReadDurationSensor(coordinator),
            *(
                EtaSensor(coordinator, monitor_entry, name)
                for monitor_entry, name in entries_for_platform(
                    coordinator, Platform.SENSOR
                )
            ),
        ]
    )


class EtaSensor(EtaValueEntity, SensorEntity):
    """A numeric value of the heater."""

    def __init__(
        self, coordinator: EtaCoordinator, entry: MonitorEntry, name: EntityName
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, name)
        self._attr_device_class = self.value_type.device_class  # type: ignore[assignment]
        self._attr_native_unit_of_measurement = self.value_type.unit
        self._attr_state_class = self.value_type.state_class
        # Plain numbers may name their unit, e.g. "Netzfreq. [Hz]"
        if self.value_type.unit is None and (unit := unit_from_name(entry.name)):
            self._attr_device_class = unit.device_class
            self._attr_native_unit_of_measurement = unit.unit
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | int | str:
        """Return the value."""
        if (states := self.value_type.states) is None:
            return self.raw_value
        # Unknown numbers stay apart, e.g. "7", so they can be found later
        return states.get(int(self.raw_value), str(int(self.raw_value)))


class EtaReadDurationSensor(CoordinatorEntity[EtaCoordinator], SensorEntity):
    """How long reading all enabled values took, to help choose the interval."""

    _attr_has_entity_name = True
    _attr_translation_key = "read_duration"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: EtaCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_read_duration"
        self._attr_device_info = device_info(coordinator.config_entry)

    @property
    def native_value(self) -> float | None:
        """Return the duration of the last successful read."""
        return self.coordinator.last_read_duration
