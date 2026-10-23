"""Framing and payload decoding for the ETA heater RS232 protocol.

Every frame looks like this:

    '{'  service id (2 bytes)  payload length  checksum  payload  '}'

The checksum is the sum of all payload bytes modulo 256. See
Protokoll_ETA.pdf in the etainterface repository for the documented subset of
the protocol; the error services SE and SS were found while testing and use
the same frame format as the documented IM service.

This module has no Home Assistant dependencies so it can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from time import monotonic

_LOGGER = logging.getLogger(__name__)

FRAME_START = 0x7B  # '{'
FRAME_END = 0x7D  # '}'
FRAME_OVERHEAD = 6  # start, service id (2), length, checksum, end
MAX_BUFFER_SIZE = 512
# An incomplete frame older than this is dropped when a new frame starts, so
# that a lost fragment does not corrupt the next frame
STALE_FRAME_TIME = 2.0

# Request -> response service ids
SERVICE_MONITOR_LIST = "MA"  # -> Mb, one monitor list entry per request
SERVICE_MONITOR_LIST_ENTRY = "Mb"
SERVICE_SUBSCRIBE = "MC"  # -> MD, values sent cyclically
SERVICE_VALUES = "MD"
SERVICE_UNSUBSCRIBE = "ME"
SERVICE_PARAMETER_INDEX = "MF"  # -> Mg
SERVICE_PARAMETER_INDEX_ENTRY = "Mg"
SERVICE_STOP = "MO"  # stops any list transfer in progress

ERROR_SERVICES = frozenset({"IM", "SE", "SS"})
FRAME_STARTS = (b"{I", b"{M", b"{P", b"{S")

ENCODING = "latin-1"
MONITOR_ENTRY_HEADER_LEN = 9
VALUE_RECORD_LEN = 5
MAX_REFRESH_SECONDS = 255


def clean_display_text(text: str) -> str:
    """Join the lines of a text formatted for the heater's display.

    The display wraps long words with a hyphen ("Fall-" / "schacht"); such
    words are joined again, other line breaks become a space.
    """
    result = ""
    for line in (" ".join(line.split()) for line in text.splitlines()):
        if not line:
            continue
        if result.endswith("-") and line[0].islower():
            result = result[:-1] + line
        else:
            result = f"{result} {line}" if result else line
    return result


def checksum(payload: bytes) -> int:
    """Return the frame checksum of a payload."""
    return sum(payload) % 256


def build_frame(service: str, payload: bytes = b"") -> bytes:
    """Build a complete frame for a service id and payload."""
    if len(payload) > 255:
        raise ValueError("payload too long")
    return (
        bytes([FRAME_START])
        + service.encode("ascii")
        + bytes([len(payload), checksum(payload)])
        + payload
        + bytes([FRAME_END])
    )


@dataclass(frozen=True, slots=True)
class Frame:
    """A frame received from the heater."""

    service: str
    payload: bytes
    checksum_ok: bool

    @property
    def is_error(self) -> bool:
        """Return True if this frame reports an error."""
        return self.service in ERROR_SERVICES

    @property
    def text(self) -> str:
        """Return the payload decoded as text."""
        return self.payload.decode(ENCODING).strip()

    @property
    def message(self) -> str:
        """Return the text as one line, see clean_display_text."""
        return clean_display_text(self.text)


class FrameParser:
    """Reassemble frames from a byte stream that may arrive in chunks."""

    def __init__(self, on_frame: Callable[[Frame], None]) -> None:
        """Initialize the parser."""
        self._on_frame = on_frame
        self._buffer = bytearray()
        self._last_data = 0.0

    def reset(self) -> None:
        """Drop any partially received data."""
        self._buffer.clear()

    def feed(self, data: bytes) -> None:
        """Add received bytes and emit every complete frame."""
        now = monotonic()
        if (
            self._buffer
            and now - self._last_data > STALE_FRAME_TIME
            and data.startswith(FRAME_STARTS)
        ):
            _LOGGER.debug("Dropping incomplete frame %s", bytes(self._buffer).hex(" "))
            self._buffer.clear()
        self._last_data = now
        self._buffer += data
        while self._buffer:
            if not self._resync():
                return
            if len(self._buffer) < FRAME_OVERHEAD:
                return
            total_len = self._buffer[3] + FRAME_OVERHEAD
            if len(self._buffer) < total_len:
                return
            raw = bytes(self._buffer[:total_len])
            del self._buffer[:total_len]
            self._emit(raw)

    def _resync(self) -> bool:
        """Drop bytes before the next frame start; return False if none found."""
        if self._buffer.startswith(FRAME_STARTS):
            return True
        positions = [
            pos for start in FRAME_STARTS if (pos := self._buffer.find(start)) != -1
        ]
        if positions:
            first = min(positions)
            _LOGGER.debug("Discarding %d bytes of unframed data", first)
            del self._buffer[:first]
            return True
        # Keep a trailing '{', it may be the start of the next frame
        keep = 1 if self._buffer[-1] == FRAME_START else 0
        if len(self._buffer) > keep:
            _LOGGER.debug(
                "Discarding %d bytes without frame start", len(self._buffer) - keep
            )
            del self._buffer[: len(self._buffer) - keep]
        return False

    def _emit(self, raw: bytes) -> None:
        service = raw[1:3].decode(ENCODING)
        payload = raw[5:-1]
        frame = Frame(service, payload, raw[4] == checksum(payload))
        # Error frames are passed on even with a bad checksum so that an error
        # is never lost; everything else must be valid.
        if not frame.checksum_ok and not frame.is_error:
            _LOGGER.debug("Checksum mismatch, dropping frame %s", raw.hex(" "))
            return
        self._on_frame(frame)


def to_signed(value: int) -> int:
    """Convert an unsigned 16 bit value to a signed one."""
    return value - 0x10000 if value >= 0x8000 else value


@dataclass(frozen=True, slots=True)
class MonitorEntry:
    """An entry of the heater's monitor list.

    Entries with node 0 are headings of the heater's menu, not values.
    """

    node: int
    index: int
    value_type: int
    divider: int
    name: str
    header: tuple[int, ...]
    # Lowest monitor list level the entry was found in
    level: int = 0
    # Context from the list order, used to tell values with the same name
    # apart: the heading above the entry, the nearest heading or group start
    # (e.g. a motor) above it, and the nearest entry above with another name
    section: str = ""
    group: str = ""
    previous: str = ""

    @property
    def is_heading(self) -> bool:
        """Return True for a heading of the heater's menu."""
        return self.node == 0

    @property
    def key(self) -> tuple[int, int]:
        """Return the (node, index) pair that identifies the value."""
        return (self.node, self.index)

    def scale(self, raw: int) -> float | int:
        """Convert a raw 16 bit value to the value shown to the user."""
        value = to_signed(raw)
        if self.divider <= 1:
            return value
        return value / self.divider

    def as_dict(self) -> dict[str, object]:
        """Serialize for storage."""
        return {
            "header": list(self.header),
            "name": self.name,
            "level": self.level,
            "section": self.section,
            "group": self.group,
            "previous": self.previous,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MonitorEntry:
        """Restore from storage."""
        header = tuple(int(b) for b in data["header"])  # type: ignore[union-attr]
        entry = cls.from_header(header, str(data["name"]), int(data.get("level", 0)))  # type: ignore[call-overload]
        return replace(
            entry,
            section=str(data.get("section", "")),
            group=str(data.get("group", "")),
            previous=str(data.get("previous", "")),
        )

    @classmethod
    def from_header(
        cls, header: tuple[int, ...], name: str, level: int = 0
    ) -> MonitorEntry:
        """Create an entry from the 9 header bytes and its name."""
        return cls(
            node=header[0],
            index=header[1] * 256 + header[2],
            value_type=header[4],
            divider=header[6] * 256 + header[7],
            name=name,
            header=header,
            level=level,
        )


def parse_monitor_entry(payload: bytes, level: int = 0) -> MonitorEntry | None:
    """Parse an Mb payload; an empty payload marks the end of the list."""
    if len(payload) < MONITOR_ENTRY_HEADER_LEN:
        return None
    header = tuple(payload[:MONITOR_ENTRY_HEADER_LEN])
    name = payload[MONITOR_ENTRY_HEADER_LEN:].decode(ENCODING).strip()
    return MonitorEntry.from_header(header, name, level)


def build_monitor_list(
    entries: list[MonitorEntry], is_group_start: Callable[[MonitorEntry], bool]
) -> list[MonitorEntry]:
    """Turn the entries as read into the list of values.

    Headings are dropped, each entry gets its context from the entries above
    it, and a value found several times (also under other names or in other
    levels) is kept once, as first found.
    """
    values: dict[tuple[int, int], MonitorEntry] = {}
    level = None
    section = group = previous = ""
    for entry in entries:
        if entry.level != level:
            level = entry.level
            section = group = previous = ""
        if entry.is_heading:
            section = group = entry.name
            continue
        values.setdefault(
            entry.key,
            replace(entry, section=section, group=group, previous=previous),
        )
        if is_group_start(entry):
            group = entry.name
        if entry.name != previous:
            previous = entry.name
    return list(values.values())


def merge_monitor_lists(
    stored: list[MonitorEntry], read: list[MonitorEntry]
) -> list[MonitorEntry]:
    """Update a stored monitor list with a new read and fill its gaps.

    Reads can miss entries when a transmission gets lost, so nothing is
    removed: entries read again are updated (keeping the lowest level and
    any context the new read lacks), entries read for the first time are
    added at the end.
    """
    by_key = {entry.key: entry for entry in read}
    merged = []
    for entry in stored:
        if (new := by_key.pop(entry.key, None)) is None:
            merged.append(entry)
            continue
        merged.append(
            replace(
                new,
                level=min(entry.level, new.level),
                section=new.section or entry.section,
                group=new.group or entry.group,
                previous=new.previous or entry.previous,
            )
        )
    return merged + list(by_key.values())


def parse_values(payload: bytes) -> dict[tuple[int, int], int] | None:
    """Parse an MD payload into raw values keyed by (node, index)."""
    if len(payload) % VALUE_RECORD_LEN:
        return None
    values: dict[tuple[int, int], int] = {}
    for pos in range(0, len(payload), VALUE_RECORD_LEN):
        node, idx_hb, idx_lb, val_hb, val_lb = payload[pos : pos + VALUE_RECORD_LEN]
        values[(node, idx_hb * 256 + idx_lb)] = val_hb * 256 + val_lb
    return values


def monitor_list_request(position: int, level: int) -> bytes:
    """Build the request for one monitor list entry."""
    return build_frame(SERVICE_MONITOR_LIST, bytes([position, level]))


def subscribe_request(refresh_s: int, keys: list[tuple[int, int]]) -> bytes:
    """Build the request to send the given values every refresh_s seconds."""
    payload = bytearray([min(refresh_s, MAX_REFRESH_SECONDS)])
    for node, index in keys:
        payload += bytes([node, index // 256, index % 256])
    return build_frame(SERVICE_SUBSCRIBE, bytes(payload))
