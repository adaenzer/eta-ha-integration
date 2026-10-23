# ETA Heater (RS232) for Home Assistant

A Home Assistant integration for ETA heaters that have the older RS232 service
interface (the protocol with `{MA…}` / `{MD…}` frames, see
`Protokoll_ETA.pdf` in the [etainterface](https://github.com/adaenzer/etainterface)
repository). It replaces the AppDaemon app `eta2hass.py` from that repository.

Newer ETA heaters with ETAtouch offer a REST API; this integration is **not**
for those.

> [!IMPORTANT]
> **Maintenance:** this is a personal project for one specific heater. There
> is no intent to develop it further; it only receives bug fixes when
> something breaks for me. Feature requests will most likely not be
> implemented.
>
> **Forks welcome:** everyone may fork this repository and adapt it for their
> own use under the terms of the [MIT license](LICENSE).
>
> **Use at your own risk:** the code comes without any warranty. It talks to
> your heating system's controller; you are responsible for what it does on
> your installation.
>
> This project is not affiliated with or endorsed by ETA Heiztechnik GmbH.

## What it provides

All entities belong to one device per heater:

- **Sensors and binary sensors** for every value of the heater's monitor list.
  The names come from the heater. Temperatures, currents, voltages, percentages
  and operating hours get proper units; plain numbers whose name mentions a
  unit get that unit (in brackets like `[Hz]`, `[mV]`, `[A]`, or as a word
  like `kg`, `kW`, `bar`). A recognized unit in brackets is removed from the
  entity name, since Home Assistant shows the unit anyway. Status values (boiler, drive, hot
  water) are shown as raw numbers, see *Known limitations*. Each entity keeps
  the original monitor list data as attributes: `monitor_name`, `node`,
  `index`, `value_type`, `divider`, `level` and `section` (the heading of the
  heater's menu it is listed under).
- **Error event** (`event.<name>_error`) fires whenever the heater sends an
  error message (`IM`, `SE`, `SS`). Its attributes `error_type` and `message`
  hold the type and text.
- **Read monitor list again** button, for when the heater's configuration
  changed.
- **Read duration** diagnostic sensor: how long reading all enabled values
  took, to help choose the update interval.

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
Then you choose the monitor list levels (0–3). Only after that step is the
monitor list read from the heater, once.

### Options

| Option | Default | Description |
|---|---|---|
| Monitor list levels | `0` | Levels 0–3 of the monitor list. Deeper levels add more values. A change takes effect when you press *Read monitor list again*. |
| Update interval | 300 s | How often the enabled values are read. |
| Publish errors to MQTT | on | PTVO only: also publish errors to `<topic>/error`. |

Connection settings can be changed later with **Reconfigure**.

## How data is updated

Right after setup, the integration reads the monitor list of the selected
levels from the heater, in the background. This can take several minutes over
Zigbee; until it is done, the device only shows the button, the error event
and the read duration sensor. The value entities appear once the list was
read. If reading fails, a repair notice says so and the button starts a new
attempt. The list is
cached and only read again when you press one of the buttons on the device:

- **Read monitor list again (fill gaps)** reads the list and merges it into
  the cached one: values read again are updated, values missing so far are
  added, nothing is removed. Over a flaky Zigbee connection a read can miss
  entries; reading again fills such gaps.
- **Reset and read monitor list** starts over with an empty list, e.g. after
  the heater's configuration changed. Entities of values that are no longer
  listed stay in Home Assistant as "not provided" and can be deleted there.
 Changing the levels in the options does not read the list; a repair
notice reminds you to press the button. While the list is read, the current
values stay available; if reading fails, the previous list is kept.

Values are polled at the update interval: the integration subscribes to a
group of values, waits for the heater to send them, unsubscribes and continues
with the next group. Values the heater does not send become unavailable.

### Communication with the controller

The controller is easily overloaded, so the integration is strict:

- Only one thing talks to the heater at a time. While the monitor list is
  read, no values are read; the last values stay shown.
- A new request is only sent after the previous one was answered. A request
  without an answer counts as lost after 30 seconds (over Zigbee, answers
  sometimes stall for 10-25 seconds and then arrive in a batch).
- For the monitor list, the controller keeps its own cursor: each request
  returns the next entry, whatever position it asks for. So entries are
  collected in the order they arrive instead of being matched to requests,
  and a lost answer only leaves a gap (three lost answers in a row stop the
  read). Values: a group without answer stops the read of further groups.
- At least 0.5 seconds pass between two frames sent to the heater.
- Every value subscription is stopped again (`ME`), also after errors, and
  before the monitor list is read.
- While the connection is reconfigured, the running connection is stopped
  for the connection test.

### Values with the same name

The heater names values for its own menu, so different values can share a
name, e.g. the warming of the stoker motor and of the discharge motor are
both called "Erwärmung". Such names get a suffix:

1. Values of different kinds get their kind, translated:
   "AUSTRAGUNG (Motor)" and "AUSTRAGUNG (Status)".
2. Otherwise the nearest heading or motor above them in the monitor list:
   "Erwärmung (STOKERSCHNECKE)" and "Erwärmung (AUSTRAGUNG)".
3. Otherwise the nearest entry above them with another name:
   "StellmotSOLL (P-Luftschieber)" and "StellmotSOLL (S-Luftschieber)".
4. Otherwise node and index, e.g. "Wert (24/57)".

The same value listed several times (also under other names or in other
levels) is shown once, with the name it has where it is found first. You can
rename any entity in Home Assistant; the name stays when the list is read
again.

### Choosing which values are read

Only values whose entity is **enabled** are read from the heater. Values of
the lowest selected level are enabled when they are first created; values
that only deeper levels add are created disabled. Enable or disable single
entities on the device page (or under *Settings → Entities*) to choose what
is read. Fewer values make a read faster, which the *Read duration* sensor
shows, so a shorter update interval becomes possible. After enabling an
entity, Home Assistant reloads the integration within about 30 seconds.

## Where data is stored

Everything is stored by Home Assistant itself, nothing in zigbee2mqtt topics:

| Data | Location |
|---|---|
| Connection settings and options | Config entry (`.storage/core.config_entries`) |
| Which values are read (enabled entities) | Entity registry (`.storage/core.entity_registry`) |
| Cached monitor list | `.storage/eta_heater.<entry id>` |

This survives restarts and updates or reinstalls of the integration through
HACS, and it is part of Home Assistant backups, so restoring a backup on
another instance takes it along. Deleting the integration entry deletes all
of it; adding the heater again reads the monitor list from the heater again.

## Works with any heater

Nothing in the integration is tied to the values of one particular heater.
Everything is derived from the monitor list the heater sends:

- The **value type** (byte 4 of an entry) decides on/off value, temperature,
  percentage, current, voltage, speed or hours. The known types were found by
  testing; unknown types are shown as plain numbers.
- **Divider, node and index** come from each entry.
- **Units of plain numbers** come from the form of the name (brackets or a
  unit word), in any language.
- **Names of values sharing a name** come from the list's structure.
- **Which values start enabled** depends on the level they were found in.

If your heater uses value types that show up as plain numbers but should be
something else, the diagnostics download lists every entry with its type.

## Known limitations

- Only reading is supported, parameters cannot be changed.
- The meaning of the status numbers (value types 16, 23 and 45) is unknown.
  State names such as "Heizt", "Bereit" or "Störung" are known to exist, but
  not which number belongs to which name, so the raw number is shown. Once the
  mapping is confirmed on a real heater it can be added as an enum sensor.
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
- **Repair notice "Another program is talking to …"**: the heater sends
  values this integration did not request, so another program polls it at the
  same time, typically the former AppDaemon app. The controller cannot serve
  both, so reads fail. Stop the other program; in Home Assistant OS, turning
  off *Start on boot* does not stop a running app.
- **Entities unavailable**: enable debug logging for
  `custom_components.eta_heater` and check whether `MD` frames arrive.
- Download diagnostics from the device page to see the monitor list and the
  last values.
- To watch the frames exchanged over zigbee2mqtt, run
  `python tools/monitor_mqtt.py <broker> --user <user>` with this repository's
  test environment. It asks for the password, only listens and prints every
  frame decoded.

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

## License

[MIT](LICENSE)
