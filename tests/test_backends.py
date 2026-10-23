"""Tests for the real MQTT backends and the serial transport."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.typing import MqttMockHAClient

from custom_components.eta_heater.connection import (
    HomeAssistantMqttBackend,
    MqttNotAvailableError,
    create_transport,
)
from custom_components.eta_heater.transport import (
    PahoMqttBackend,
    PtvoMqttTransport,
    SerialTransport,
    TransportError,
)

from .conftest import PTVO_DATA, SERIAL_DATA


# The MQTT integration keeps a periodic timer running until it is unloaded
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_home_assistant_backend(
    hass: HomeAssistant, mqtt_mock: MqttMockHAClient
) -> None:
    """The HA MQTT integration is used for subscribing and publishing."""
    backend = HomeAssistantMqttBackend(hass)
    await backend.async_start()
    received: list[bytes] = []
    unsubscribe = await backend.async_subscribe("eta/test", received.append)
    async_fire_mqtt_message(hass, "eta/test", b'{"action": {}}')
    await hass.async_block_till_done()
    assert received == [b'{"action": {}}']
    unsubscribe()

    await backend.async_publish("eta/test/set", "x")
    mqtt_mock.async_publish.assert_called_once()
    assert mqtt_mock.async_publish.call_args.args == ("eta/test/set", "x", 0, False)
    await backend.async_stop()


async def test_home_assistant_backend_missing(hass: HomeAssistant) -> None:
    """Without the MQTT integration the backend cannot start."""
    with pytest.raises(MqttNotAvailableError):
        await HomeAssistantMqttBackend(hass).async_start()


def test_create_transport(hass: HomeAssistant) -> None:
    """The config entry data selects the transport and backend."""
    transport = create_transport(hass, PTVO_DATA)
    assert isinstance(transport, PtvoMqttTransport)
    assert isinstance(transport.backend, HomeAssistantMqttBackend)
    transport = create_transport(
        hass,
        {**PTVO_DATA, "broker": "external", "host": "broker", "port": 1883},
    )
    assert isinstance(transport.backend, PahoMqttBackend)  # type: ignore[attr-defined]
    transport = create_transport(hass, SERIAL_DATA)
    assert isinstance(transport, SerialTransport)
    assert transport.port == SERIAL_DATA["device"]


def _paho_client(reason_failure: bool = False) -> MagicMock:
    client = MagicMock()

    def connect(host: str, port: int) -> None:
        reason = SimpleNamespace(is_failure=reason_failure)
        client.on_connect(client, None, None, reason, None)

    client.connect.side_effect = connect
    return client


async def test_paho_backend(hass: HomeAssistant) -> None:
    """An external broker is used through paho-mqtt."""
    client = _paho_client()
    backend = PahoMqttBackend("broker", 1883, "user", "pw", hass.async_add_executor_job)
    with patch("paho.mqtt.client.Client", return_value=client):
        await backend.async_start()
    client.username_pw_set.assert_called_once_with("user", "pw")
    client.loop_start.assert_called_once()

    received: list[bytes] = []
    unsubscribe = await backend.async_subscribe("eta", received.append)
    client.subscribe.assert_called_with("eta")
    client.on_message(client, None, SimpleNamespace(topic="eta", payload=b"data"))
    client.on_message(client, None, SimpleNamespace(topic="other", payload=b"x"))
    await hass.async_block_till_done()
    assert received == [b"data"]

    # subscriptions are renewed after a reconnect
    client.subscribe.reset_mock()
    client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    client.subscribe.assert_called_with("eta")

    await backend.async_publish("eta/set", "x")
    client.publish.assert_called_with("eta/set", "x", retain=False)
    unsubscribe()
    client.unsubscribe.assert_called_with("eta")

    await backend.async_stop()
    client.disconnect.assert_called_once()
    with pytest.raises(TransportError):
        await backend.async_publish("eta/set", "x")


async def test_paho_backend_failures(hass: HomeAssistant) -> None:
    """Connection problems raise TransportError."""
    backend = PahoMqttBackend("broker", 1883, None, None, hass.async_add_executor_job)
    with (
        patch("paho.mqtt.client.Client", return_value=_paho_client(True)),
        pytest.raises(TransportError, match="refused"),
    ):
        await backend.async_start()

    failing = MagicMock()
    failing.connect.side_effect = OSError("unreachable")
    with (
        patch("paho.mqtt.client.Client", return_value=failing),
        pytest.raises(TransportError, match="unreachable"),
    ):
        await backend.async_start()


async def test_serial_transport() -> None:
    """The serial port is opened with serialx and passes data through."""
    serial = MagicMock()
    created = {}

    async def create(loop, protocol_factory, url, baudrate):
        created["protocol"] = protocol_factory()
        created["args"] = (url, baudrate)
        return serial, created["protocol"]

    transport = SerialTransport("/dev/ttyUSB0")
    received: list[bytes] = []
    transport.set_receiver(received.append)
    with pytest.raises(TransportError):
        await transport.async_send(b"x")
    with patch("serialx.create_serial_connection", create):
        await transport.async_connect()
    assert created["args"] == ("/dev/ttyUSB0", 19200)

    await transport.async_send(b"{ME\x00\x00}")
    serial.write.assert_called_once_with(b"{ME\x00\x00}")
    created["protocol"].data_received(b"{MD")
    assert received == [b"{MD"]
    await transport.async_disconnect()
    serial.close.assert_called_once()

    async def fail(*args, **kwargs):
        raise OSError("no such device")

    with (
        patch("serialx.create_serial_connection", fail),
        pytest.raises(TransportError, match="no such device"),
    ):
        await transport.async_connect()
