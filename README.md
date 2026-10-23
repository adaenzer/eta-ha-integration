# ETA Heater (RS232) for Home Assistant

A Home Assistant integration for ETA heaters that have the older RS232 service
interface (the protocol with `{MA…}` / `{MD…}` frames, see
`Protokoll_ETA.pdf` in the [etainterface](https://github.com/adaenzer/etainterface)
repository). It replaces the AppDaemon app `eta2hass.py` from that repository.

Newer ETA heaters with ETAtouch offer a REST API; this integration is **not**
for those.

## What it provides

All entities belong to one device per heater:

- **Sensors and binary sensors** for every value of the heater's monitor list.
  The names come from the heater. Temperatures, currents, voltages, percentages
  and operating hours get proper units; boiler, drive and hot water states are
  shown as translated states.
- **Error event** (`event.<name>_error`) fires whenever the heater sends an
  error message (`IM`, `SE`, `SS`). Its attributes `error_type` and `message`
  hold the type and text.
- **Read monitor list again** button, for when the heater's configuration
  changed.

With the PTVO/MQTT connection, error messages are also published as JSON to
`<topic>/error`, exactly like the AppDaemon app did, so existing automations
keep working. You can turn this off in the options.

## Supported connections

| Connection | Hardware | Notes |
|---|---|---|
| PTVO Zigbee UART via zigbee2mqtt | Zigbee module with [PTVO firmware](https://ptvo.info/zigbee-configurable-firmware-features/) on the heater's RS232 port, UART 19200 baud, packet end `125` | Uses the MQTT integration of Home Assistant, or any other broker you configure. Six values per request. |
| Direct serial | USB serial adapter (or similar) connected to the Home Assistant host | 19200 baud, 8N1. Twenty values per request. |

The PTVO firmware configuration and the zigbee2mqtt external converter are in
the `hass_z2m` folder of the etainterface repository.

## Installation

### HACS

1. In HACS, add this repository as a custom repository of type *Integration*.
2. Install **ETA Heater (RS232)** and restart Home Assistant.

### Manual

Copy `custom_components/eta_heater` to the `custom_components` folder of your
Home Assistant configuration and restart Home Assistant.

## Configuration

Go to **Settings → Devices & services → Add integration → ETA Heater (RS232)**
and choose the connection.

### PTVO Zigbee UART via zigbee2mqtt

| Parameter | Description |
|---|---|
| zigbee2mqtt topic | Base topic of the PTVO device, e.g. `zigbee2mqtt/heater` (without `/set`). |
| MQTT broker | *Home Assistant MQTT integration* uses the broker and credentials of the MQTT integration. *External broker* asks for host, port, username and password of a different broker. |

### Direct serial

| Parameter | Description |
|---|---|
| Serial port | Prefer the `/dev/serial/by-id/…` path, it does not change between restarts. |
| Baud rate | 19200 for ETA heaters. |

Before the entry is created, the integration checks that the heater answers.

### Options

| Option | Default | Description |
|---|---|---|
| Monitor list levels | `0` | Levels of the monitor list to read. Changing them reads the list again. |
| Update interval | 120 s | How often all values are read. |
| Publish errors to MQTT | on | PTVO only: also publish errors to `<topic>/error`. |

Connection settings can be changed later with **Reconfigure**.

## How data is updated

On the first start, the integration reads the monitor list of the configured
levels from the heater. This can take a few minutes over Zigbee. The list is
cached and only read again when the levels change or the button is pressed.

Values are polled at the update interval: the integration subscribes to a
group of values, waits for the heater to send them, unsubscribes and continues
with the next group. Values the heater does not send become unavailable.

## Known limitations

- Only reading is supported, parameters cannot be changed.
- The value types are known from testing only. Unknown types are shown as plain
  sensors with the raw value; the diagnostics download lists them.
- Over PTVO, a frame longer than one Zigbee message, or containing the byte
  `0x7D`, can be split by the firmware. Error texts may then contain a stray
  `}` or lose their last character.
- Only one heater per serial port or MQTT topic.

## Troubleshooting

- **"Connected, but the heater did not answer"**: check the wiring and baud
  rate, and for PTVO that zigbee2mqtt shows the device and publishes `action`
  messages when the heater sends data.
- **Entities unavailable**: enable debug logging for
  `custom_components.eta_heater` and check whether `MD` frames arrive.
- Download diagnostics from the device page to see the monitor list and the
  last values.

## Removal

Delete the integration entry under **Settings → Devices & services**. The
cached monitor list is removed with it. Then remove the integration in HACS or
delete `custom_components/eta_heater`.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests use a simulated heater (`tests/fake_heater.py`) that answers like
the real controller, over both the serial and the PTVO/MQTT transport.
