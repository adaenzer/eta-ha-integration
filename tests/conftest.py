"""Fixtures for the ETA heater tests."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_DEVICE
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eta_heater import client as client_module
from custom_components.eta_heater.const import (
    BROKER_HOME_ASSISTANT,
    CONF_BAUDRATE,
    CONF_BROKER,
    CONF_CONNECTION,
    CONF_TOPIC,
    CONNECTION_PTVO_MQTT,
    CONNECTION_SERIAL,
    DOMAIN,
)
from custom_components.eta_heater.transport import PtvoMqttTransport, Transport

from .fake_heater import TOPIC, FakeHeater, FakeMqttBackend, FakeSerialTransport

PTVO_DATA = {
    CONF_CONNECTION: CONNECTION_PTVO_MQTT,
    CONF_TOPIC: TOPIC,
    CONF_BROKER: BROKER_HOME_ASSISTANT,
}
SERIAL_DATA = {
    CONF_CONNECTION: CONNECTION_SERIAL,
    CONF_DEVICE: "/dev/serial/by-id/usb-eta",
    CONF_BAUDRATE: 19200,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture(autouse=True)
def short_timeouts() -> Generator[None]:
    """Make the client give up quickly."""
    with (
        patch.object(client_module, "RESPONSE_TIMEOUT", 0.05),
        patch.object(client_module, "VALUES_TIMEOUT", 0.1),
    ):
        yield


@pytest.fixture
def heater() -> FakeHeater:
    """Return a simulated heater."""
    return FakeHeater()


@pytest.fixture
def transports(heater: FakeHeater) -> Generator[list[Transport]]:
    """Connect every transport the integration creates to the fake heater."""
    created: list[Transport] = []

    def create(hass: Any, data: Mapping[str, Any]) -> Transport:
        transport: Transport
        if data[CONF_CONNECTION] == CONNECTION_SERIAL:
            transport = FakeSerialTransport(heater)
        else:
            transport = PtvoMqttTransport(FakeMqttBackend(heater), data[CONF_TOPIC])
        created.append(transport)
        return transport

    with (
        patch("custom_components.eta_heater.create_transport", create),
        patch("custom_components.eta_heater.config_flow.create_transport", create),
    ):
        yield created


@pytest.fixture
def ptvo_entry() -> MockConfigEntry:
    """Return a config entry using PTVO over the HA MQTT integration."""
    return MockConfigEntry(domain=DOMAIN, title="ETA heater", data=PTVO_DATA)


@pytest.fixture
def serial_entry() -> MockConfigEntry:
    """Return a config entry using a serial port."""
    return MockConfigEntry(domain=DOMAIN, title="ETA heater", data=SERIAL_DATA)
