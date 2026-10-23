"""Transports that carry ETA frames between Home Assistant and the heater."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from typing import Any, Protocol

from .protocol import FRAME_END

_LOGGER = logging.getLogger(__name__)

Receiver = Callable[[bytes], None]

DEFAULT_BAUDRATE = 19200
# The PTVO firmware ends a UART packet at '}' and strips it before reporting
PTVO_PACKET_END = FRAME_END
# Zigbee payloads are small, so fewer values fit into one subscription
PTVO_MAX_VALUES_PER_REQUEST = 6
SERIAL_MAX_VALUES_PER_REQUEST = 20


class TransportError(Exception):
    """The transport could not be opened or used."""


class Transport(ABC):
    """Bidirectional byte stream to the heater."""

    max_values_per_request: int

    def __init__(self) -> None:
        """Initialize the transport."""
        self._receiver: Receiver | None = None

    def set_receiver(self, receiver: Receiver) -> None:
        """Set the callback that gets all received bytes."""
        self._receiver = receiver

    def _received(self, data: bytes) -> None:
        if self._receiver is not None and data:
            self._receiver(data)

    @abstractmethod
    async def async_connect(self) -> None:
        """Open the connection."""

    @abstractmethod
    async def async_disconnect(self) -> None:
        """Close the connection."""

    @abstractmethod
    async def async_send(self, frame: bytes) -> None:
        """Send one complete frame."""


class SerialTransport(Transport):
    """Direct RS232 connection, e.g. through a USB serial adapter."""

    max_values_per_request = SERIAL_MAX_VALUES_PER_REQUEST

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """Initialize the transport."""
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self._serial: asyncio.Transport | None = None

    async def async_connect(self) -> None:
        """Open the serial port (8N1, no handshake)."""
        import serialx

        transport = self

        class _Protocol(asyncio.Protocol):
            def data_received(self, data: bytes) -> None:
                transport._received(data)

            def connection_lost(self, exc: Exception | None) -> None:
                _LOGGER.warning("Serial connection to %s lost: %s", transport.port, exc)
                transport._serial = None

        try:
            self._serial, _ = await serialx.create_serial_connection(
                asyncio.get_running_loop(), _Protocol, self.port, baudrate=self.baudrate
            )
        except (OSError, ValueError, serialx.SerialException) as err:
            raise TransportError(f"Cannot open {self.port}: {err}") from err

    async def async_disconnect(self) -> None:
        """Close the serial port."""
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    async def async_send(self, frame: bytes) -> None:
        """Write a frame to the serial port."""
        if self._serial is None:
            raise TransportError(f"Serial port {self.port} is not open")
        self._serial.write(frame)


class MqttBackend(Protocol):
    """Minimal MQTT client interface used by the PTVO transport."""

    async def async_start(self) -> None:
        """Connect to the broker."""

    async def async_stop(self) -> None:
        """Disconnect from the broker."""

    async def async_subscribe(
        self, topic: str, callback: Callable[[bytes], None]
    ) -> Callable[[], None]:
        """Subscribe to a topic; returns an unsubscribe function."""

    async def async_publish(
        self, topic: str, payload: str, retain: bool = False
    ) -> None:
        """Publish a message."""


def decode_ptvo_action(action: Any) -> bytes | None:
    """Turn the 'action' attribute zigbee2mqtt reports for PTVO UART data into bytes.

    zigbee2mqtt serializes the received byte array as an object with the
    positions as keys ({"0": 123, "1": 77, ...}); lists and Node.js Buffer
    objects are accepted as well.
    """
    if isinstance(action, dict):
        if action.get("type") == "Buffer" and isinstance(action.get("data"), list):
            values = action["data"]
        else:
            values = [action.get(str(idx)) for idx in range(len(action))]
    elif isinstance(action, list):
        values = action
    else:
        return None
    return bytes(v for v in values if isinstance(v, int) and 0 <= v <= 255)


class PtvoMqttTransport(Transport):
    """Connection through a PTVO Zigbee UART firmware and zigbee2mqtt."""

    max_values_per_request = PTVO_MAX_VALUES_PER_REQUEST

    def __init__(self, backend: MqttBackend, topic: str) -> None:
        """Initialize the transport."""
        super().__init__()
        self.backend = backend
        self.topic = topic
        self._unsubscribe: Callable[[], None] | None = None

    async def async_connect(self) -> None:
        """Connect and listen to the device topic."""
        await self.backend.async_start()
        self._unsubscribe = await self.backend.async_subscribe(
            self.topic, self._handle_message
        )

    async def async_disconnect(self) -> None:
        """Stop listening and disconnect."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.backend.async_stop()

    async def async_send(self, frame: bytes) -> None:
        """Send a frame; PTVO expects the length as first byte."""
        payload = json.dumps({"action": [len(frame), *frame]})
        await self.backend.async_publish(f"{self.topic}/set", payload)

    def _handle_message(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if not isinstance(message, dict) or "action" not in message:
            return
        data = decode_ptvo_action(message["action"])
        if not data:
            return
        # Each report is one UART packet whose terminating '}' was stripped.
        # Packets of 4 bytes or less cannot be a frame and are ignored, as in
        # the original AppDaemon implementation.
        if len(data) > 4:
            self._received(data + bytes([PTVO_PACKET_END]))


class PahoMqttBackend:
    """MQTT backend for a broker other than the one Home Assistant uses."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        run_in_executor: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None,
    ) -> None:
        """Initialize the backend."""
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._run_in_executor = run_in_executor
        self._client: Any = None
        self._subscriptions: dict[str, Callable[[bytes], None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _async_run(self, func: Callable[[], Any]) -> Any:
        if self._run_in_executor is not None:
            return await self._run_in_executor(func)
        return await asyncio.get_running_loop().run_in_executor(None, func)

    async def async_start(self) -> None:
        """Connect to the broker; raises TransportError on failure."""
        import paho.mqtt.client as mqtt

        self._loop = asyncio.get_running_loop()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username:
            client.username_pw_set(self.username, self.password or None)
        connected = asyncio.Event()
        result: dict[str, Any] = {}
        loop = self._loop

        def on_connect(
            _client: Any, _userdata: Any, _flags: Any, reason: Any, _props: Any
        ) -> None:
            result["reason"] = reason
            loop.call_soon_threadsafe(connected.set)
            if not reason.is_failure:
                # Subscribe again after a reconnect
                for topic in self._subscriptions:
                    client.subscribe(topic)

        def on_message(_client: Any, _userdata: Any, message: Any) -> None:
            if callback := self._subscriptions.get(message.topic):
                loop.call_soon_threadsafe(callback, bytes(message.payload))

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            await self._async_run(lambda: client.connect(self.host, self.port))
        except (OSError, ValueError) as err:
            raise TransportError(
                f"Cannot connect to {self.host}:{self.port}: {err}"
            ) from err
        client.loop_start()
        try:
            async with asyncio.timeout(10):
                await connected.wait()
        except TimeoutError as err:
            client.loop_stop()
            raise TransportError("MQTT broker did not answer") from err
        if result["reason"].is_failure:
            client.loop_stop()
            raise TransportError(f"MQTT broker refused connection: {result['reason']}")
        self._client = client

    async def async_stop(self) -> None:
        """Disconnect from the broker."""
        if self._client is not None:
            client, self._client = self._client, None
            client.disconnect()
            await self._async_run(client.loop_stop)

    async def async_subscribe(
        self, topic: str, callback: Callable[[bytes], None]
    ) -> Callable[[], None]:
        """Subscribe to a topic."""
        self._subscriptions[topic] = callback
        if self._client is not None:
            self._client.subscribe(topic)

        def unsubscribe() -> None:
            self._subscriptions.pop(topic, None)
            if self._client is not None:
                self._client.unsubscribe(topic)

        return unsubscribe

    async def async_publish(
        self, topic: str, payload: str, retain: bool = False
    ) -> None:
        """Publish a message."""
        if self._client is None:
            raise TransportError("Not connected to the MQTT broker")
        self._client.publish(topic, payload, retain=retain)
