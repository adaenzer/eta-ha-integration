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
    MOTOR_VALUE_TYPE,
    STORAGE_VERSION,
    value_type_for,
)
from .protocol import MonitorEntry, build_monitor_list, merge_monitor_lists
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

type EtaConfigEntry = ConfigEntry[EtaCoordinator]
type EtaData = dict[tuple[int, int], float | int]


# A value missing in reads is shown with its last value for this many update
# intervals, but at least VALUE_MIN_MAX_AGE seconds
VALUE_MAX_AGE_INTERVALS = 3
VALUE_MIN_MAX_AGE = 900


def monitor_list_store(hass: HomeAssistant, entry_id: str) -> Store[dict]:
    """Return the store that caches the monitor list of an entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


def levels_issue_id(entry_id: str) -> str:
    """Return the id of the repair issue shown when the levels were changed."""
    return f"levels_changed_{entry_id}"


def other_program_issue_id(entry_id: str) -> str:
    """Return the id of the repair issue shown when someone else polls."""
    return f"other_program_{entry_id}"


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
        # When each value was last received, to keep showing it for a while
        # when a read misses it
        self._received_at: dict[tuple[int, int], float] = {}
        self._foreign_value_frames = 0
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

    async def _async_read_monitor_list(self, reset: bool = False) -> None:
        """Read the monitor list of the configured levels and cache it.

        Unless reset, the read is merged into the cached list: entries read
        again are updated and missing ones added, nothing is removed.
        """
        levels = self.levels
        _LOGGER.info("Reading monitor list levels %s from the heater", levels)
        monitor_list = build_monitor_list(
            await self.client.async_read_monitor_list(levels),
            lambda entry: entry.value_type == MOTOR_VALUE_TYPE,
        )
        if not monitor_list:
            raise MonitorListEmptyError("The heater returned an empty monitor list")
        if not reset:
            added = len(
                {e.key for e in monitor_list} - {e.key for e in self.monitor_list}
            )
            monitor_list = merge_monitor_lists(self.monitor_list, monitor_list)
            _LOGGER.info("Monitor list read, %d new values", added)
        await self._store.async_save(
            {CONF_LEVELS: levels, "entries": [e.as_dict() for e in monitor_list]}
        )
        self.list_levels = levels
        self.monitor_list = monitor_list

    async def async_reread_monitor_list(self, reset: bool = False) -> None:
        """Read the monitor list (again) and reload the entry to apply it.

        Without reset the read is merged into the current list. Values keep
        being shown while reading; if reading fails, the list stays as it is.
        """
        self.rereading = True
        issue_id = monitor_list_issue_id(self.config_entry.entry_id)
        try:
            await self._async_read_monitor_list(reset)
        except (EtaError, TransportError) as err:
            self._async_check_other_program()
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

    def _async_check_other_program(self) -> None:
        """Warn while another program is requesting values from the heater.

        The controller cannot serve two programs, so requests of this
        integration fail while e.g. a former AppDaemon app is still polling.
        """
        issue_id = other_program_issue_id(self.config_entry.entry_id)
        seen = self.client.foreign_value_frames
        if seen == self._foreign_value_frames:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        self._foreign_value_frames = seen
        _LOGGER.warning(
            "The heater sends values this integration did not request; another "
            "program is talking to it and disturbs the communication"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="other_program",
            translation_placeholders={"title": self.config_entry.title},
        )

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
            self._async_check_other_program()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._async_check_other_program()
        now = monotonic()
        received = {e.key: e.scale(raw[e.key]) for e in selected if e.key in raw}
        for key in received:
            self._received_at[key] = now
        if received:
            self.last_read_duration = now - start
        # Over a flaky connection a read can miss some values; they are shown
        # with their last value until they are older than value_max_age
        keys = {e.key for e in selected}
        data = {
            key: value
            for key, value in (self.data or {}).items()
            if key in keys and now - self._received_at.get(key, 0) <= self.value_max_age
        }
        data.update(received)
        if not data:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_values")
        return data

    @property
    def value_max_age(self) -> float:
        """Return how long a value is shown after it was last received."""
        interval = self.update_interval.total_seconds() if self.update_interval else 0
        return max(VALUE_MAX_AGE_INTERVALS * interval, VALUE_MIN_MAX_AGE)
