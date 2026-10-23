"""Coordinator that reads the heater's values."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import EtaClient, EtaError
from .const import (
    CONF_LEVELS,
    DEFAULT_LEVELS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .protocol import MonitorEntry
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

type EtaConfigEntry = ConfigEntry[EtaCoordinator]
type EtaData = dict[tuple[int, int], float | int]


def monitor_list_store(hass: HomeAssistant, entry_id: str) -> Store[dict]:
    """Return the store that caches the monitor list of an entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


class EtaCoordinator(DataUpdateCoordinator[EtaData]):
    """Read the monitor list once, then poll its values."""

    config_entry: EtaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: EtaConfigEntry, client: EtaClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            always_update=False,
        )
        self.client = client
        self.monitor_list: list[MonitorEntry] = []
        self._store = monitor_list_store(hass, entry.entry_id)

    @property
    def levels(self) -> list[int]:
        """Return the monitor list levels to read."""
        return sorted(self.config_entry.options.get(CONF_LEVELS, DEFAULT_LEVELS))

    async def _async_setup(self) -> None:
        """Load the monitor list from storage or read it from the heater.

        Reading the list takes a while, so it is only read again when the
        configured levels change or the user asks for it.
        """
        stored = await self._store.async_load()
        if stored and stored.get(CONF_LEVELS) == self.levels:
            self.monitor_list = [MonitorEntry.from_dict(e) for e in stored["entries"]]
            return
        _LOGGER.info("Reading monitor list levels %s from the heater", self.levels)
        try:
            self.monitor_list = await self.client.async_read_monitor_list(self.levels)
        except (EtaError, TransportError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="monitor_list_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not self.monitor_list:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="monitor_list_empty"
            )
        await self._store.async_save(
            {
                CONF_LEVELS: self.levels,
                "entries": [entry.as_dict() for entry in self.monitor_list],
            }
        )

    async def async_clear_monitor_list(self) -> None:
        """Forget the cached monitor list so it is read again on the next setup."""
        await self._store.async_remove()

    async def _async_update_data(self) -> EtaData:
        """Read all values of the monitor list."""
        try:
            raw = await self.client.async_read_values(
                [entry.key for entry in self.monitor_list]
            )
        except (EtaError, TransportError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not raw:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_values")
        return {
            entry.key: entry.scale(raw[entry.key])
            for entry in self.monitor_list
            if entry.key in raw
        }
