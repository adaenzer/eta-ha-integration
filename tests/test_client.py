"""Tests for the client talking to the heater."""

from __future__ import annotations

import asyncio
import itertools
from unittest.mock import patch

import pytest

from custom_components.eta_heater import client as client_module
from custom_components.eta_heater.client import EtaClient, EtaTimeoutError
from custom_components.eta_heater.protocol import (
    Frame,
    build_monitor_list,
    merge_monitor_lists,
)
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
    # Connecting stops subscriptions left by anyone else
    assert [f.service for f in heater.requests] == ["ME"]
    heater.requests.clear()
    heater.request_times.clear()
    keys = [(8, i) for i in range(1, 9)] + [(9, 300), (9, 999)]
    values = await client.async_read_values(keys)
    assert values == {key: heater.values[key] for key in keys if key in heater.values}
    # node 8: six and two values, node 9: its own group, sorted by index
    groups = [
        [
            (f.payload[i], f.payload[i + 1] * 256 + f.payload[i + 2])
            for i in range(1, len(f.payload), 3)
        ]
        for f in heater.requests_of("MC")
    ]
    assert groups == [
        [(8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 6)],
        [(8, 7), (8, 8)],
        [(9, 300), (9, 999)],
    ]
    assert [f.service for f in heater.requests] == ["MC", "ME"] * 3


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


async def test_frame_gap(heater: FakeHeater) -> None:
    """Frames are sent with a minimum gap and never overlap operations."""
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    # Connecting stops subscriptions left by anyone else
    assert [f.service for f in heater.requests] == ["ME"]
    heater.requests.clear()
    heater.request_times.clear()
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
    """After two groups without answer no further groups are requested."""
    heater.answer_values = False
    client = EtaClient(PtvoMqttTransport(FakeMqttBackend(heater), TOPIC))
    await client.async_connect()
    # Connecting stops subscriptions left by anyone else
    assert [f.service for f in heater.requests] == ["ME"]
    heater.requests.clear()
    heater.request_times.clear()
    client.transport.max_values_per_request = 3  # type: ignore[misc]
    assert await client.async_read_values([(8, i) for i in range(1, 9)]) == {}
    assert [f.service for f in heater.requests] == ["MC", "ME", "MC", "ME"]


async def test_values_lost_answer_continues(heater: FakeHeater) -> None:
    """A group whose answer got lost does not stop the read."""
    heater.lost_value_answers = {1}
    client = EtaClient(FakeSerialTransport(heater))
    client.transport.max_values_per_request = 3  # type: ignore[misc]
    values = await client.async_read_values([(8, i) for i in range(1, 9)])
    assert set(values) == {(8, i) for i in range(4, 9)}


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
    assert [f.service for f in heater.requests] == ["MC", "ME", "MC", "ME"]


async def test_level_switch_quirk(heater: FakeHeater) -> None:
    """A premature "end of list" after a level change does not skip the level."""
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0, 1, 2])
    expected = [n for level in (0, 1, 2) for _, n in heater.monitor_list[level]]
    assert [e.name for e in entries] == expected

    heater.level_switch_quirk = False
    assert [e.name for e in await client.async_read_monitor_list([0, 1, 2])] == expected


async def test_slow_answers_not_repeated(heater: FakeHeater) -> None:
    """Requests wait for slow answers instead of being sent again."""
    heater.delays = [0.03] * 20  # below the 0.05 s response timeout
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0])
    assert [e.name for e in entries] == [n for _, n in heater.monitor_list[0]]
    # one request per entry and one that got "end of list"
    assert len(heater.requests_of("MA")) == len(heater.monitor_list[0]) + 1


async def test_late_answer_kept(heater: FakeHeater) -> None:
    """An answer arriving after the timeout is still kept, in order."""
    heater.delays = [0.08]
    client = EtaClient(FakeSerialTransport(heater))
    entries = await client.async_read_monitor_list([0])
    assert [e.name for e in entries] == [n for _, n in heater.monitor_list[0]]
    ma = [
        t
        for f, t in zip(heater.requests, heater.request_times, strict=True)
        if f.service == "MA"
    ]
    # the second request waited for the response timeout
    assert ma[1] - ma[0] >= client_module.RESPONSE_TIMEOUT


async def test_lost_answer_missing_then_merged(heater: FakeHeater) -> None:
    """A lost answer leaves a gap that a second read fills."""
    heater.lost_answers = {3}
    client = EtaClient(FakeSerialTransport(heater))
    first = build_monitor_list(
        await client.async_read_monitor_list([0]), lambda e: False
    )
    all_names = [n for _, n in heater.monitor_list[0] if n != "Kessel"]
    missing = heater.monitor_list[0][2][1]
    assert missing not in [e.name for e in first]

    second = build_monitor_list(
        await client.async_read_monitor_list([0]), lambda e: False
    )
    merged = merge_monitor_lists(first, second)
    assert sorted({e.name for e in merged}) == sorted(set(all_names))


async def test_too_many_lost_answers(heater: FakeHeater) -> None:
    """Reading gives up after several lost answers in a row."""
    heater.lost_answers = {2, 3, 4}
    client = EtaClient(FakeSerialTransport(heater))
    with pytest.raises(EtaTimeoutError):
        await client.async_read_monitor_list([0])


async def test_display_asked_again_until_complete(heater: FakeHeater) -> None:
    """Mirroring is requested again when only the end of a frame arrived."""
    heater.display_lost = 2
    client = EtaClient(FakeSerialTransport(heater))
    display = await client.async_read_display()
    assert display.complete
    assert display.lines[2] == "HACK.KES Bereit"
    assert [f.payload for f in heater.requests_of("II")] == [b"1", b"1", b"1", b"0"]


async def test_display_tail_as_fallback(heater: FakeHeater) -> None:
    """If no complete frame arrives, the flags from a frame end are used."""
    heater.display_lost = 10
    client = EtaClient(FakeSerialTransport(heater))
    display = await client.async_read_display()
    assert not display.complete
    assert display.flags == "000001000"
    assert display.led_green
    assert display.text == "… UFFER Geladen"  # the beginning got lost
    assert heater.requests_of("II")[-1].payload == b"0"

    heater.respond = False
    with pytest.raises(EtaTimeoutError):
        await client.async_read_display()
