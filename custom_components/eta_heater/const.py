"""Constants for the ETA heater integration."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    Platform,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfMass,
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
    """How a monitor list value type is shown in Home Assistant.

    The kind is the entity's translation key: it selects the icon and the
    label added to the name when two values of different kinds share a name.
    """

    platform: Platform
    kind: str
    device_class: SensorDeviceClass | BinarySensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None


def _binary(
    kind: str, device_class: BinarySensorDeviceClass | None = None
) -> ValueType:
    return ValueType(Platform.BINARY_SENSOR, kind, device_class)


def _status(kind: str) -> ValueType:
    # The meaning of the numbers is unknown, so the raw value is shown
    return ValueType(Platform.SENSOR, kind)


def _measurement(
    kind: str, unit: str | None, device_class: SensorDeviceClass | None = None
) -> ValueType:
    return ValueType(
        Platform.SENSOR, kind, device_class, unit, SensorStateClass.MEASUREMENT
    )


def _hours() -> ValueType:
    return ValueType(
        Platform.SENSOR,
        "hours",
        SensorDeviceClass.DURATION,
        UnitOfTime.HOURS,
        SensorStateClass.TOTAL_INCREASING,
    )


# Motors start a group of related values in the monitor list (current,
# warming, ...), which helps to tell values with the same name apart
MOTOR_VALUE_TYPE: Final = 5

# Keyed by byte 4 of a monitor list entry. Found by testing, the protocol
# documentation does not describe them.
VALUE_TYPES: Final[dict[int, ValueType]] = {
    0: _binary("on_off"),
    2: _binary("opening", BinarySensorDeviceClass.OPENING),
    5: _binary("motor", BinarySensorDeviceClass.MOVING),
    8: _binary("on_off"),
    13: _binary("on_off"),
    15: _binary("opening", BinarySensorDeviceClass.OPENING),
    16: _status("boiler_state"),
    17: _binary("on_off"),
    23: _status("drive_state"),
    30: _binary("on_off"),
    32: ValueType(Platform.SENSOR, "number"),
    37: _measurement("percentage", PERCENTAGE),
    45: _status("hot_water_state"),
    48: _hours(),
    65: _measurement(
        "current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT
    ),
    80: _binary("on_off"),
    85: _measurement("speed", REVOLUTIONS_PER_MINUTE),
    86: _measurement(
        "voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE
    ),
    91: _binary("on_off"),
    97: _binary("on_off"),
    104: _hours(),
    107: _measurement("percentage", PERCENTAGE),
    115: _hours(),
    176: _measurement(
        "temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE
    ),
}

# Unknown value types are shown as plain sensors with the raw value
UNKNOWN_VALUE_TYPE: Final = ValueType(Platform.SENSOR, "number")


def value_type_for(code: int) -> ValueType:
    """Return how a monitor list value type is shown."""
    return VALUE_TYPES.get(code, UNKNOWN_VALUE_TYPE)


@dataclass(frozen=True, slots=True)
class NameUnit:
    """A unit recognized in the name of a value whose type has no unit."""

    pattern: re.Pattern[str]
    unit: str
    device_class: SensorDeviceClass
    state_class: SensorStateClass


# Checked in order, the first match wins
NAME_UNITS: Final = (
    # e.g. "kg seit letzter Kübelleerung": counts up, reset when emptied
    NameUnit(
        re.compile(r"\bkg\b.*\bseit\b|\bseit\b.*\bkg\b", re.IGNORECASE),
        UnitOfMass.KILOGRAMS,
        SensorDeviceClass.WEIGHT,
        SensorStateClass.TOTAL_INCREASING,
    ),
    NameUnit(
        re.compile(r"\bkg\b|\[kg\]", re.IGNORECASE),
        UnitOfMass.KILOGRAMS,
        SensorDeviceClass.WEIGHT,
        SensorStateClass.MEASUREMENT,
    ),
    NameUnit(
        re.compile(r"\[Hz\]"),
        UnitOfFrequency.HERTZ,
        SensorDeviceClass.FREQUENCY,
        SensorStateClass.MEASUREMENT,
    ),
    NameUnit(
        re.compile(r"\[mV\]"),
        UnitOfElectricPotential.MILLIVOLT,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
    ),
)


def unit_from_name(name: str) -> NameUnit | None:
    """Return the unit a value's name mentions, if any."""
    return next((unit for unit in NAME_UNITS if unit.pattern.search(name)), None)
