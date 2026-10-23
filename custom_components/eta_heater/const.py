"""Constants for the ETA heater integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    Platform,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)

DOMAIN: Final = "eta_heater"

CONF_CONNECTION: Final = "connection"
CONNECTION_PTVO_MQTT: Final = "ptvo_mqtt"
CONNECTION_SERIAL: Final = "serial"

CONF_TOPIC: Final = "topic"
CONF_BROKER: Final = "broker"
BROKER_HOME_ASSISTANT: Final = "home_assistant"
BROKER_EXTERNAL: Final = "external"
CONF_BAUDRATE: Final = "baudrate"

CONF_LEVELS: Final = "levels"
CONF_PUBLISH_ERRORS: Final = "publish_errors"

DEFAULT_TOPIC: Final = "zigbee2mqtt/heater"
DEFAULT_MQTT_PORT: Final = 1883
DEFAULT_LEVELS: Final = [0]
DEFAULT_SCAN_INTERVAL: Final = 300
MIN_SCAN_INTERVAL: Final = 30
AVAILABLE_LEVELS: Final = ["0", "1", "2", "3"]

ERROR_TOPIC_SUFFIX: Final = "/error"
EVENT_TYPE_ERROR: Final = "error"

STORAGE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class ValueType:
    """How a monitor list value type is shown in Home Assistant."""

    platform: Platform
    device_class: SensorDeviceClass | BinarySensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    translation_key: str | None = None


def _binary(device_class: BinarySensorDeviceClass | None = None) -> ValueType:
    return ValueType(Platform.BINARY_SENSOR, device_class)


def _status(translation_key: str) -> ValueType:
    # The meaning of the numbers is unknown, so the raw value is shown. The
    # translation key only selects the icon.
    return ValueType(Platform.SENSOR, translation_key=translation_key)


def _measurement(
    unit: str | None, device_class: SensorDeviceClass | None = None
) -> ValueType:
    return ValueType(Platform.SENSOR, device_class, unit, SensorStateClass.MEASUREMENT)


def _hours() -> ValueType:
    return ValueType(
        Platform.SENSOR,
        SensorDeviceClass.DURATION,
        UnitOfTime.HOURS,
        SensorStateClass.TOTAL_INCREASING,
    )


# Keyed by byte 4 of a monitor list entry. Found by testing, the protocol
# documentation does not describe them.
VALUE_TYPES: Final[dict[int, ValueType]] = {
    0: _binary(),
    2: _binary(BinarySensorDeviceClass.OPENING),
    5: _binary(BinarySensorDeviceClass.MOVING),
    8: _binary(),
    13: _binary(),
    15: _binary(BinarySensorDeviceClass.OPENING),
    16: _status("boiler_state"),
    17: _binary(),
    23: _status("drive_state"),
    30: _binary(),
    32: ValueType(Platform.SENSOR),
    37: _measurement(PERCENTAGE),
    45: _status("hot_water_state"),
    48: _hours(),
    65: _measurement(UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
    80: _binary(),
    85: _measurement(REVOLUTIONS_PER_MINUTE),
    86: _measurement(UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    91: _binary(),
    97: _binary(),
    104: _hours(),
    107: _measurement(PERCENTAGE),
    115: _hours(),
    176: _measurement(UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
}

# Unknown value types are shown as plain sensors with the raw value
UNKNOWN_VALUE_TYPE: Final = ValueType(Platform.SENSOR)


def value_type_for(code: int) -> ValueType:
    """Return how a monitor list value type is shown."""
    return VALUE_TYPES.get(code, UNKNOWN_VALUE_TYPE)
