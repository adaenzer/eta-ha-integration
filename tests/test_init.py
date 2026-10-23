"""Tests for setting up the integration and its entities."""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
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
    """Set up the entry and wait for the first monitor list read and reload."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
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
    """The list is read on the first setup only; level changes need the button."""
    await _setup(hass, serial_entry)
    requests = len(heater.requests_of("MA"))
    assert requests == len(heater.monitor_list[0]) + 1

    await hass.config_entries.async_reload(serial_entry.entry_id)
    await hass.async_block_till_done()
    assert len(heater.requests_of("MA")) == requests

    issues = ir.async_get(hass)
    issue_id = f"levels_changed_{serial_entry.entry_id}"
    hass.config_entries.async_update_entry(serial_entry, options={CONF_LEVELS: [0, 1]})
    await hass.config_entries.async_reload(serial_entry.entry_id)
    await hass.async_block_till_done()
    assert len(heater.requests_of("MA")) == requests
    assert issues.async_get_issue(DOMAIN, issue_id)

    await _press_reread(hass, serial_entry)
    assert len(heater.requests_of("MA")) > requests
    assert _entity_id(hass, serial_entry, 9, 300)
    assert not issues.async_get_issue(DOMAIN, issue_id)


async def _press_reread(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_reread_monitor_list"
    )
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()


async def test_reread_button(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """The button reads the monitor list again and applies it."""
    await _setup(hass, serial_entry)
    heater.monitor_list[0].append(((8, 0, 20, 0, 176, 0, 0, 10, 0), "Neu"))
    heater.values[(8, 20)] = 100
    await _press_reread(hass, serial_entry)
    assert hass.states.get(_entity_id(hass, serial_entry, 8, 20)).state == "10.0"


async def test_reread_failure(
    hass: HomeAssistant,
    heater: FakeHeater,
    serial_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed re-read keeps the previous list and its values."""
    await _setup(hass, serial_entry)
    temp = _entity_id(hass, serial_entry, 8, 1)
    heater.respond = False
    await _press_reread(hass, serial_entry)
    assert "Reading the monitor list failed" in caplog.text
    assert serial_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(temp).state == "65.2"
    assert not serial_entry.runtime_data.rereading


async def test_reread_in_progress(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Pressing the button while reading is refused."""
    await _setup(hass, serial_entry)
    serial_entry.runtime_data.rereading = True
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{serial_entry.entry_id}_reread_monitor_list"
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
        )


async def test_remove_entry(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Removing the entry removes its repair issue."""
    await _setup(hass, serial_entry)
    hass.config_entries.async_update_entry(serial_entry, options={CONF_LEVELS: [1]})
    await hass.config_entries.async_reload(serial_entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"levels_changed_{serial_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    await hass.config_entries.async_remove(serial_entry.entry_id)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


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


async def test_first_read_fails(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """A failed first read is reported and can be repeated with the button."""
    heater.respond = False
    await _setup(hass, serial_entry)
    assert serial_entry.state is ConfigEntryState.LOADED
    issue_id = f"monitor_list_failed_{serial_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    heater.respond = True
    await _press_reread(hass, serial_entry)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert hass.states.get(_entity_id(hass, serial_entry, 8, 1)).state == "65.2"


async def test_setup_returns_before_list_is_read(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Setup does not wait for the slow monitor list read."""
    heater.respond = False
    serial_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(serial_entry.entry_id)
    assert serial_entry.state is ConfigEntryState.LOADED
    assert serial_entry.runtime_data.rereading
    await hass.async_block_till_done(wait_background_tasks=True)


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
    assert diagnostics["monitor_list"][0]["level"] == 0
    assert diagnostics["monitor_list"][0]["read"] is True

    unsubscribes = len(heater.requests_of("ME"))
    assert await hass.config_entries.async_unload(serial_entry.entry_id)
    assert serial_entry.state is ConfigEntryState.NOT_LOADED
    assert len(heater.requests_of("ME")) == unsubscribes + 1


def _requested_keys(heater: FakeHeater) -> set[tuple[int, int]]:
    keys = set()
    for frame in heater.requests_of("MC"):
        for pos in range(1, len(frame.payload), 3):
            node, hb, lb = frame.payload[pos : pos + 3]
            keys.add((node, hb * 256 + lb))
    return keys


async def test_value_selection(hass: HomeAssistant, heater: FakeHeater) -> None:
    """Only enabled entities are read; deeper levels start disabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"connection": "serial", "device": "/dev/ttyUSB0", "baudrate": 19200},
        options={CONF_LEVELS: [0, 1]},
    )
    await _setup(hass, entry)
    registry = er.async_get(hass)
    deep = _entity_id(hass, entry, 9, 300)
    assert registry.async_get(deep).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert (9, 300) not in _requested_keys(heater)
    assert (8, 1) in _requested_keys(heater)

    # Disabling an entity stops reading it
    temp = _entity_id(hass, entry, 8, 1)
    registry.async_update_entity(temp, disabled_by=er.RegistryEntryDisabler.USER)
    heater.requests.clear()
    await entry.runtime_data.async_refresh()
    assert (8, 1) not in _requested_keys(heater)

    # Enabling an entity reads it after the entry was reloaded
    registry.async_update_entity(deep, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (9, 300) in _requested_keys(heater)
    assert hass.states.get(deep).state == "40.0"

    # Nothing enabled: nothing is read and setup still works
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        registry.async_update_entity(
            entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    heater.requests.clear()
    await entry.runtime_data.async_refresh()
    assert not heater.requests_of("MC")
    assert entry.runtime_data.last_update_success


async def test_read_duration(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """The time a full read takes is shown as a diagnostic sensor."""
    await _setup(hass, serial_entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{serial_entry.entry_id}_read_duration"
    )
    state = hass.states.get(entity_id)
    assert float(state.state) >= 0
    assert state.attributes["unit_of_measurement"] == "s"


async def test_no_value_reads_during_reread(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """Value reads are skipped while the monitor list is read."""
    await _setup(hass, serial_entry)
    coordinator = serial_entry.runtime_data
    temp = _entity_id(hass, serial_entry, 8, 1)
    heater.requests.clear()
    coordinator.rereading = True
    await coordinator.async_refresh()
    assert not heater.requests
    assert hass.states.get(temp).state == "65.2"


async def test_only_foreign_values_fail_update(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """An update without any requested value marks the entities unavailable."""
    await _setup(hass, serial_entry)
    heater.foreign_values = True
    heater.answer_values = False
    await serial_entry.runtime_data.async_refresh()
    assert not serial_entry.runtime_data.last_update_success
    assert (
        hass.states.get(_entity_id(hass, serial_entry, 8, 1)).state == STATE_UNAVAILABLE
    )


async def test_names_and_attributes(hass: HomeAssistant, heater: FakeHeater) -> None:
    """Values sharing a name get a suffix; original data is kept as attributes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"connection": "serial", "device": "/dev/ttyUSB0", "baudrate": 19200},
        options={CONF_LEVELS: [2]},
    )
    await _setup(hass, entry)

    def state(node: int, index: int):
        return hass.states.get(_entity_id(hass, entry, node, index))

    names = {
        key: state(*key).attributes["friendly_name"].removeprefix("ETA heater ")
        for key in [
            (40, 33),
            (40, 34),
            (40, 23),
            (40, 29),
            (24, 54),
            (24, 58),
            (24, 63),
        ]
    }
    assert names == {
        (40, 33): "Erwärmung (STOKERSCHNECKE)",
        (40, 34): "Erwärmung (AUSTRAGUNG)",
        (40, 23): "AUSTRAGUNG (motor)",
        (40, 29): "AUSTRAGUNG (state)",
        (24, 54): "StellmotSOLL (P-Luftschieber)",
        (24, 58): "StellmotSOLL (S-Luftschieber)",
        (24, 63): "kg seit letzter Ent",
    }

    ash = state(24, 63)
    assert ash.state == "12.3"
    assert ash.attributes["unit_of_measurement"] == "kg"
    assert ash.attributes["state_class"] == "total_increasing"
    assert ash.attributes["monitor_name"] == "kg seit letzter  Ent"
    assert ash.attributes["node"] == 24
    assert ash.attributes["index"] == 63
    assert ash.attributes["value_type"] == 32
    assert ash.attributes["divider"] == 10
    assert ash.attributes["section"] == "BEDIENEINHEIT"

    assert state(40, 19).state == "1.5"  # 1500 mA with divider 1000
