"""Tests for the config flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eta_heater.config_flow import _async_list_serial_ports
from custom_components.eta_heater.connection import MqttNotAvailableError
from custom_components.eta_heater.const import (
    BROKER_EXTERNAL,
    BROKER_HOME_ASSISTANT,
    CONF_BAUDRATE,
    CONF_BROKER,
    CONF_CONNECTION,
    CONF_LEVELS,
    CONF_PUBLISH_ERRORS,
    CONF_TOPIC,
    CONNECTION_PTVO_MQTT,
    CONNECTION_SERIAL,
    DOMAIN,
)
from custom_components.eta_heater.transport import TransportError

from .conftest import PTVO_DATA, SERIAL_DATA
from .fake_heater import TOPIC, FakeHeater

pytestmark = pytest.mark.usefixtures("transports")


async def _finish_levels(hass: HomeAssistant, result: dict) -> dict:
    """Complete the levels step that follows a successful connection test."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "levels"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LEVELS: ["1", "0"]}
    )
    # Let the monitor list read and the reload finish while the fakes exist
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    return result


async def _start(hass: HomeAssistant, step: str) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [CONNECTION_PTVO_MQTT, CONNECTION_SERIAL]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    return result["flow_id"]


async def test_ptvo_home_assistant_broker(hass: HomeAssistant) -> None:
    """PTVO over the HA MQTT integration."""
    flow_id = await _start(hass, CONNECTION_PTVO_MQTT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_TOPIC: f" {TOPIC}/ ", CONF_BROKER: BROKER_HOME_ASSISTANT}
    )
    result = await _finish_levels(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_LEVELS: [0, 1]}
    assert result["title"] == f"ETA heater ({TOPIC})"
    assert result["data"] == PTVO_DATA


async def test_ptvo_external_broker(hass: HomeAssistant) -> None:
    """PTVO over an external broker asks for its settings."""
    flow_id = await _start(hass, CONNECTION_PTVO_MQTT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_TOPIC: TOPIC, CONF_BROKER: BROKER_EXTERNAL}
    )
    assert result["step_id"] == "broker"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_HOST: "10.0.0.2",
            CONF_PORT: 1884,
            CONF_USERNAME: "z2m",
            CONF_PASSWORD: "pw",
        },
    )
    result = await _finish_levels(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CONNECTION: CONNECTION_PTVO_MQTT,
        CONF_TOPIC: TOPIC,
        CONF_BROKER: BROKER_EXTERNAL,
        CONF_HOST: "10.0.0.2",
        CONF_PORT: 1884,
        CONF_USERNAME: "z2m",
        CONF_PASSWORD: "pw",
    }


async def test_serial(hass: HomeAssistant) -> None:
    """Direct serial connection."""
    with patch(
        "custom_components.eta_heater.config_flow._async_list_serial_ports",
        return_value=["/dev/ttyUSB0"],
    ):
        flow_id = await _start(hass, CONNECTION_SERIAL)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_DEVICE: SERIAL_DATA[CONF_DEVICE], CONF_BAUDRATE: 19200}
        )
    result = await _finish_levels(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == SERIAL_DATA


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MqttNotAvailableError("no mqtt"), "mqtt_not_available"),
        (TransportError("refused"), "cannot_connect"),
        (None, "no_response"),
    ],
)
async def test_errors_and_recovery(
    hass: HomeAssistant,
    heater: FakeHeater,
    error: Exception | None,
    expected: str,
) -> None:
    """Connection problems are shown and the user can try again."""
    flow_id = await _start(hass, CONNECTION_PTVO_MQTT)
    user_input = {CONF_TOPIC: TOPIC, CONF_BROKER: BROKER_HOME_ASSISTANT}

    heater.respond = error is not None
    with patch(
        "tests.fake_heater.FakeMqttBackend.async_start",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    heater.respond = True
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    result = await _finish_levels(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_already_configured(
    hass: HomeAssistant, ptvo_entry: MockConfigEntry
) -> None:
    """The same topic on the same broker cannot be added twice."""
    ptvo_entry.add_to_hass(hass)
    flow_id = await _start(hass, CONNECTION_PTVO_MQTT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_TOPIC: TOPIC, CONF_BROKER: BROKER_HOME_ASSISTANT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(hass: HomeAssistant, ptvo_entry: MockConfigEntry) -> None:
    """The connection settings can be changed, also to the same values."""
    ptvo_entry.add_to_hass(hass)
    result = await ptvo_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == CONNECTION_PTVO_MQTT
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOPIC: TOPIC, CONF_BROKER: BROKER_HOME_ASSISTANT}
    )
    assert result.get("errors") is None, result
    assert result["reason"] == "reconfigure_successful"

    result = await ptvo_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC: "zigbee2mqtt/eta", CONF_BROKER: BROKER_HOME_ASSISTANT},
    )
    assert result["reason"] == "reconfigure_successful"
    assert ptvo_entry.data[CONF_TOPIC] == "zigbee2mqtt/eta"
    assert ptvo_entry.title == "ETA heater (zigbee2mqtt/eta)"


async def test_reconfigure_serial(
    hass: HomeAssistant, serial_entry: MockConfigEntry
) -> None:
    """A serial entry is reconfigured with the serial form."""
    serial_entry.add_to_hass(hass)
    with patch(
        "custom_components.eta_heater.config_flow._async_list_serial_ports",
        return_value=[],
    ):
        result = await serial_entry.start_reconfigure_flow(hass)
        assert result["step_id"] == CONNECTION_SERIAL
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB1", CONF_BAUDRATE: 9600}
        )
    assert result["reason"] == "reconfigure_successful"
    assert serial_entry.data[CONF_DEVICE] == "/dev/ttyUSB1"


async def test_options(hass: HomeAssistant, ptvo_entry: MockConfigEntry) -> None:
    """Levels must be numbers; PTVO entries can publish errors."""
    ptvo_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(ptvo_entry.entry_id)
    assert CONF_PUBLISH_ERRORS in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_LEVELS: [], CONF_SCAN_INTERVAL: 60, CONF_PUBLISH_ERRORS: False},
    )
    assert result["errors"] == {CONF_LEVELS: "invalid_levels"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_LEVELS: ["2", "0", "2"],
            CONF_SCAN_INTERVAL: 60,
            CONF_PUBLISH_ERRORS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ptvo_entry.options == {
        CONF_LEVELS: [0, 2],
        CONF_SCAN_INTERVAL: 60,
        CONF_PUBLISH_ERRORS: False,
    }


async def test_options_serial(
    hass: HomeAssistant, serial_entry: MockConfigEntry
) -> None:
    """Serial entries have no MQTT error publishing."""
    serial_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(serial_entry.entry_id)
    assert CONF_PUBLISH_ERRORS not in result["data_schema"].schema


async def test_list_serial_ports(hass: HomeAssistant, tmp_path) -> None:
    """Ports are listed with their by-id links where available."""
    ports = [
        SimpleNamespace(device="/dev/ttyUSB0"),
        SimpleNamespace(device="/dev/ttyS0"),
    ]
    with patch("serialx.async_list_serial_ports", return_value=ports):
        result = await _async_list_serial_ports(hass)
    assert sorted(result) == result
    assert len(result) == 2

    with patch("serialx.async_list_serial_ports", side_effect=OSError):
        assert await _async_list_serial_ports(hass) == []


async def test_levels_required(hass: HomeAssistant) -> None:
    """At least one level must be selected."""
    flow_id = await _start(hass, CONNECTION_PTVO_MQTT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_TOPIC: TOPIC, CONF_BROKER: BROKER_HOME_ASSISTANT}
    )
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_LEVELS: []})
    assert result["errors"] == {CONF_LEVELS: "invalid_levels"}


async def test_reconfigure_loaded_entry(
    hass: HomeAssistant, heater: FakeHeater, serial_entry: MockConfigEntry
) -> None:
    """A running entry is stopped while probing and restored on failure."""
    serial_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(serial_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert serial_entry.state is ConfigEntryState.LOADED

    with patch(
        "custom_components.eta_heater.config_flow._async_list_serial_ports",
        return_value=[],
    ):
        result = await serial_entry.start_reconfigure_flow(hass)
        heater.respond = False
        heater.requests.clear()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB1", CONF_BAUDRATE: 19200}
        )
        assert result["errors"] == {"base": "no_response"}
        # the entry unsubscribed before the probe and was set up again
        assert heater.requests[0].service == "ME"
        assert serial_entry.state is ConfigEntryState.SETUP_RETRY

        heater.respond = True
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB1", CONF_BAUDRATE: 19200}
        )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert serial_entry.state is ConfigEntryState.LOADED
