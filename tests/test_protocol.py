"""Tests for frame building and parsing."""

from __future__ import annotations

from unittest.mock import patch

from custom_components.eta_heater.protocol import (
    Frame,
    FrameParser,
    MonitorEntry,
    build_frame,
    parse_monitor_entry,
    parse_values,
    subscribe_request,
    to_signed,
)


def _parse(*chunks: bytes) -> list[Frame]:
    frames: list[Frame] = []
    parser = FrameParser(frames.append)
    for chunk in chunks:
        parser.feed(chunk)
    return frames


def test_build_frame() -> None:
    """Frames carry length and checksum of the payload."""
    assert build_frame("ME") == b"{ME\x00\x00}"
    assert build_frame("MA", b"\x02\x01") == b"{MA\x02\x03\x02\x01}"


def test_subscribe_request() -> None:
    """Subscriptions list node and big endian index per value."""
    frame = subscribe_request(30, [(8, 1), (9, 300)])
    assert frame[5:-1] == bytes([30, 8, 0, 1, 9, 1, 44])


def test_parse_chunked_and_concatenated() -> None:
    """Frames are reassembled across chunks and split when concatenated."""
    md = build_frame("MD", bytes([8, 0, 1, 2, 140]))
    err = build_frame("IM", "Störung Saugzug".encode("latin-1"))
    frames = _parse(md[:3], md[3:] + err[:7], err[7:])
    assert [f.service for f in frames] == ["MD", "IM"]
    assert frames[1].text == "Störung Saugzug"
    assert frames[1].is_error


def test_resync_after_garbage() -> None:
    """Bytes before a frame start are discarded."""
    frames = _parse(b"\x00xx}" + build_frame("ME") + b"zz{", b"MD\x00\x00}")
    assert [f.service for f in frames] == ["ME", "MD"]


def test_bad_checksum() -> None:
    """Bad checksums drop normal frames but keep error frames."""
    bad_md = b"{MD\x05\x00" + bytes([8, 0, 1, 2, 140]) + b"}"
    for service in ("IM", "SE", "SS"):
        bad_err = b"{" + service.encode() + b"\x03\x00abc}"
        frames = _parse(bad_md + bad_err)
        assert [(f.service, f.checksum_ok) for f in frames] == [(service, False)]


def test_values_and_entries() -> None:
    """Values are signed and scaled by the entry's divider."""
    assert to_signed(0xFFF6) == -10
    assert to_signed(0x7FFF) == 0x7FFF
    assert parse_values(bytes([8, 0, 1, 2, 140, 9, 1, 44, 0, 1])) == {
        (8, 1): 652,
        (9, 300): 1,
    }
    assert parse_values(b"\x01\x02") is None

    entry = parse_monitor_entry(bytes([8, 0, 1, 0, 176, 0, 0, 10, 0]) + b" Kessel ")
    assert entry is not None
    assert (entry.key, entry.value_type, entry.name) == ((8, 1), 176, "Kessel")
    assert entry.scale(652) == 65.2
    assert entry.scale(0xFFF6) == -1.0
    assert MonitorEntry.from_dict(entry.as_dict()) == entry
    assert parse_monitor_entry(b"") is None


def test_divider_is_16_bit() -> None:
    """The divider spans header bytes 6 and 7."""
    entry = parse_monitor_entry(bytes([40, 0, 19, 0, 65, 2, 3, 232, 2]) + b"Strom")
    assert entry is not None
    assert entry.divider == 1000
    assert entry.scale(1500) == 1.5


def test_stale_fragment_dropped() -> None:
    """An incomplete frame does not swallow a frame arriving much later."""
    frames: list[Frame] = []
    parser = FrameParser(frames.append)
    md = build_frame("MD", bytes([8, 0, 1, 2, 140]))
    with patch("custom_components.eta_heater.protocol.monotonic", return_value=10.0):
        parser.feed(md[:6])
    with patch("custom_components.eta_heater.protocol.monotonic", return_value=11.0):
        parser.feed(md[6:])
    assert [f.service for f in frames] == ["MD"]

    with patch("custom_components.eta_heater.protocol.monotonic", return_value=20.0):
        parser.feed(md[:6])
    with patch("custom_components.eta_heater.protocol.monotonic", return_value=25.0):
        parser.feed(md)
    assert [f.service for f in frames] == ["MD", "MD"]
