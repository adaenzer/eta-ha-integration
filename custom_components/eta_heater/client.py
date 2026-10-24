"""Talk to the ETA heater over any transport.

The controller is easily overloaded, so communication follows strict rules:

- Only one operation (probe, monitor list read, value read) runs at a time.
- A request is only sent when the previous one was answered or given up
  after RESPONSE_TIMEOUT.
- There is a pause of at least FRAME_GAP between any two frames sent.
- Value subscriptions are always stopped again, also when something fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from itertools import groupby
import logging
from operator import itemgetter
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
    sms_command_request,
    subscribe_request,
)
from .transport import Transport

_LOGGER = logging.getLogger(__name__)

# Minimum time between two frames sent to the heater
FRAME_GAP = 0.5
# How long to wait for an answer before a request counts as lost. Over Zigbee
# the answers sometimes stall for 10-25 s and then arrive in a batch.
RESPONSE_TIMEOUT = 30.0
# After a repeated request was answered, wait this long so that a late answer
# to the lost request is not taken as the answer to the next one
LATE_ANSWER_TIME = 2.0
# Lost answers in a row after which reading a monitor list level is given up
MONITOR_LIST_MAX_LOST = 3
# After "end of list", wait this long for late entries before ending a level
LEVEL_SETTLE_TIME = 1.5
# Requests per level at most (the heater's lists have up to about 200 entries)
MAX_LEVEL_REQUESTS = 400
# Over Zigbee the first answer to a subscription can get lost; the heater
# sends the values again every SUBSCRIBE_REFRESH_S, so waiting a bit longer
# than twice that gives a lost answer two more chances
VALUES_TIMEOUT = 40.0
# Groups without any answer in a row after which a value read stops
MAX_UNANSWERED_GROUPS = 2
# Once the first values of a group arrived, wait this long for the rest
MORE_VALUES_TIME = 1.0
DISCONNECT_TIMEOUT = 30.0
MAX_POSITION = 255  # the list position is a single byte
# Refresh interval requested from the heater while reading values: how soon
# the values are sent again when the first answer got lost
SUBSCRIBE_REFRESH_S = 15

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
        self._list_queue: asyncio.Queue[Frame] | None = None
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
            _LOGGER.warning(
                "Heater reported error %s: %s", frame.service, frame.message
            )
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
        if frame.service == SERVICE_MONITOR_LIST_ENTRY and self._list_queue is not None:
            self._list_queue.put_nowait(frame)
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

    async def async_send_command(self, code: int) -> None:
        """Send a remote control command (service IH).

        The controller executes it like an SMS command of ETA's GSM
        connector. It does not answer in a documented way, so the effect
        shows in the values (e.g. the boiler state) only.
        """
        async with self._lock:
            _LOGGER.info("Sending command 0x%02x to the heater", code)
            await self._async_send(sms_command_request(code))

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
        may lack entries whose transmission got lost; reading again and
        merging the results fills such gaps (see protocol.merge_monitor_lists).
        """
        entries: list[MonitorEntry] = []
        async with self._lock:
            self._list_queue = asyncio.Queue()
            try:
                await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
                await self._async_send(build_frame(SERVICE_STOP))
                for level in sorted(levels):
                    entries += await self._async_read_level(level)
                await self._async_send(build_frame(SERVICE_STOP))
            finally:
                self._list_queue = None
        _LOGGER.debug("Read %d monitor list entries", len(entries))
        return entries

    async def _async_read_level(self, level: int) -> list[MonitorEntry]:
        """Read one level of the monitor list.

        The controller does not reliably answer the requested position: it
        keeps a cursor and answers each request with the next entry, and over
        Zigbee answers can be late, come in batches or get lost. So answers
        are not matched to requests. Position 0 starts the level, each
        further request asks for the next entry, and every entry arriving is
        kept in arrival order. A request is only sent when no request is
        waiting for its answer, or after RESPONSE_TIMEOUT.
        """
        assert self._list_queue is not None
        queue = self._list_queue
        entries: list[MonitorEntry] = []
        position = 0
        restarted = False
        lost = 0
        await self._async_send(monitor_list_request(position, level))
        for _ in range(MAX_LEVEL_REQUESTS):
            try:
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    frames = [await queue.get()]
            except TimeoutError:
                lost += 1
                if lost >= MONITOR_LIST_MAX_LOST:
                    raise EtaTimeoutError(
                        f"No monitor list answer from heater (level {level})"
                    ) from None
                _LOGGER.debug("No answer in level %d, asking for the next entry", level)
            else:
                lost = 0
                # A batch of late answers is taken in one go
                while not queue.empty():
                    frames.append(queue.get_nowait())
                ended = self._add_entries(frames, level, entries)
                if ended and not await self._async_more_entries(level, entries):
                    if entries or restarted:
                        break
                    # After another level the first answer can be a stale
                    # "end of list"; start the level once more
                    restarted = True
                    await self._async_send(monitor_list_request(0, level))
                    continue
            position = min(position + 1, MAX_POSITION)
            await self._async_send(monitor_list_request(position, level))
        else:
            _LOGGER.warning("Level %d did not end after %d requests", level, position)
        return entries

    async def _async_more_entries(
        self, level: int, entries: list[MonitorEntry]
    ) -> bool:
        """Wait for late entries after "end of list"; True if any arrived."""
        assert self._list_queue is not None
        frames: list[Frame] = []
        try:
            async with asyncio.timeout(LEVEL_SETTLE_TIME):
                while True:
                    frames.append(await self._list_queue.get())
        except TimeoutError:
            pass
        count = len(entries)
        ended = self._add_entries(frames, level, entries)
        return len(entries) > count and not ended

    @staticmethod
    def _add_entries(
        frames: list[Frame], level: int, entries: list[MonitorEntry]
    ) -> bool:
        """Add received entries; return True if the last frame ended the list."""
        ended = False
        for frame in frames:
            if (entry := parse_monitor_entry(frame.payload, level)) is None:
                ended = True
                continue
            ended = False
            # The same entry twice in a row is one answer received twice
            if (
                not entries
                or entries[-1].header != entry.header
                or entries[-1].name != entry.name
            ):
                entries.append(entry)
        return ended

    async def async_read_values(
        self, keys: list[tuple[int, int]]
    ) -> dict[tuple[int, int], int]:
        """Read raw values, in groups as large as the transport allows.

        A group without answer is given up after VALUES_TIMEOUT and the next
        one is requested; after MAX_UNANSWERED_GROUPS in a row the remaining
        groups are not requested. Returns the values read.
        """
        result: dict[tuple[int, int], int] = {}
        unanswered = 0
        async with self._lock:
            self._value_queue = asyncio.Queue()
            try:
                for number, group in enumerate(self._value_groups(keys), 1):
                    self._requested_keys.update(group)
                    try:
                        await self._async_send(
                            subscribe_request(SUBSCRIBE_REFRESH_S, group)
                        )
                        answered = await self._async_collect(set(group), result)
                    finally:
                        await self._async_send(build_frame(SERVICE_UNSUBSCRIBE))
                    if answered:
                        unanswered = 0
                        continue
                    unanswered += 1
                    _LOGGER.debug("No values for group %d", number)
                    if unanswered >= MAX_UNANSWERED_GROUPS:
                        _LOGGER.debug("Heater does not answer, not requesting more")
                        break
            finally:
                self._value_queue = None
        return result

    def _value_groups(self, keys: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
        """Split the values into groups, each from one node, sorted by index.

        Groups mixing the boiler board with another board (e.g. node 24 and
        40) were answered less reliably over Zigbee.
        """
        size = self.transport.max_values_per_request
        groups: list[list[tuple[int, int]]] = []
        for _, node_keys in groupby(sorted(set(keys)), key=itemgetter(0)):
            node_list = list(node_keys)
            groups += [node_list[i : i + size] for i in range(0, len(node_list), size)]
        return groups

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
