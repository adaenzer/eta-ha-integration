"""The ETA heater integration."""

from __future__ import annotations

from collections.abc import Callable
import json
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
import homeassistant.util.dt as dt_util

from .client import EtaClient
from .connection import MqttNotAvailableError, create_transport
from .const import (
    CONF_CONNECTION,
    CONF_PUBLISH_ERRORS,
    CONF_TOPIC,
    CONNECTION_PTVO_MQTT,
    DOMAIN,
    ERROR_TOPIC_SUFFIX,
)
from .coordinator import (
    EtaConfigEntry,
    EtaCoordinator,
    levels_issue_id,
    monitor_list_store,
)
from .protocol import Frame
from .transport import PtvoMqttTransport, TransportError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: EtaConfigEntry) -> bool:
    """Set up the ETA heater from a config entry."""
    client = EtaClient(create_transport(hass, entry.data))
    try:
        await client.async_connect()
    except MqttNotAvailableError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="mqtt_not_available"
        ) from err
    except TransportError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = EtaCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.async_disconnect()
        raise
    entry.runtime_data = coordinator

    if entry.data[CONF_CONNECTION] == CONNECTION_PTVO_MQTT and entry.options.get(
        CONF_PUBLISH_ERRORS, True
    ):
        entry.async_on_unload(_publish_errors_to_mqtt(hass, entry, client))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _publish_errors_to_mqtt(
    hass: HomeAssistant, entry: EtaConfigEntry, client: EtaClient
) -> Callable[[], None]:
    """Publish errors to <topic>/error like the former AppDaemon app did."""
    assert isinstance(client.transport, PtvoMqttTransport)
    backend = client.transport.backend
    topic = entry.data[CONF_TOPIC] + ERROR_TOPIC_SUFFIX

    @callback
    def _publish(frame: Frame) -> None:
        payload = json.dumps(
            {
                "time": dt_util.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:21],
                "ErrorType": frame.service,
                "ErrorText": frame.text,
            }
        )
        entry.async_create_background_task(
            hass, backend.async_publish(topic, payload), "eta_heater_publish_error"
        )

    return client.add_error_listener(_publish)


async def async_unload_entry(hass: HomeAssistant, entry: EtaConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.async_disconnect()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: EtaConfigEntry) -> None:
    """Remove the cached monitor list when the entry is deleted."""
    await monitor_list_store(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, levels_issue_id(entry.entry_id))
