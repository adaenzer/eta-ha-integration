"""A simulated ETA heater for tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from time import monotonic

from custom_components.eta_heater.protocol import Frame, FrameParser, build_frame
from custom_components.eta_heater.transport import Transport, TransportError

TOPIC = "zigbee2mqtt/heater"

# Display frames as the controller sends them: 4 lines of 20 characters and
# 9 flags (position 6: green LED, position 8: red LED)
MAIN_DISPLAY = (
    f"{'20:29:59':>19} "
    f"{'TEMPERATURANZEIGEN':<19}\xb3"
    f"{'HACK.KES     Bereit':<20}"
    f"{'PUFFER      Geladen':<20}"
    "000001000"
)
INFO_DISPLAY = (
    f"{'Hackgutsteuerung 0':<20}"
    f"{'INFO':^20}"
    f"{'\xa0Kessel wurde per':<20}"
    f"{' SMS EIN geschaltet':<20}"
    "000000000"
)
ERROR_DISPLAY = (
    f"{'Austragung 0':<20}"
    f"{'** FEHLER **':^20}"
    f"{'Wartungsdeckel Fall-':<20}"
    f"{'schacht offen!':<20}"
    "000000010"
)


def entry(node: int, index: int, value_type: int, divider: int = 1) -> tuple[int, ...]:
    """Return monitor list header bytes."""
    return (
        node,
        index // 256,
        index % 256,
        0,
        value_type,
        0,
        divider // 256,
        divider % 256,
        0,
    )


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
    # Structure as found on a real heater: headings, motors starting groups,
    # values sharing names
    2: [
        (entry(0, 0, 0), "HACKGUTSTEUERUNG"),
        (entry(40, 22, 5), "STOKERSCHNECKE"),
        (entry(40, 19, 65, 1000), "Stoker Strom"),
        (entry(40, 33, 37), "Erwärmung"),
        (entry(40, 23, 5), "AUSTRAGUNG"),
        (entry(40, 20, 65, 1000), "Austrag. Strom"),
        (entry(40, 34, 37), "Erwärmung"),
        (entry(40, 29, 23), "AUSTRAGUNG"),
        (entry(0, 0, 0), "BEDIENEINHEIT"),
        (entry(24, 18, 37), "P-Luftschieber"),
        (entry(24, 54, 37), "StellmotSOLL"),
        (entry(24, 57, 37), "S-Luftschieber"),
        (entry(24, 58, 37), "StellmotSOLL"),
        (entry(24, 63, 32, 10), "kg seit letzter  Ent"),
        (entry(24, 91, 115), "Zustandstimer"),
    ],
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
    (40, 22): 1,
    (40, 19): 1500,
    (40, 33): 11,
    (40, 23): 0,
    (40, 20): 0,
    (40, 34): 8,
    (40, 29): 0,
    (24, 18): 2,
    (24, 54): 0,
    (24, 57): 1,
    (24, 58): 0,
    (24, 63): 123,
    (24, 91): 6825,
}


class FakeHeater:
    """Answers requests like the heater's controller does."""

    def __init__(self) -> None:
        """Initialize the heater."""
        self.monitor_list = {level: list(e) for level, e in MONITOR_LIST.items()}
        self.values = dict(VALUES)
        self.respond = True
        self.answer_values = True
        # Numbers (1, 2, ...) of value answers lost on the way
        self.lost_value_answers: set[int] = set()
        self._mc_answers = 0
        self.foreign_values = False
        self.mirroring = False
        # Display frames of which only the end arrives
        self.display_lost = 0
        # 4 lines of 20 characters and 9 flags, as the controller sends them
        self.display = MAIN_DISPLAY
        self.keys: list[int] = []
        # Like the real controller: the first request of another level is
        # answered with "end of list" first and the actual entry shortly after
        self.level_switch_quirk = True
        self._level: int | None = None
        self._cursor = 0
        self._out_at = 0.0
        # Numbers (1, 2, ...) of monitor list answers lost on the way
        self.lost_answers: set[int] = set()
        self._ma_answers = 0
        # Requests to ignore, and delays (seconds) for the next answers
        self.drop = 0
        self.delays: list[float] = []
        self.requests: list[Frame] = []
        self.request_times: list[float] = []
        self.output: Callable[[bytes], None] = lambda data: None
        self._parser = FrameParser(self._handle)

    def receive(self, data: bytes) -> None:
        """Receive bytes sent to the heater."""
        if data.startswith(b"{IT"):
            # Key frames do not carry the usual checksum
            self.keys.append(data[5])
            if data[5] == 1 and self.display != MAIN_DISPLAY:
                self.display = MAIN_DISPLAY
                if self.mirroring:
                    self._send(build_frame("IE", self.display.encode("latin-1")))
            return
        self._parser.feed(data)

    def requests_of(self, service: str) -> list[Frame]:
        """Return the received requests of a service."""
        return [f for f in self.requests if f.service == service]

    def send_error(self, service: str, text: str) -> None:
        """Send an error frame."""
        self.output(build_frame(service, text.encode("latin-1")))

    def _send(self, frame: bytes) -> None:
        # Frames arrive in the order they were sent, also when one is late
        loop = asyncio.get_running_loop()
        delay = self.delays.pop(0) if self.delays else 0
        self._out_at = max(loop.time() + delay, self._out_at)
        loop.call_at(self._out_at, self.output, frame)

    def _handle(self, frame: Frame) -> None:
        self.requests.append(frame)
        self.request_times.append(monotonic())
        if frame.service == "II" and self.respond:
            self.mirroring = frame.payload == b"1"
            if not self.mirroring:
                return
            if self.display_lost:
                # Over Zigbee only the end of the frame arrives
                self.display_lost -= 1
                self._send(self.display.encode("latin-1")[-28:])
                return
            self._send(build_frame("IE", self.display.encode("latin-1")))
            return
        if not self.respond or frame.service not in ("MA", "MC"):
            return
        if self.drop:
            self.drop -= 1
            return
        if frame.service == "MA":
            self._answer_monitor_list(*frame.payload)
        elif frame.service == "MC" and self.foreign_values:
            # Answer to a request of another program talking to the heater
            self._send(build_frame("MD", bytes([24, 0, 153, 0, 7])))
            if self.answer_values:
                self._answer_values(frame)
        elif frame.service == "MC" and self.answer_values:
            self._answer_values(frame)

    def _answer_monitor_list(self, position: int, level: int) -> None:
        """Answer like the real controller, which keeps a cursor.

        Position 0 (or another level) starts at the first entry; any other
        request returns the entry after the previous one, whatever position
        it asks for.
        """
        entries = self.monitor_list.get(level, [])
        switched = self._level is not None and level != self._level
        if position == 0 or level != self._level:
            self._cursor = 0
        self._level = level
        payload = b""
        if self._cursor < len(entries):
            header, name = entries[self._cursor]
            payload = bytes(header) + f" {name} ".encode("latin-1")
            self._cursor += 1
        self._ma_answers += 1
        if self._ma_answers in self.lost_answers:
            return
        if self.level_switch_quirk and switched and payload:
            # After another level the first answer is a stale "end of list"
            loop = asyncio.get_running_loop()
            delay = self.delays.pop(0) if self.delays else 0
            loop.call_later(delay, self.output, build_frame("Mb", b""))
            loop.call_later(delay + 0.01, self.output, build_frame("Mb", payload))
            return
        self._send(build_frame("Mb", payload))

    def _answer_values(self, frame: Frame) -> None:
        self._mc_answers += 1
        if self._mc_answers in self.lost_value_answers:
            return
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
