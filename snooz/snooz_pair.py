#!/usr/bin/env python3
"""Scan for a SNOOZ in pairing mode and save its address and password to ~/snooz.json.

Put the SNOOZ in pairing mode first: close the SNOOZ phone app, then hold the
power button about 5 seconds until all the buttons flash.
"""
import asyncio
import json
from pathlib import Path

from bleak import BleakScanner
from pysnooz.advertisement import get_device_model, parse_firmware_flags
from pysnooz.const import SNOOZ_ADVERTISEMENT_LENGTH
from pysnooz.model import SnoozDeviceModel

CONFIG = Path.home() / "snooz.json"
SCAN_SECONDS = 15


async def main() -> None:
    print(f"Scanning for {SCAN_SECONDS} seconds...")
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)

    snoozes = 0
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        if not name.lower().startswith(("snooz", "breez")):
            continue
        snoozes += 1
        payload = next(
            (v for v in adv.manufacturer_data.values() if len(v) == SNOOZ_ADVERTISEMENT_LENGTH),
            None,
        )
        flags = parse_firmware_flags(payload[0]) if payload else None
        if flags is None:
            print(f"{name} {device.address}: found, but its advertisement wasn't recognised")
            continue
        model = get_device_model(name, flags.firmware_version)
        print(f"{name} {device.address}: {model.name}, firmware {flags.firmware_version.name}, "
              f"pairing mode {'ON' if flags.is_pairing else 'off'}, signal {adv.rssi} dBm")
        if flags.is_pairing and model != SnoozDeviceModel.UNSUPPORTED:
            CONFIG.write_text(json.dumps({
                "address": device.address,
                "name": name,
                "model": model.value,
                "firmware": flags.firmware_version.value,
                "password": payload[1:].hex(),
            }, indent=2))
            print(f"Saved to {CONFIG}. You can now use snooz_ctl.py.")
            return

    if snoozes == 0:
        print("No SNOOZ seen. Is it powered on and within a few metres of the Pi?")
    else:
        print("Saw a SNOOZ but it wasn't in pairing mode. Hold its power button ~5 s "
              "until the buttons flash, then run this again right away.")


asyncio.run(main())
