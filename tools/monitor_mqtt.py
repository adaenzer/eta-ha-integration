"""Show the ETA frames exchanged through zigbee2mqtt and a PTVO device.

Usage:
    python tools/monitor_mqtt.py HOST [--port 1883] [--user USER] [--topic zigbee2mqtt/heater]

The password is asked for interactively (or read from MQTT_PASSWORD), so it
never ends up in the shell history. Only listens, never publishes.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import sys

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib

from custom_components.eta_heater.protocol import (
    Frame,
    FrameParser,
    parse_monitor_entry,
    parse_values,
)
from custom_components.eta_heater.transport import (
    PTVO_PACKET_END,
    decode_ptvo_action,
)


def describe(frame: Frame) -> str:
    """Return a readable summary of a frame."""
    text = f"{frame.service} len={len(frame.payload)}"
    if not frame.checksum_ok:
        text += " CHECKSUM MISMATCH"
    if frame.is_error:
        return f"{text} ERROR: {frame.text!r}"
    if frame.service == "Mb":
        entry = parse_monitor_entry(frame.payload)
        if entry is None:
            return f"{text} (end of list)"
        return (
            f"{text} node={entry.node} index={entry.index} type={entry.value_type} "
            f"divider={entry.divider} name={entry.name!r}"
        )
    if frame.service == "MD":
        return f"{text} values={parse_values(frame.payload)}"
    if frame.service == "MA" and len(frame.payload) == 2:
        return f"{text} position={frame.payload[0]} level={frame.payload[1]}"
    if frame.service == "MC" and frame.payload:
        keys = [
            (frame.payload[i], frame.payload[i + 1] * 256 + frame.payload[i + 2])
            for i in range(1, len(frame.payload) - 2, 3)
        ]
        return f"{text} refresh={frame.payload[0]}s keys={keys}"
    return f"{text} payload={frame.payload.hex(' ')}"


def log(direction: str, message: str) -> None:
    """Print a line with a timestamp."""
    print(f"{datetime.now():%H:%M:%S.%f}"[:-3], direction, message, flush=True)


def main() -> None:
    """Connect and print the traffic until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user")
    parser.add_argument("--topic", default="zigbee2mqtt/heater")
    args = parser.parse_args()
    password = None
    if args.user:
        password = os.environ.get("MQTT_PASSWORD") or getpass.getpass("MQTT password: ")

    from_heater = FrameParser(lambda frame: log("heater ->", describe(frame)))
    to_heater = FrameParser(lambda frame: log("-> heater", describe(frame)))

    def on_connect(client, userdata, flags, reason, properties):
        if reason.is_failure:
            log("!!", f"connection refused: {reason}")
            return
        log("--", f"connected, listening on {args.topic} and {args.topic}/set")
        client.subscribe([(args.topic, 0), (f"{args.topic}/set", 0)])

    def on_message(client, userdata, message):
        try:
            payload = json.loads(message.payload)
        except ValueError:
            log("??", f"{message.topic}: {message.payload!r}")
            return
        if not isinstance(payload, dict) or "action" not in payload:
            return
        data = decode_ptvo_action(payload["action"])
        if data is None:
            log("??", f"{message.topic}: action={payload['action']!r}")
            return
        if message.topic.endswith("/set"):
            # The first byte is the length PTVO expects
            to_heater.feed(data[1:])
        else:
            log("   raw", data.hex(" "))
            from_heater.feed(data + bytes([PTVO_PACKET_END]))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if args.user:
        client.username_pw_set(args.user, password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    with contextlib.suppress(KeyboardInterrupt):
        client.loop_forever()


if __name__ == "__main__":
    main()
