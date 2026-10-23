"""Binary sensors for on/off values of the ETA heater."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EtaConfigEntry, EtaCoordinator
from .entity import EtaValueEntity, entries_for_platform
from .naming import EntityName
from .protocol import MonitorEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors from the monitor list."""
    coordinator = entry.runtime_data
    async_add_entities(
        EtaBinarySensor(coordinator, monitor_entry, name)
        for monitor_entry, name in entries_for_platform(
            coordinator, Platform.BINARY_SENSOR
        )
    )


class EtaBinarySensor(EtaValueEntity, BinarySensorEntity):
    """An on/off value of the heater."""

    def __init__(
        self, coordinator: EtaCoordinator, entry: MonitorEntry, name: EntityName
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, entry, name)
        self._attr_device_class = self.value_type.device_class  # type: ignore[assignment]

    @property
    def is_on(self) -> bool:
        """Return True if the value is not zero."""
        return self.raw_value != 0
