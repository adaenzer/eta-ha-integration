"""Tests for the PTVO MQTT transport."""

from __future__ import annotations

import json

from custom_components.eta_heater.transport import PtvoMqttTransport, decode_ptvo_action

from .fake_heater import TOPIC, FakeHeater, FakeMqttBackend


def test_decode_ptvo_action() -> None:
    """All forms zigbee2mqtt may use for the byte array are accepted."""
    assert decode_ptvo_action({"0": 123, "1": 77, "2": 68}) == b"{MD"
    assert decode_ptvo_action([123, 77]) == b"{M"
    assert decode_ptvo_action({"type": "Buffer", "data": [123]}) == b"{"
    assert decode_ptvo_action("single") is None


async def test_ptvo_transport() -> None:
    """Frames are sent with a length prefix and received with '}' restored."""
    backend = FakeMqttBackend(FakeHeater())
    transport = PtvoMqttTransport(backend, TOPIC)
    received: list[bytes] = []
    transport.set_receiver(received.append)
    await transport.async_connect()

    backend.heater.receive = lambda data: None  # type: ignore[method-assign]
    await transport.async_send(b"{ME\x00\x00}")
    assert backend.published == [
        (f"{TOPIC}/set", json.dumps({"action": [6, *b"{ME\x00\x00}"]}))
    ]

    backend.report(b"{MD\x00\x00")
    backend.report(b"{MD")  # too short to be a frame
    backend.subscriptions[TOPIC](b'{"linkquality": 100}')
    backend.subscriptions[TOPIC](b"not json")
    assert received == [b"{MD\x00\x00}"]

    await transport.async_disconnect()
    assert TOPIC not in backend.subscriptions
