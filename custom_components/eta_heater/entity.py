"""Base entities for the ETA heater integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, value_type_for
from .coordinator import EtaConfigEntry, EtaCoordinator, value_unique_id
from .protocol import MonitorEntry


def device_info(entry: EtaConfigEntry) -> DeviceInfo:
    """Return the device all entities of an entry belong to."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="ETA heater",
        manufacturer="ETA Heiztechnik",
        model="RS232 interface",
    )


def entries_for_platform(
    coordinator: EtaCoordinator, platform: Platform
) -> list[MonitorEntry]:
    """Return the monitor list entries that belong to a platform."""
    return [
        e
        for e in coordinator.monitor_list
        if value_type_for(e.value_type).platform == platform
    ]


class EtaValueEntity(CoordinatorEntity[EtaCoordinator]):
    """An entity showing one value of the monitor list."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EtaCoordinator, entry: MonitorEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.monitor_entry = entry
        self.value_type = value_type_for(entry.value_type)
        # Names come from the heater, so they cannot be translated
        self._attr_name = entry.name
        self._attr_translation_key = self.value_type.translation_key
        self._attr_unique_id = value_unique_id(coordinator.config_entry.entry_id, entry)
        # Disabled entities are not read from the heater, see the coordinator
        self._attr_entity_registry_enabled_default = coordinator.enabled_by_default(
            entry
        )
        self._attr_device_info = device_info(coordinator.config_entry)

    @property
    def available(self) -> bool:
        """Return True if the last update contained this value."""
        return super().available and self.monitor_entry.key in self.coordinator.data

    @property
    def raw_value(self) -> float | int:
        """Return the current value."""
        return self.coordinator.data[self.monitor_entry.key]
