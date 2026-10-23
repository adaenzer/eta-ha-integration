"""Base entities for the ETA heater integration."""

from __future__ import annotations

from typing import Any

from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, value_type_for
from .coordinator import EtaConfigEntry, EtaCoordinator, value_unique_id
from .naming import EntityName, entity_names
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
) -> list[tuple[MonitorEntry, EntityName]]:
    """Return the monitor list entries of a platform with their entity names."""
    names = entity_names(coordinator.monitor_list)
    return [
        (e, names[e.key])
        for e in coordinator.monitor_list
        if value_type_for(e.value_type).platform == platform
    ]


class EtaValueEntity(CoordinatorEntity[EtaCoordinator]):
    """An entity showing one value of the monitor list."""

    _attr_has_entity_name = True
    # The monitor list entry never changes, so it is not recorded
    _unrecorded_attributes = frozenset(
        {"monitor_name", "node", "index", "value_type", "divider", "level", "section"}
    )

    def __init__(
        self, coordinator: EtaCoordinator, entry: MonitorEntry, name: EntityName
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.monitor_entry = entry
        self.value_type = value_type_for(entry.value_type)
        self._attr_translation_key = self.value_type.kind
        if name.with_kind:
            # The translation adds the kind, e.g. "{name} (Motor)"
            self._attr_translation_placeholders = {"name": name.name}
        else:
            # Names come from the heater, so they cannot be translated
            self._attr_name = name.name
        self._attr_extra_state_attributes: dict[str, Any] = {
            "monitor_name": entry.name,
            "node": entry.node,
            "index": entry.index,
            "value_type": entry.value_type,
            "divider": entry.divider,
            "level": entry.level,
            "section": entry.section,
        }
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
