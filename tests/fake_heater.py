"""A simulated ETA heater for tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from time import monotonic

from custom_components.eta_heater.protocol import Frame, FrameParser, build_frame
from custom_components.eta_heater.transport import Transport, TransportError

TOPIC = "zigbee2mqtt/heater"


def entry(node: int, index: int, value_type: int, divider: int = 1) -> tuple[int, ...]:
    """Return monitor list header bytes."""
    return (node, index // 256, index % 256, 0, value_type, 0, 0, divider, 0)


MONITOR_LIST = {
    0: [
        (entry(0, 0, 0), "Kessel"),  # heading, ignored
        (entry(8, 1, 176, 10), "Kesseltemperatur"),
        (entry(8, 2, 16), "Kesselstatus"),
        (entry(8, 3, 15), "Luftklappe"),
        (entry(8, 4, 176, 10), "Aussentemperatur"),
        (entry(8, 5, 99), "Unbekannt"),
        (entry(8, 6, 48), "Betriebsstunden"),
        (entry(8, 1, 176, 10), "Kesseltemperatur"),  # duplicate
        (entry(8, 7, 176, 10), "Puffer oben"),
        (entry(8, 8, 176, 10), "Puffer unten"),
    ],
    1: [(entry(9, 300, 176, 10), "Vorlauf MK1")],
}

VALUES = {
    (8, 1): 652,
    (8, 2): 3,
    (8, 3): 1,
    (8, 4): 0xFFF6,
    (8, 5): 7,
    (8, 6): 1234,
    (8, 7): 701,
    (8, 8): 455,
    (9, 300): 400,
}


class FakeHeater:
    """Answers requests like the heater's controller does."""

    def __init__(self) -> None:
        """Initialize the heater."""
        self.monitor_list = {level: list(e) for level, e in MONITOR_LIST.items()}
        self.values = dict(VALUES)
        self.respond = True
        self.answer_values = True
        # Requests to ignore, and delays (seconds) for the next answers
        self.drop = 0
        self.delays: list[float] = []
        self.requests: list[Frame] = []
        self.request_times: list[float] = []
        self.output: Callable[[bytes], None] = lambda data: None
        self._parser = FrameParser(self._handle)

    def receive(self, data: bytes) -> None:
        """Receive bytes sent to the heater."""
        self._parser.feed(data)

    def requests_of(self, service: str) -> list[Frame]:
        """Return the received requests of a service."""
        return [f for f in self.requests if f.service == service]

    def send_error(self, service: str, text: str) -> None:
        """Send an error frame."""
        self.output(build_frame(service, text.encode("latin-1")))

    def _send(self, frame: bytes) -> None:
        delay = self.delays.pop(0) if self.delays else 0
        asyncio.get_running_loop().call_later(delay, self.output, frame)

    def _handle(self, frame: Frame) -> None:
        self.requests.append(frame)
        self.request_times.append(monotonic())
        if not self.respond or frame.service not in ("MA", "MC"):
            return
        if self.drop:
            self.drop -= 1
            return
        if frame.service == "MA":
            position, level = frame.payload
            entries = self.monitor_list.get(level, [])
            payload = b""
            if position < len(entries):
                header, name = entries[position]
                payload = bytes(header) + f" {name} ".encode("latin-1")
            self._send(build_frame("Mb", payload))
        elif frame.service == "MC" and self.answer_values:
            payload = b""
            for pos in range(1, len(frame.payload), 3):
                node, hb, lb = frame.payload[pos : pos + 3]
                if (value := self.values.get((node, hb * 256 + lb))) is not None:
                    payload += bytes([node, hb, lb, value // 256, value % 256])
            self._send(build_frame("MD", payload))


class FakeSerialTransport(Transport):
    """Serial transport connected to a fake heater."""

    max_values_per_request = 20

    def __init__(self, heater: FakeHeater) -> None:
        """Initialize the transport."""
        super().__init__()
        self.heater = heater
        heater.output = self._received
        self.connect_error: Exception | None = None

    async def async_connect(self) -> None:
        """Connect."""
        if self.connect_error:
            raise self.connect_error

    async def async_disconnect(self) -> None:
        """Disconnect."""

    async def async_send(self, frame: bytes) -> None:
        """Send a frame to the heater."""
        self.heater.receive(frame)


class FakeMqttBackend:
    """MQTT backend that behaves like zigbee2mqtt with a PTVO device."""

    def __init__(self, heater: FakeHeater) -> None:
        """Initialize the backend."""
        self.heater = heater
        heater.output = self._from_heater
        self.published: list[tuple[str, str]] = []
        self.subscriptions: dict[str, Callable[[bytes], None]] = {}
        self.start_error: Exception | None = None

    async def async_start(self) -> None:
        """Connect."""
        if self.start_error:
            raise self.start_error

    async def async_stop(self) -> None:
        """Disconnect."""

    async def async_subscribe(
        self, topic: str, callback: Callable[[bytes], None]
    ) -> Callable[[], None]:
        """Subscribe."""
        self.subscriptions[topic] = callback
        return lambda: self.subscriptions.pop(topic, None)

    async def async_publish(
        self, topic: str, payload: str, retain: bool = False
    ) -> None:
        """Publish; messages to <topic>/set go to the heater."""
        self.published.append((topic, payload))
        if topic.endswith("/set"):
            action = json.loads(payload)["action"]
            assert action[0] == len(action) - 1
            self.heater.receive(bytes(action[1:]))

    def report(self, data: bytes) -> None:
        """Report UART data the way zigbee2mqtt does (packet end stripped)."""
        message = json.dumps(
            {"action": {str(i): b for i, b in enumerate(data)}, "linkquality": 120}
        )
        for callback in list(self.subscriptions.values()):
            callback(message.encode())

    def _from_heater(self, frame: bytes) -> None:
        assert frame.endswith(b"}")
        self.report(frame[:-1])


class FailingTransportError(TransportError):
    """Raised by tests to simulate a broken connection."""
