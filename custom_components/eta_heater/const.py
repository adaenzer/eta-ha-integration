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
DEFAULT_SCAN_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 30
AVAILABLE_LEVELS: Final = [str(level) for level in range(10)]

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
    options: tuple[str, ...] | None = None
    translation_key: str | None = None


def _binary(device_class: BinarySensorDeviceClass | None = None) -> ValueType:
    return ValueType(Platform.BINARY_SENSOR, device_class)


def _enum(translation_key: str, *options: str) -> ValueType:
    return ValueType(
        Platform.SENSOR,
        SensorDeviceClass.ENUM,
        options=options,
        translation_key=translation_key,
    )


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
    16: _enum(
        "boiler_state",
        "off",
        "heating_up",
        "igniting",
        "heating",
        "ember_burnout",
        "ready",
        "deashing",
        "locked",
        "external_heat",
        "calibrating",
        "fault",
    ),
    17: _binary(),
    23: _enum("drive_state", "off", "on", "fault", "emptying", "self_test", "reverse"),
    30: _binary(),
    32: ValueType(Platform.SENSOR),
    37: _measurement(PERCENTAGE),
    45: _enum(
        "hot_water_state",
        "charging",
        "charged",
        "requesting",
        "off_by_timer",
        "solar_priority",
        "fault",
    ),
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
