"""Tests for the client talking to the heater."""

from __future__ import annotations

import asyncio
import itertools
from unittest.mock import patch

import pytest

from custom_components.eta_heater import client as client_module
from custom_components.eta_heater.client import EtaClient, EtaTimeoutError
from custom_components.eta_heater.protocol import Frame
from custom_components.eta_heater.transport import PtvoMqttTransport

from .fake_heater import TOPIC, FakeHeater, FakeMqttBackend, FakeSerialTransport


async def test_read_monitor_list(heater: FakeHeater) -> None:
    """The list is returned as read, with headings, levels in ascending order."""
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([1, 0])
    assert [e.name for e in entries] == [
        name for level in (0, 1) for _, name in heater.monitor_list[level]
    ]
    assert entries[0].is_heading
    assert {e.level for e in entries} == {0, 1}


async def test_read_values_in_groups(heater: FakeHeater) -> None:
    """PTVO only gets six values per subscription."""
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    keys = [(8, i) for i in range(1, 9)] + [(9, 300), (9, 999)]
    values = await client.async_read_values(keys)
    assert values == {key: heater.values[key] for key in keys if key in heater.values}
    assert len(heater.requests_of("MC")) == 2
    assert len(heater.requests_of("ME")) == 2
    assert [f.service for f in heater.requests] == ["MC", "ME", "MC", "ME"]


async def test_no_response(heater: FakeHeater) -> None:
    """A probe is not repeated; a list read gives up after three attempts."""
    heater.respond = False
    client = EtaClient(FakeSerialTransport(heater))
    with pytest.raises(EtaTimeoutError):
        await client.async_probe()
    assert len(heater.requests_of("MA")) == 1
    with pytest.raises(EtaTimeoutError):
        await client.async_read_monitor_list([0])
    assert len(heater.requests_of("MA")) == 4
    assert await client.async_read_values([(8, 1)]) == {}


async def test_slow_answers_not_repeated(heater: FakeHeater) -> None:
    """Requests wait for slow answers instead of being sent again."""
    heater.delays = [0.03] * 20  # below the 0.05 s response timeout
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0])
    assert len(entries) == len(heater.monitor_list[0])
    assert len(heater.requests_of("MA")) == len(heater.monitor_list[0]) + 1


async def test_lost_request_repeated_after_timeout(heater: FakeHeater) -> None:
    """A lost request is repeated only once the response timeout passed."""
    heater.drop = 1
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0])
    assert len(entries) == len(heater.monitor_list[0])
    ma = [
        t
        for f, t in zip(heater.requests, heater.request_times, strict=True)
        if f.service == "MA"
    ]
    assert len(ma) == len(heater.monitor_list[0]) + 2
    assert ma[1] - ma[0] >= client_module.RESPONSE_TIMEOUT


async def test_late_answer_not_taken_for_next(heater: FakeHeater) -> None:
    """A late answer to a lost request is dropped, not used for the next one."""
    heater.delays = [0.08]  # first answer arrives after the retry
    client = EtaClient(FakeSerialTransport(heater))
    with patch.object(client_module, "LATE_ANSWER_TIME", 0.1):
        entries = await client.async_read_monitor_list([0])
    assert [e.name for e in entries] == [n for _, n in heater.monitor_list[0]]


async def test_frame_gap(heater: FakeHeater) -> None:
    """Frames are sent with a minimum gap and never overlap operations."""
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    with patch.object(client_module, "FRAME_GAP", 0.01):
        await asyncio.gather(
            client.async_read_values([(8, i) for i in range(1, 9)]),
            client.async_read_monitor_list([1]),
        )
    gaps = [b - a for a, b in itertools.pairwise(heater.request_times)]
    assert min(gaps) >= 0.009
    services = [f.service for f in heater.requests]
    # the value read finished (with its unsubscribe) before the list read began
    assert services[:4] == ["MC", "ME", "MC", "ME"]
    assert services[4:6] == ["ME", "MO"]


async def test_values_stop_without_answer(heater: FakeHeater) -> None:
    """No further groups are requested when a group is not answered."""
    heater.answer_values = False
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    assert await client.async_read_values([(8, i) for i in range(1, 9)]) == {}
    assert [f.service for f in heater.requests] == ["MC", "ME"]


async def test_disconnect_unsubscribes(heater: FakeHeater) -> None:
    """Disconnecting stops a subscription."""
    client = EtaClient(FakeSerialTransport(heater))
    await client.async_disconnect()
    assert [f.service for f in heater.requests] == ["ME"]


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


async def test_foreign_values_ignored(heater: FakeHeater) -> None:
    """Values another program requested do not count as an answer."""
    heater.foreign_values = True
    client = EtaClient(FakeSerialTransport(heater))
    assert await client.async_read_values([(8, 1), (8, 2)]) == {(8, 1): 652, (8, 2): 3}
    assert client.foreign_value_frames == 1

    heater.answer_values = False
    heater.requests.clear()
    keys = [(8, i) for i in range(1, 9)]
    client.transport.max_values_per_request = 4  # type: ignore[misc]
    assert await client.async_read_values(keys) == {}
    assert [f.service for f in heater.requests] == ["MC", "ME"]
