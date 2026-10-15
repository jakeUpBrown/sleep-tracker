#!/usr/bin/env python3
"""Interactive SNOOZ control. Connects to the SNOOZ saved by snooz_pair.py,
stays connected, and lets you type commands until you quit."""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

from bleak import BleakScanner
from pysnooz import (
    SnoozAdvertisementData,
    SnoozCommandResultStatus,
    SnoozDevice,
    SnoozDeviceModel,
    SnoozFirmwareVersion,
    disable_night_mode,
    enable_night_mode,
    set_light_brightness,
    set_volume,
    turn_light_off,
    turn_light_on,
    turn_off,
    turn_on,
)

CONFIG = Path.home() / "snooz.json"

HELP = """
Commands:
  on [VOLUME]              turn on (optionally at VOLUME 0-100)
  off                      turn off
  volume VOLUME            set volume 0-100
  fadein VOLUME SECONDS    turn on and fade up to VOLUME over SECONDS
  fadeout SECONDS          fade down and turn off over SECONDS
  light on [BRIGHTNESS]    button lights on (SNOOZ Pro)
  light off                button lights off
  brightness BRIGHTNESS    button light brightness 0-100
  night on|off             night mode (dims the lights)
  status                   show the current state
  help                     show this list
  quit                     disconnect and exit
"""


def seconds(value: str) -> timedelta:
    return timedelta(seconds=int(value))


def parse(words: list[str]):
    """Turn typed words into a pysnooz command, or raise ValueError."""
    cmd, args = words[0].lower(), words[1:]
    if cmd == "on":
        return turn_on(volume=int(args[0]) if args else None)
    if cmd == "off":
        return turn_off()
    if cmd == "volume":
        return set_volume(int(args[0]))
    if cmd == "fadein":
        return turn_on(volume=int(args[0]), duration=seconds(args[1]))
    if cmd == "fadeout":
        return turn_off(duration=seconds(args[0]))
    if cmd == "light" and args and args[0] == "on":
        return turn_light_on(int(args[1]) if len(args) > 1 else None)
    if cmd == "light" and args and args[0] == "off":
        return turn_light_off()
    if cmd == "brightness":
        return set_light_brightness(int(args[0]))
    if cmd == "night" and args and args[0] in ("on", "off"):
        return enable_night_mode() if args[0] == "on" else disable_night_mode()
    raise ValueError


async def connect() -> SnoozDevice:
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} not found. Run snooz_pair.py first.")
    saved = json.loads(CONFIG.read_text())

    print(f"Looking for {saved['name']} ({saved['address']})...")
    ble_device = await BleakScanner.find_device_by_address(saved["address"], timeout=15)
    if ble_device is None:
        sys.exit("Couldn't find it. Is the SNOOZ on and the phone app closed?")

    adv = SnoozAdvertisementData(
        SnoozDeviceModel(saved["model"]),
        SnoozFirmwareVersion(saved["firmware"]),
        saved["password"],
    )
    return SnoozDevice(ble_device, adv)


async def main() -> None:
    device = await connect()
    # Fetching device info forces the connection now instead of on the first command.
    await device.async_get_info()
    print(f"Connected. {device.state}")
    print(HELP)

    try:
        while True:
            line = (await asyncio.to_thread(input, "snooz> ")).strip()
            if not line:
                continue
            words = line.split()
            if words[0] in ("quit", "exit", "q"):
                break
            if words[0] in ("help", "?"):
                print(HELP)
                continue
            if words[0] == "status":
                print(device.state)
                continue
            try:
                command = parse(words)
            except (ValueError, IndexError):
                print("Didn't understand that. Type 'help' for the list.")
                continue
            if command.duration:
                print(f"Fading over {command.duration.seconds} s...")
            result = await device.async_execute_command(command)
            if result.status == SnoozCommandResultStatus.SUCCESSFUL:
                print(f"OK. {device.state}")
            else:
                print(f"Failed: {result.status.name}")
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        await device.async_disconnect()
        print("Disconnected.")


asyncio.run(main())
