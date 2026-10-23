"""Tests for setting up the integration and its entities."""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eta_heater.const import CONF_LEVELS, CONF_PUBLISH_ERRORS, DOMAIN
from custom_components.eta_heater.diagnostics import async_get_config_entry_diagnostics
from custom_components.eta_heater.transport import Transport, TransportError

from .fake_heater import TOPIC, FakeHeater

pytestmark = pytest.mark.usefixtures("transports")


def _entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, node: int, index: int
) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{node}_{index}"
    ) or er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_{node}_{index}"
    )
    assert entity_id
    return entity_id


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_ptvo(
    hass: HomeAssistant, heater: FakeHeater, ptvo_entry: MockConfigEntry
) -> None:
    """Entities are created from the monitor list with converted values."""
    await _setup(hass, ptvo_entry)
    assert ptvo_entry.state is ConfigEntryState.LOADED

    def state(node: int, index: int):
        return hass.states.get(_entity_id(hass, ptvo_entry, node, index))

    temp = state(8, 1)
    assert temp.state == "65.2"
    assert temp.attributes["unit_of_measurement"] == "°C"
    assert temp.attributes["friendly_name"] == "ETA heater Kesseltemperatur"
    assert state(8, 4).state == "-1.0"
    assert state(8, 2).state == "3"
    assert state(8, 3).state == STATE_ON
    assert state(8, 5).state == "7"
    assert state(8, 6).attributes["unit_of_measurement"] == "h"
    # level 1 is not configured
    assert not er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ptvo_entry.entry_id}_9_300"
    )


async def test_values_update(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Values update and become unavailable when the heater stops answering."""
    await _setup(hass, serial_entry)
    coordinator = serial_entry.runtime_data
    flap = _entity_id(hass, serial_entry, 8, 3)

    heater.values[(8, 3)] = 0
    del heater.values[(8, 1)]
    await coordinator.async_refresh()
    assert hass.states.get(flap).state == STATE_OFF
    assert (
        hass.states.get(_entity_id(hass, serial_entry, 8, 1)).state == STATE_UNAVAILABLE
    )

    heater.respond = False
    await coordinator.async_refresh()
    assert hass.states.get(flap).state == STATE_UNAVAILABLE


async def test_monitor_list_cached(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """The monitor list is read once and again only when levels change."""
    await _setup(hass, serial_entry)
    requests = len(heater.requests_of("MA"))
    assert requests == len(heater.monitor_list[0]) + 1

    await hass.config_entries.async_reload(serial_entry.entry_id)
    await hass.async_block_till_done()
    assert len(heater.requests_of("MA")) == requests

    hass.config_entries.async_update_entry(serial_entry, options={CONF_LEVELS: [0, 1]})
    await hass.config_entries.async_reload(serial_entry.entry_id)
    await hass.async_block_till_done()
    assert len(heater.requests_of("MA")) > requests
    assert _entity_id(hass, serial_entry, 9, 300)


async def test_reread_button(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """The button reads the monitor list again."""
    await _setup(hass, serial_entry)
    requests = len(heater.requests_of("MA"))
    heater.monitor_list[0].append(((8, 0, 20, 0, 176, 0, 0, 10, 0), "Neu"))
    heater.values[(8, 20)] = 100

    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{serial_entry.entry_id}_reread_monitor_list"
    )
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(heater.requests_of("MA")) > requests
    assert hass.states.get(_entity_id(hass, serial_entry, 8, 20)).state == "10.0"


async def test_errors(
    hass: HomeAssistant,
    heater: FakeHeater,
    ptvo_entry: MockConfigEntry,
    transports: list[Transport],
) -> None:
    """Errors fire the event entity and are published to <topic>/error."""
    await _setup(hass, ptvo_entry)
    event = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{ptvo_entry.entry_id}_error"
    )
    heater.send_error("IM", "Saugzug blockiert")
    await hass.async_block_till_done()

    state = hass.states.get(event)
    assert state.attributes["event_type"] == "error"
    assert state.attributes["error_type"] == "IM"
    assert state.attributes["message"] == "Saugzug blockiert"

    published = [
        json.loads(payload)
        for topic, payload in transports[-1].backend.published  # type: ignore[attr-defined]
        if topic == f"{TOPIC}/error"
    ]
    assert len(published) == 1
    assert published[0]["ErrorType"] == "IM"
    assert published[0]["ErrorText"] == "Saugzug blockiert"
    assert len(published[0]["time"]) == 21


async def test_errors_not_published_when_disabled(
    hass: HomeAssistant,
    heater: FakeHeater,
    transports: list[Transport],
) -> None:
    """Publishing errors to MQTT can be turned off."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"connection": "ptvo_mqtt", "topic": TOPIC, "broker": "home_assistant"},
        options={CONF_PUBLISH_ERRORS: False},
    )
    await _setup(hass, entry)
    heater.send_error("SS", "x")
    await hass.async_block_till_done()
    assert not any(
        topic.endswith("/error")
        for topic, _ in transports[-1].backend.published  # type: ignore[attr-defined]
    )


async def test_not_ready(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Setup is retried when the heater cannot be reached."""
    heater.respond = False
    await _setup(hass, serial_entry)
    assert serial_entry.state is ConfigEntryState.SETUP_RETRY


async def test_connect_failure(
    hass: HomeAssistant, serial_entry: MockConfigEntry
) -> None:
    """Setup is retried when the port cannot be opened."""
    with patch(
        "tests.fake_heater.FakeSerialTransport.async_connect",
        side_effect=TransportError("busy"),
    ):
        await _setup(hass, serial_entry)
    assert serial_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_and_diagnostics(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Diagnostics list the monitor list; unloading stops the subscription."""
    await _setup(hass, serial_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, serial_entry)
    assert diagnostics["monitor_list"][0]["name"] == "Kesseltemperatur"
    assert diagnostics["monitor_list"][0]["value"] == 65.2

    unsubscribes = len(heater.requests_of("ME"))
    assert await hass.config_entries.async_unload(serial_entry.entry_id)
    assert serial_entry.state is ConfigEntryState.NOT_LOADED
    assert len(heater.requests_of("ME")) == unsubscribes + 1
