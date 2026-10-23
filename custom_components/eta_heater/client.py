"""Talk to the ETA heater over any transport.

The controller is easily overloaded, so communication follows strict rules:

- Only one operation (probe, monitor list read, value read) runs at a time.
- A request is only sent when the previous one was answered or given up.
  A request without answer is only repeated after RESPONSE_TIMEOUT.
- There is a pause of at least FRAME_GAP between any two frames sent.
- Value subscriptions are always stopped again, also when something fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from time import monotonic

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

# Minimum time between two frames sent to the heater
FRAME_GAP = 0.5
# How long to wait for an answer before a request counts as lost
RESPONSE_TIMEOUT = 10.0
# Attempts per monitor list entry; a lost request is repeated, never earlier
# than RESPONSE_TIMEOUT after it was sent
MONITOR_LIST_ATTEMPTS = 3
# After a repeated request was answered, wait this long so that a late answer
# to the lost request is not taken as the answer to the next one
LATE_ANSWER_TIME = 2.0
VALUES_TIMEOUT = 10.0
# Once the first values of a group arrived, wait this long for the rest
MORE_VALUES_TIME = 1.0
DISCONNECT_TIMEOUT = 30.0
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
        self._last_send = 0.0
        self._waiters: dict[str, asyncio.Future[Frame]] = {}
        self._value_queue: asyncio.Queue[dict[tuple[int, int], int]] | None = None
        self._error_listeners: list[ErrorListener] = []
        # Values this client ever subscribed to; value frames with none of
        # them were requested by another program talking to the heater
        self._requested_keys: set[tuple[int, int]] = set()
        self.foreign_value_frames = 0
        transport.set_receiver(self._parser.feed)

    async def async_connect(self) -> None:
        """Open the transport and stop subscriptions left by anyone else."""
        await self.transport.async_connect()
        async with self._lock:
            await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))

    async def async_disconnect(self) -> None:
        """Stop any running subscription and close the transport."""
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT), self._lock:
                await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
        except Exception:
            _LOGGER.debug("Could not unsubscribe before disconnecting")
        await self.transport.async_disconnect()

    async def _async_send(self, frame: bytes) -> None:
        """Send a frame, keeping the minimum gap to the previous one."""
        if (wait := self._last_send + FRAME_GAP - monotonic()) > 0:
            await asyncio.sleep(wait)
        try:
            await self.transport.async_send(frame)
        finally:
            self._last_send = monotonic()

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
            if not (values := parse_values(frame.payload)):
                return
            if not values.keys() & self._requested_keys:
                self.foreign_value_frames += 1
                _LOGGER.debug("Values requested by someone else: %s", sorted(values))
            if self._value_queue is not None:
                self._value_queue.put_nowait(values)
            return
        if (waiter := self._waiters.pop(frame.service, None)) and not waiter.done():
            waiter.set_result(frame)
            return
        _LOGGER.debug("Unhandled frame %s: %s", frame.service, frame.payload.hex(" "))

    async def _async_request(
        self, request: bytes, response_service: str, attempts: int = 1
    ) -> Frame:
        """Send a request and wait for its answer.

        The request is only repeated after the previous attempt timed out.
        """
        for attempt in range(1, attempts + 1):
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[response_service] = waiter
            try:
                await self._async_send(request)
                frame = await asyncio.wait_for(waiter, RESPONSE_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug(
                    "No %s answer (attempt %d/%d)", response_service, attempt, attempts
                )
                continue
            finally:
                self._waiters.pop(response_service, None)
            if attempt > 1:
                # A late answer to a lost attempt is dropped while no request
                # is waiting for it
                await asyncio.sleep(LATE_ANSWER_TIME)
            return frame
        raise EtaTimeoutError(f"No {response_service} answer from heater")

    async def async_probe(self) -> None:
        """Check that the heater answers; raises EtaError if it does not."""
        async with self._lock:
            await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
            await self._async_send(build_frame(SERVICE_STOP))
            await self._async_request(
                monitor_list_request(0, 0), SERVICE_MONITOR_LIST_ENTRY
            )

    async def async_read_monitor_list(self, levels: list[int]) -> list[MonitorEntry]:
        """Read the monitor list for the given levels, as the heater sends it.

        Levels are read in ascending order. The result includes headings and
        duplicates; see protocol.build_monitor_list.
        """
        entries: list[MonitorEntry] = []
        async with self._lock:
            await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
            await self._async_send(build_frame(SERVICE_STOP))
            for level in sorted(levels):
                for position in range(MAX_MONITOR_ENTRIES):
                    frame = await self._async_request(
                        monitor_list_request(position, level),
                        SERVICE_MONITOR_LIST_ENTRY,
                        MONITOR_LIST_ATTEMPTS,
                    )
                    entry = parse_monitor_entry(frame.payload, level)
                    if entry is None:
                        break
                    entries.append(entry)
            await self._async_send(build_frame(SERVICE_STOP))
        _LOGGER.debug("Read %d monitor list entries", len(entries))
        return entries

    async def async_read_values(
        self, keys: list[tuple[int, int]]
    ) -> dict[tuple[int, int], int]:
        """Read raw values, in groups as large as the transport allows.

        If the heater does not answer a group at all, the remaining groups
        are not requested and the values read so far are returned.
        """
        result: dict[tuple[int, int], int] = {}
        group_size = self.transport.max_values_per_request
        async with self._lock:
            self._value_queue = asyncio.Queue()
            try:
                for start in range(0, len(keys), group_size):
                    group = keys[start : start + group_size]
                    self._requested_keys.update(group)
                    try:
                        await self._async_send(
                            subscribe_request(SUBSCRIBE_REFRESH_S, group)
                        )
                        answered = await self._async_collect(set(group), result)
                    finally:
                        await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
                    if not answered:
                        _LOGGER.debug(
                            "No values for group %d, not requesting more",
                            start // group_size + 1,
                        )
                        break
            finally:
                self._value_queue = None
        return result

    async def _async_collect(
        self, wanted: set[tuple[int, int]], result: dict[tuple[int, int], int]
    ) -> bool:
        """Collect values until all arrived; return False if none arrived."""
        assert self._value_queue is not None
        missing = set(wanted)
        timeout = VALUES_TIMEOUT
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while missing:
                async with asyncio.timeout_at(deadline):
                    values = await self._value_queue.get()
                # Values requested by someone else (another program talking
                # to the heater, or an earlier subscription) are ignored
                if not (received := values.keys() & wanted):
                    _LOGGER.debug("Ignoring values not requested: %s", sorted(values))
                    continue
                result.update({key: values[key] for key in received})
                missing -= received
                # Values the heater does not know never arrive
                deadline = asyncio.get_running_loop().time() + MORE_VALUES_TIME
        except TimeoutError:
            _LOGGER.debug("No value received for %s", sorted(missing))
        return missing != wanted
