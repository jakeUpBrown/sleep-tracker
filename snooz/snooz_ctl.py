#!/usr/bin/env python3
"""Control the SNOOZ saved by snooz_pair.py.

Usage:
  python snooz_ctl.py on [VOLUME]        turn on, optionally at VOLUME (0-100)
  python snooz_ctl.py off                turn off
  python snooz_ctl.py volume VOLUME      set volume (0-100)
  python snooz_ctl.py fadeout SECONDS    fade volume down and turn off over SECONDS
"""
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
    set_volume,
    turn_off,
    turn_on,
)

CONFIG = Path.home() / "snooz.json"


def build_command(args: list[str]):
    if not args:
        sys.exit(__doc__)
    action = args[0]
    if action == "on":
        return turn_on(volume=int(args[1]) if len(args) > 1 else None)
    if action == "off":
        return turn_off()
    if action == "volume" and len(args) > 1:
        return set_volume(int(args[1]))
    if action == "fadeout" and len(args) > 1:
        return turn_off(duration=timedelta(seconds=int(args[1])))
    sys.exit(__doc__)


async def main() -> None:
    command = build_command(sys.argv[1:])
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} not found. Run snooz_pair.py first.")
    saved = json.loads(CONFIG.read_text())

    ble_device = await BleakScanner.find_device_by_address(saved["address"], timeout=15)
    if ble_device is None:
        sys.exit(f"Couldn't find {saved['address']}. Is the SNOOZ on and the phone app closed?")

    adv = SnoozAdvertisementData(
        SnoozDeviceModel(saved["model"]),
        SnoozFirmwareVersion(saved["firmware"]),
        saved["password"],
    )
    device = SnoozDevice(ble_device, adv)
    try:
        result = await device.async_execute_command(command)
        if result.status == SnoozCommandResultStatus.SUCCESSFUL:
            print(f"Done in {result.duration}. State: {device.state}")
        else:
            print(f"Command failed: {result.status.name}")
    finally:
        await device.async_disconnect()


asyncio.run(main())
