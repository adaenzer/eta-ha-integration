"""Create the transport for a config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.const import (
    CONF_DEVICE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback

from .const import (
    BROKER_EXTERNAL,
    CONF_BAUDRATE,
    CONF_BROKER,
    CONF_CONNECTION,
    CONF_TOPIC,
    CONNECTION_SERIAL,
)
from .transport import (
    DEFAULT_BAUDRATE,
    PahoMqttBackend,
    PtvoMqttTransport,
    SerialTransport,
    Transport,
    TransportError,
)

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE_TIMEOUT = 10


class MqttNotAvailableError(TransportError):
    """The Home Assistant MQTT integration is not set up."""


class HomeAssistantMqttBackend:
    """Use the broker Home Assistant's MQTT integration is connected to."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backend."""
        self.hass = hass

    async def async_start(self) -> None:
        """Wait until the MQTT integration is available."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise MqttNotAvailableError("MQTT integration is not available")

    async def async_stop(self) -> None:
        """Nothing to do, the connection belongs to the MQTT integration."""

    async def async_subscribe(
        self, topic: str, receiver: Callable[[bytes], None]
    ) -> Callable[[], None]:
        """Subscribe to a topic."""

        @callback
        def _message_received(msg: mqtt.ReceiveMessage) -> None:
            payload = msg.payload
            receiver(payload if isinstance(payload, bytes) else payload.encode())

        unsubscribe = await mqtt.async_subscribe(
            self.hass, topic, _message_received, qos=0, encoding=None
        )
        # The MQTT integration subscribes in the background; an answer from
        # the heater that arrives before that would be lost
        subscribed = asyncio.Event()
        stop_waiting = mqtt.async_on_subscribe_done(self.hass, topic, 0, subscribed.set)
        try:
            async with asyncio.timeout(SUBSCRIBE_TIMEOUT):
                await subscribed.wait()
        except TimeoutError:
            _LOGGER.warning("Subscription to %s not confirmed, continuing", topic)
        finally:
            stop_waiting()
        return unsubscribe

    async def async_publish(
        self, topic: str, payload: str, retain: bool = False
    ) -> None:
        """Publish a message."""
        await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=retain)


def create_transport(hass: HomeAssistant, data: Mapping[str, Any]) -> Transport:
    """Create the transport described by config entry data."""
    if data[CONF_CONNECTION] == CONNECTION_SERIAL:
        return SerialTransport(
            data[CONF_DEVICE], data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
        )
    if data[CONF_BROKER] == BROKER_EXTERNAL:
        backend: Any = PahoMqttBackend(
            data[CONF_HOST],
            data[CONF_PORT],
            data.get(CONF_USERNAME),
            data.get(CONF_PASSWORD),
            hass.async_add_executor_job,
        )
    else:
        backend = HomeAssistantMqttBackend(hass)
    return PtvoMqttTransport(backend, data[CONF_TOPIC])
