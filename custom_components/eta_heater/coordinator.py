"""Coordinator that reads the heater's values."""

from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import EtaClient, EtaError
from .const import (
    CONF_LEVELS,
    DEFAULT_LEVELS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
    value_type_for,
)
from .protocol import MonitorEntry
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

type EtaConfigEntry = ConfigEntry[EtaCoordinator]
type EtaData = dict[tuple[int, int], float | int]


def monitor_list_store(hass: HomeAssistant, entry_id: str) -> Store[dict]:
    """Return the store that caches the monitor list of an entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


def levels_issue_id(entry_id: str) -> str:
    """Return the id of the repair issue shown when the levels were changed."""
    return f"levels_changed_{entry_id}"


def monitor_list_issue_id(entry_id: str) -> str:
    """Return the id of the repair issue shown when reading the list failed."""
    return f"monitor_list_failed_{entry_id}"


class MonitorListEmptyError(EtaError):
    """The heater returned no values for the requested levels."""


def value_unique_id(entry_id: str, monitor_entry: MonitorEntry) -> str:
    """Return the unique id of the entity showing a monitor list value."""
    return f"{entry_id}_{monitor_entry.node}_{monitor_entry.index}"


class EtaCoordinator(DataUpdateCoordinator[EtaData]):
    """Read the monitor list once, then poll the values of enabled entities."""

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
        # Levels the monitor list in use was read with; the configured levels
        # only take effect when the list is read again
        self.list_levels: list[int] = []
        self.rereading = False
        self.last_read_duration: float | None = None
        self._store = monitor_list_store(hass, entry.entry_id)

    @property
    def levels(self) -> list[int]:
        """Return the configured monitor list levels."""
        return sorted(self.config_entry.options.get(CONF_LEVELS, DEFAULT_LEVELS))

    async def _async_setup(self) -> None:
        """Load the monitor list from storage, or start reading it.

        Reading the list takes minutes. Home Assistant waits for the setup
        when the config flow finishes, so the first read runs in the
        background and the entry is reloaded once the list is available.
        After that, the list is only read again when the user presses the
        button.
        """
        if stored := await self._store.async_load():
            self.list_levels = stored[CONF_LEVELS]
            self.monitor_list = [MonitorEntry.from_dict(e) for e in stored["entries"]]
            self._async_update_levels_issue()
            return
        self.rereading = True
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_reread_monitor_list(),
            "eta_heater_read_monitor_list",
        )

    async def _async_read_monitor_list(self) -> None:
        """Read the monitor list of the configured levels and cache it."""
        levels = self.levels
        _LOGGER.info("Reading monitor list levels %s from the heater", levels)
        monitor_list = await self.client.async_read_monitor_list(levels)
        if not monitor_list:
            raise MonitorListEmptyError("The heater returned an empty monitor list")
        await self._store.async_save(
            {CONF_LEVELS: levels, "entries": [e.as_dict() for e in monitor_list]}
        )
        self.list_levels = levels
        self.monitor_list = monitor_list

    async def async_reread_monitor_list(self) -> None:
        """Read the monitor list (again) and reload the entry to apply it.

        Values keep being shown from the previous list while reading; if
        reading fails, the previous list stays in use.
        """
        self.rereading = True
        issue_id = monitor_list_issue_id(self.config_entry.entry_id)
        try:
            await self._async_read_monitor_list()
        except (EtaError, TransportError) as err:
            error = str(err) or type(err).__name__
            _LOGGER.error("Reading the monitor list failed: %s", error)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="monitor_list_failed",
                translation_placeholders={
                    "title": self.config_entry.title,
                    "error": error,
                },
            )
            return
        finally:
            self.rereading = False
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _async_update_levels_issue(self) -> None:
        """Tell the user to read the list again after changing the levels."""
        issue_id = levels_issue_id(self.config_entry.entry_id)
        if self.list_levels == self.levels:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="levels_changed",
            translation_placeholders={
                "title": self.config_entry.title,
                "levels": ", ".join(map(str, self.levels)),
                "list_levels": ", ".join(map(str, self.list_levels)),
            },
        )

    def enabled_by_default(self, monitor_entry: MonitorEntry) -> bool:
        """Return True if a value's entity is enabled when it is first created.

        Values of the lowest level are enabled, values that only deeper
        levels add are created disabled and can be enabled one by one.
        """
        return monitor_entry.level == min(self.list_levels, default=0)

    def selected_entries(self) -> list[MonitorEntry]:
        """Return the monitor list entries whose entities are enabled.

        Only these are read from the heater, so disabling entities shortens
        the time a full read takes.
        """
        registry = er.async_get(self.hass)
        selected = []
        for monitor_entry in self.monitor_list:
            entity_id = registry.async_get_entity_id(
                value_type_for(monitor_entry.value_type).platform,
                DOMAIN,
                value_unique_id(self.config_entry.entry_id, monitor_entry),
            )
            if entity_id is None:
                enabled = self.enabled_by_default(monitor_entry)
            else:
                enabled = not registry.async_get(entity_id).disabled  # type: ignore[union-attr]
            if enabled:
                selected.append(monitor_entry)
        return selected

    async def _async_update_data(self) -> EtaData:
        """Read the values of all enabled entities."""
        if self.rereading:
            # The monitor list is being read; keep the last values instead of
            # queueing a value read behind it
            return self.data or {}
        selected = self.selected_entries()
        if not selected:
            return {}
        start = monotonic()
        try:
            raw = await self.client.async_read_values([e.key for e in selected])
        except (EtaError, TransportError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not raw:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_values")
        self.last_read_duration = monotonic() - start
        return {e.key: e.scale(raw[e.key]) for e in selected if e.key in raw}
