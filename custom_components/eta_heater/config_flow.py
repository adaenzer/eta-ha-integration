"""Config flow for the ETA heater integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_DEVICE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .client import EtaClient, EtaError
from .connection import MqttNotAvailableError, create_transport
from .const import (
    AVAILABLE_LEVELS,
    BROKER_EXTERNAL,
    BROKER_HOME_ASSISTANT,
    CONF_BAUDRATE,
    CONF_BROKER,
    CONF_CONNECTION,
    CONF_LEVELS,
    CONF_PUBLISH_ERRORS,
    CONF_TOPIC,
    CONNECTION_PTVO_MQTT,
    CONNECTION_SERIAL,
    DEFAULT_LEVELS,
    DEFAULT_MQTT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOPIC,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import EtaConfigEntry
from .transport import DEFAULT_BAUDRATE, TransportError

_LOGGER = logging.getLogger(__name__)

# Fields that identify a connection; two entries must not share them
IDENTIFYING_KEYS = {
    CONNECTION_PTVO_MQTT: (CONF_BROKER, CONF_HOST, CONF_TOPIC),
    CONNECTION_SERIAL: (CONF_DEVICE,),
}


def _ptvo_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_TOPIC, default=defaults.get(CONF_TOPIC, DEFAULT_TOPIC)
            ): TextSelector(),
            vol.Required(
                CONF_BROKER, default=defaults.get(CONF_BROKER, BROKER_HOME_ASSISTANT)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[BROKER_HOME_ASSISTANT, BROKER_EXTERNAL],
                    translation_key=CONF_BROKER,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _broker_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, "")
            ): TextSelector(),
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_MQTT_PORT)
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
                vol.Coerce(int),
            ),
            vol.Optional(
                CONF_USERNAME,
                description={"suggested_value": defaults.get(CONF_USERNAME)},
            ): TextSelector(),
            vol.Optional(
                CONF_PASSWORD,
                description={"suggested_value": defaults.get(CONF_PASSWORD)},
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


def _serial_schema(defaults: Mapping[str, Any], ports: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_DEVICE, default=defaults.get(CONF_DEVICE, vol.UNDEFINED)
            ): (
                SelectSelector(
                    SelectSelectorConfig(
                        options=ports,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            ),
            vol.Required(
                CONF_BAUDRATE, default=defaults.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1200, max=115200, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Coerce(int),
            ),
        }
    )


def _serial_by_id(devices: list[str]) -> list[str]:
    """Replace device paths by their stable /dev/serial/by-id links if possible."""
    by_id = Path("/dev/serial/by-id")
    links = (
        {str(link.resolve()): str(link) for link in by_id.iterdir()}
        if by_id.is_dir()
        else {}
    )
    return sorted(links.get(device, device) for device in devices)


async def _async_list_serial_ports(hass: HomeAssistant) -> list[str]:
    """Return the serial ports, stable /dev/serial/by-id paths preferred."""
    import serialx

    try:
        ports = await serialx.async_list_serial_ports()
    except OSError:
        return []
    return await hass.async_add_executor_job(
        _serial_by_id, [port.device for port in ports]
    )


class EtaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the ETA heater."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: EtaConfigEntry) -> EtaOptionsFlow:
        """Return the options flow."""
        return EtaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick how the heater is connected."""
        return self.async_show_menu(
            step_id="user", menu_options=[CONNECTION_PTVO_MQTT, CONNECTION_SERIAL]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the connection settings of an existing entry."""
        entry = self._get_reconfigure_entry()
        self._data = dict(entry.data)
        if entry.data[CONF_CONNECTION] == CONNECTION_SERIAL:
            return await self.async_step_serial()
        return await self.async_step_ptvo_mqtt()

    async def async_step_ptvo_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the zigbee2mqtt topic of the PTVO device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_TOPIC] = user_input[CONF_TOPIC].strip().rstrip("/")
            self._data.update(user_input, **{CONF_CONNECTION: CONNECTION_PTVO_MQTT})
            if user_input[CONF_BROKER] == BROKER_EXTERNAL:
                return await self.async_step_broker()
            for key in (CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD):
                self._data.pop(key, None)
            if not (errors := await self._async_validate()):
                return await self._async_finish()
        return self.async_show_form(
            step_id="ptvo_mqtt",
            data_schema=_ptvo_schema(self._data),
            errors=errors,
        )

    async def async_step_broker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure an MQTT broker other than Home Assistant's."""
        errors: dict[str, str] = {}
        if user_input is not None:
            for key in (CONF_USERNAME, CONF_PASSWORD):
                self._data.pop(key, None)
            self._data.update(user_input)
            if not (errors := await self._async_validate()):
                return await self._async_finish()
        return self.async_show_form(
            step_id="broker",
            data_schema=_broker_schema(self._data),
            errors=errors,
        )

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a direct serial connection."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input, **{CONF_CONNECTION: CONNECTION_SERIAL})
            if not (errors := await self._async_validate()):
                return await self._async_finish()
        return self.async_show_form(
            step_id="serial",
            data_schema=_serial_schema(
                self._data, await _async_list_serial_ports(self.hass)
            ),
            errors=errors,
        )

    async def _async_validate(self) -> dict[str, str]:
        """Check for duplicates and that the heater answers."""
        keys = IDENTIFYING_KEYS[self._data[CONF_CONNECTION]]
        match = {key: self._data[key] for key in keys if key in self._data}
        if self.source != "reconfigure" or any(
            self._get_reconfigure_entry().data.get(key) != value
            for key, value in match.items()
        ):
            self._async_abort_entries_match(match)

        client = EtaClient(create_transport(self.hass, self._data))
        try:
            await client.async_connect()
        except MqttNotAvailableError:
            return {"base": "mqtt_not_available"}
        except TransportError as err:
            _LOGGER.debug("Cannot connect: %s", err)
            return {"base": "cannot_connect"}
        try:
            await client.async_probe()
        except (EtaError, TransportError) as err:
            _LOGGER.debug("Heater did not answer: %s", err)
            return {"base": "no_response"}
        finally:
            await client.async_disconnect()
        return {}

    async def _async_finish(self) -> ConfigFlowResult:
        if self._data[CONF_CONNECTION] == CONNECTION_SERIAL:
            title = f"ETA heater ({self._data[CONF_DEVICE]})"
        else:
            title = f"ETA heater ({self._data[CONF_TOPIC]})"
        if self.source == "reconfigure":
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), title=title, data=self._data
            )
        return self.async_create_entry(title=title, data=self._data)


class EtaOptionsFlow(OptionsFlowWithReload):
    """Options of the ETA heater; the entry is reloaded after a change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            levels = sorted({int(level) for level in user_input[CONF_LEVELS]})
            if levels:
                return self.async_create_entry(data={**user_input, CONF_LEVELS: levels})
            errors[CONF_LEVELS] = "invalid_levels"

        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_LEVELS,
                default=[str(lvl) for lvl in options.get(CONF_LEVELS, DEFAULT_LEVELS)],
            ): SelectSelector(
                SelectSelectorConfig(
                    options=AVAILABLE_LEVELS,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=3600,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Coerce(int),
            ),
        }
        if self.config_entry.data[CONF_CONNECTION] == CONNECTION_PTVO_MQTT:
            schema[
                vol.Required(
                    CONF_PUBLISH_ERRORS, default=options.get(CONF_PUBLISH_ERRORS, True)
                )
            ] = BooleanSelector()
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )
