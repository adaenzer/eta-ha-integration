"""Talk to the ETA heater over any transport.

The heater answers requests asynchronously and also sends unsolicited frames
(cyclic values, error messages). All requests are serialized by a lock and
wait for their response with a timeout instead of sleeping for fixed times.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from .protocol import (
    SERVICE_MONITOR_LIST_ENTRY,
    SERVICE_STOP,
    SERVICE_UNSUBSCRIBE,
    SERVICE_VALUES,
    Frame,
    FrameParser,
    MonitorEntry,
    build_frame,
    monitor_list_request,
    parse_monitor_entry,
    parse_values,
    subscribe_request,
)
from .transport import Transport

_LOGGER = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 5.0
REQUEST_RETRIES = 3
VALUES_TIMEOUT = 6.0
MAX_MONITOR_ENTRIES = 256  # the list position is a single byte
# Refresh interval requested from the heater while reading values. Each group
# is unsubscribed before it repeats, so this only needs to exceed the timeout.
SUBSCRIBE_REFRESH_S = 30

ErrorListener = Callable[[Frame], None]


class EtaError(Exception):
    """Base error of the ETA client."""


class EtaTimeoutError(EtaError):
    """The heater did not answer in time."""


class EtaClient:
    """Request/response handling on top of a transport."""

    def __init__(self, transport: Transport) -> None:
        """Initialize the client."""
        self.transport = transport
        self._parser = FrameParser(self._handle_frame)
        self._lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future[Frame]] = {}
        self._value_queue: asyncio.Queue[dict[tuple[int, int], int]] | None = None
        self._error_listeners: list[ErrorListener] = []
        transport.set_receiver(self._parser.feed)

    async def async_connect(self) -> None:
        """Open the transport."""
        await self.transport.async_connect()

    async def async_disconnect(self) -> None:
        """Stop any running subscription and close the transport."""
        try:
            await self.transport.async_send(build_frame(SERVICE_UNSUBSCRIBE))
        except Exception:
            _LOGGER.debug("Could not unsubscribe before disconnecting")
        await self.transport.async_disconnect()

    def add_error_listener(self, listener: ErrorListener) -> Callable[[], None]:
        """Register a callback for error frames; returns a remove function."""
        self._error_listeners.append(listener)
        return lambda: self._error_listeners.remove(listener)

    def _handle_frame(self, frame: Frame) -> None:
        if frame.is_error:
            _LOGGER.warning("Heater reported error %s: %s", frame.service, frame.text)
            for listener in list(self._error_listeners):
                listener(frame)
            return
        if frame.service == SERVICE_VALUES:
            if (
                self._value_queue is not None
                and (values := parse_values(frame.payload)) is not None
            ):
                self._value_queue.put_nowait(values)
            return
        if (waiter := self._waiters.pop(frame.service, None)) and not waiter.done():
            waiter.set_result(frame)
            return
        _LOGGER.debug("Unhandled frame %s: %s", frame.service, frame.payload.hex(" "))

    async def _async_request(self, request: bytes, response_service: str) -> Frame:
        """Send a request and wait for the response, retrying on timeout."""
        for attempt in range(1, REQUEST_RETRIES + 1):
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[response_service] = waiter
            await self.transport.async_send(request)
            try:
                return await asyncio.wait_for(waiter, RESPONSE_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug(
                    "No %s response (attempt %d/%d)",
                    response_service,
                    attempt,
                    REQUEST_RETRIES,
                )
            finally:
                self._waiters.pop(response_service, None)
        raise EtaTimeoutError(f"No {response_service} response from heater")

    async def async_probe(self) -> None:
        """Check that the heater answers; raises EtaError if it does not."""
        async with self._lock:
            await self.transport.async_send(build_frame(SERVICE_STOP))
            await self._async_request(
                monitor_list_request(0, 0), SERVICE_MONITOR_LIST_ENTRY
            )

    async def async_read_monitor_list(self, levels: list[int]) -> list[MonitorEntry]:
        """Read the monitor list for the given levels, without duplicates.

        Levels are read in ascending order, so each entry keeps the lowest
        level it was found in.
        """
        entries: dict[tuple[int, int], MonitorEntry] = {}
        async with self._lock:
            await self.transport.async_send(build_frame(SERVICE_STOP))
            for level in sorted(levels):
                for position in range(MAX_MONITOR_ENTRIES):
                    frame = await self._async_request(
                        monitor_list_request(position, level),
                        SERVICE_MONITOR_LIST_ENTRY,
                    )
                    entry = parse_monitor_entry(frame.payload, level)
                    if entry is None:
                        break
                    # Node 0 entries are headings, not values
                    if entry.node != 0:
                        entries.setdefault(entry.key, entry)
            await self.transport.async_send(build_frame(SERVICE_STOP))
        _LOGGER.debug("Read %d monitor list entries", len(entries))
        return list(entries.values())

    async def async_read_values(
        self, keys: list[tuple[int, int]]
    ) -> dict[tuple[int, int], int]:
        """Read raw values, in groups as large as the transport allows."""
        result: dict[tuple[int, int], int] = {}
        group_size = self.transport.max_values_per_request
        async with self._lock:
            self._value_queue = asyncio.Queue()
            try:
                for start in range(0, len(keys), group_size):
                    group = keys[start : start + group_size]
                    await self.transport.async_send(
                        subscribe_request(SUBSCRIBE_REFRESH_S, group)
                    )
                    await self._async_collect(set(group), result)
                    await self.transport.async_send(build_frame(SERVICE_UNSUBSCRIBE))
            finally:
                self._value_queue = None
        return result

    async def _async_collect(
        self, wanted: set[tuple[int, int]], result: dict[tuple[int, int], int]
    ) -> None:
        assert self._value_queue is not None
        missing = set(wanted)
        try:
            async with asyncio.timeout(VALUES_TIMEOUT):
                while missing:
                    values = await self._value_queue.get()
                    result.update(values)
                    missing -= values.keys()
        except TimeoutError:
            _LOGGER.debug("No value received for %s", sorted(missing))
