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
    UnitOfPower,
    UnitOfPressure,
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
    # Status values: number -> state key, unknown numbers are shown as is
    states: dict[int, str] | None = None


def _binary(
    kind: str, device_class: BinarySensorDeviceClass | None = None
) -> ValueType:
    return ValueType(Platform.BINARY_SENSOR, kind, device_class)


def _status(kind: str, states: dict[int, str] | None = None) -> ValueType:
    return ValueType(Platform.SENSOR, kind, states=states)


# Found by comparing the values with the main screen of the controller. The
# boiler states follow the order of the texts the panel uses, 0, 2-6 were
# seen; the rest of that order is assumed. Buffer states do not follow such
# an order, so only the ones seen are listed.
BOILER_STATES: Final = {
    0: "off",
    1: "preheating",
    2: "igniting",
    3: "heating",
    4: "ember_burnout",
    5: "ready",
    6: "deashing",
    7: "blocked",
    8: "external_heat",
    9: "calibrating",
    10: "fault",
}
BUFFER_STATES: Final = {
    -5: "charging",
    1: "charged",
    2: "requesting",
}


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
    16: _status("boiler_state", BOILER_STATES),
    17: _binary("on_off"),
    23: _status("drive_state"),
    30: _binary("on_off"),
    32: ValueType(Platform.SENSOR, "number"),
    37: _measurement("percentage", PERCENTAGE),
    45: _status("hot_water_state", BUFFER_STATES),
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
    # Seconds, e.g. "Zustandstimer" (time in the current state); the
    # parameter list shows it: "Mischerlaufzt" 120 s, "HeizversZeit" 300 s
    115: ValueType(
        Platform.SENSOR,
        "seconds",
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        SensorStateClass.MEASUREMENT,
    ),
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
    """A unit mentioned in the name of a value whose type has no unit."""

    unit: str
    device_class: SensorDeviceClass | None


# Units the heater may write into names. Single letters (A, V, h, s) only
# count in brackets, e.g. "[A]", because they also occur as abbreviations.
UNITS_IN_BRACKETS: Final[dict[str, NameUnit]] = {
    "°C": NameUnit(UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "%": NameUnit(PERCENTAGE, None),
    "kg": NameUnit(UnitOfMass.KILOGRAMS, SensorDeviceClass.WEIGHT),
    "Hz": NameUnit(UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY),
    "mV": NameUnit(UnitOfElectricPotential.MILLIVOLT, SensorDeviceClass.VOLTAGE),
    "V": NameUnit(UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    "mA": NameUnit(UnitOfElectricCurrent.MILLIAMPERE, SensorDeviceClass.CURRENT),
    "A": NameUnit(UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
    "kW": NameUnit(UnitOfPower.KILO_WATT, SensorDeviceClass.POWER),
    "bar": NameUnit(UnitOfPressure.BAR, SensorDeviceClass.PRESSURE),
    "h": NameUnit(UnitOfTime.HOURS, SensorDeviceClass.DURATION),
    "min": NameUnit(UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "s": NameUnit(UnitOfTime.SECONDS, SensorDeviceClass.DURATION),
    "rpm": NameUnit(REVOLUTIONS_PER_MINUTE, None),
    "1/min": NameUnit(REVOLUTIONS_PER_MINUTE, None),
}
UNITS_AS_WORDS: Final = ("kg", "Hz", "mV", "mA", "kW", "bar", "°C", "rpm")

_BRACKETS = re.compile(r"\[\s*([^\]]+?)\s*\]")
_WORDS = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(unit) for unit in UNITS_AS_WORDS) + r")(?!\w)"
)


def strip_unit(name: str) -> str:
    """Remove a recognized unit in brackets; Home Assistant shows the unit."""
    stripped = _BRACKETS.sub(
        lambda match: " " if match.group(1) in UNITS_IN_BRACKETS else match.group(0),
        name,
    )
    return " ".join(stripped.split()) or name


def unit_from_name(name: str) -> NameUnit | None:
    """Return the unit a value's name mentions, if any.

    This only looks at the name's form, not at specific names, so it works
    for any heater and language: "Netzfreq. [Hz]", "kg seit letzter ...".
    """
    for match in _BRACKETS.finditer(name):
        if unit := UNITS_IN_BRACKETS.get(match.group(1)):
            return unit
    if match := _WORDS.search(name):
        return UNITS_IN_BRACKETS[match.group(1)]
    return None
