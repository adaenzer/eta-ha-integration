"""Buttons of the ETA heater: read the monitor list, send commands."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import EtaError
from .const import CONF_ALLOW_COMMANDS, DOMAIN, HEATER_COMMANDS, HeaterCommand
from .coordinator import EtaConfigEntry
from .entity import device_info
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
# Seconds after a command until the display is read to show its confirmation
DISPLAY_AFTER_COMMAND = 3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    buttons: list[ButtonEntity] = [
        EtaMonitorListButton(entry, "reread_monitor_list", reset=False),
        EtaMonitorListButton(entry, "reset_monitor_list", reset=True),
        EtaReadDisplayButton(entry),
    ]
    if entry.options.get(CONF_ALLOW_COMMANDS, False):
        buttons += [EtaCommandButton(entry, command) for command in HEATER_COMMANDS]
    async_add_entities(buttons)


class EtaMonitorListButton(ButtonEntity):
    """Read the monitor list of the configured levels from the heater.

    Reading again merges the result into the current list, which fills gaps
    left by lost transmissions; reset starts over with an empty list.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: EtaConfigEntry, key: str, reset: bool) -> None:
        """Initialize the button."""
        self._entry = entry
        self._reset = reset
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Start reading; this takes minutes, so it runs in the background."""
        coordinator = self._entry.runtime_data
        if coordinator.rereading:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reread_in_progress"
            )
        coordinator.rereading = True
        self._entry.async_create_background_task(
            self.hass,
            coordinator.async_reread_monitor_list(reset=self._reset),
            "eta_heater_read_monitor_list",
        )


class EtaCommandButton(ButtonEntity):
    """Send a remote control command, like ETA's GSM connector does by SMS."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: EtaConfigEntry, command: HeaterCommand) -> None:
        """Initialize the button."""
        self._entry = entry
        self._command = command
        self._attr_translation_key = command.key
        self._attr_unique_id = f"{entry.entry_id}_{command.key}"
        self._attr_entity_registry_enabled_default = command.enabled_by_default
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Send the command and read the values to show its effect."""
        coordinator = self._entry.runtime_data
        if coordinator.rereading:
            # The list read holds the line for minutes; refuse instead of
            # sending the command at some unknown later time
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="busy_reading_list"
            )
        try:
            await coordinator.client.async_send_command(self._command.code)
        except (EtaError, TransportError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # The controller confirms commands on its display (INFO screen)
        await asyncio.sleep(DISPLAY_AFTER_COMMAND)
        try:
            await coordinator.async_read_display()
        except (EtaError, TransportError) as err:
            _LOGGER.debug("Could not read the display after the command: %s", err)
        await coordinator.async_request_refresh()


class EtaReadDisplayButton(ButtonEntity):
    """Read the heater's display into the display sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "read_display"

    def __init__(self, entry: EtaConfigEntry) -> None:
        """Initialize the button."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_read_display"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Read the display."""
        coordinator = self._entry.runtime_data
        if coordinator.rereading:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="busy_reading_list"
            )
        try:
            await coordinator.async_read_display()
        except (EtaError, TransportError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="display_failed",
                translation_placeholders={"error": str(err)},
            ) from err
