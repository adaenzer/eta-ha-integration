"""Tests for the client talking to the heater."""

from __future__ import annotations

import pytest

from custom_components.eta_heater.client import EtaClient, EtaTimeoutError
from custom_components.eta_heater.protocol import Frame
from custom_components.eta_heater.transport import PtvoMqttTransport

from .fake_heater import TOPIC, FakeHeater, FakeMqttBackend, FakeSerialTransport


async def test_read_monitor_list(heater: FakeHeater) -> None:
    """Headings and duplicates are skipped, levels are combined."""
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0, 1])
    assert [e.key for e in entries] == [
        (8, 1),
        (8, 2),
        (8, 3),
        (8, 4),
        (8, 5),
        (8, 6),
        (8, 7),
        (8, 8),
        (9, 300),
    ]
    assert entries[0].name == "Kesseltemperatur"


async def test_read_values_in_groups(heater: FakeHeater) -> None:
    """PTVO only gets six values per subscription."""
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    keys = [(8, i) for i in range(1, 9)] + [(9, 300), (9, 999)]
    values = await client.async_read_values(keys)
    assert values == {key: heater.values[key] for key in keys if key in heater.values}
    assert len(heater.requests_of("MC")) == 2
    assert len(heater.requests_of("ME")) == 2


async def test_no_response(heater: FakeHeater) -> None:
    """A heater that does not answer raises after retries."""
    heater.respond = False
    client = EtaClient(FakeSerialTransport(heater))
    with pytest.raises(EtaTimeoutError):
        await client.async_probe()
    assert len(heater.requests_of("MA")) == 3
    assert await client.async_read_values([(8, 1)]) == {}


async def test_error_listener(heater: FakeHeater) -> None:
    """Error frames go to the listeners, also during a request."""
    client = EtaClient(FakeSerialTransport(heater))
    errors: list[Frame] = []
    remove = client.add_error_listener(errors.append)
    heater.send_error("SE", "Fühler defekt")
    await client.async_probe()
    remove()
    heater.send_error("IM", "ignored")
    assert [(e.service, e.text) for e in errors] == [("SE", "Fühler defekt")]
