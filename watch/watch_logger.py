#!/usr/bin/env python3
"""Record the Bangle.js 2 "Pi Sleep Log" stream to a CSV, one file per night.

Usage:
  python -u watch_logger.py            connect to the watch and log until Ctrl+C
  python -u watch_logger.py --forget   forget the saved watch and search again

The first run saves the watch's address to ~/bangle.json. Data goes to
~/sleeptracker/data/watch-YYYY-MM-DD.csv, named for the evening the night started.
It reconnects on its own if the watch drops off, and sets the watch's clock each time.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from bleak import BleakClient, BleakScanner

CONFIG = Path.home() / "bangle.json"
DATA_DIR = Path.home() / "sleeptracker" / "data"
UART_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Pi writes to the watch here
UART_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # watch sends to the Pi here
WATCH_FIELDS = ["watch_unix", "samples", "mov_sum", "mov_max", "ax", "ay", "az", "hr", "hr_conf", "battery"]
HEADER = ["time"] + WATCH_FIELDS + ["pi_unix"]
NO_DATA_WARNING_SECONDS = 60


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def night_file(now: datetime) -> Path:
    # Anything before noon belongs to the previous evening's night
    return DATA_DIR / f"watch-{(now - timedelta(hours=12)):%Y-%m-%d}.csv"


class Recorder:
    def __init__(self) -> None:
        self.rows = 0
        self.last_row = time.monotonic()

    def handle_line(self, line: str) -> None:
        if not line.startswith("PL,"):
            return  # watch console chatter, ignore
        values = line[3:].split(",")
        if len(values) != len(WATCH_FIELDS):
            print(f"{stamp()} skipped a garbled line: {line!r}")
            return
        watch_time = datetime.fromtimestamp(int(values[0]))
        path = night_file(datetime.now())
        new = not path.exists()
        with path.open("a") as f:
            if new:
                f.write(",".join(HEADER) + "\n")
            f.write(",".join([watch_time.isoformat(timespec="seconds"), *values, str(int(time.time()))]) + "\n")
        self.rows += 1
        self.last_row = time.monotonic()
        if self.rows % 6 == 1:  # print about once a minute
            row = dict(zip(WATCH_FIELDS, values))
            print(f"{stamp()} saved {self.rows} rows to {path.name}: movement {row['mov_sum']}, "
                  f"heart rate {row['hr'] or '-'} (confidence {row['hr_conf']}), battery {row['battery']}%")


async def find_watch():
    if CONFIG.exists():
        address = json.loads(CONFIG.read_text())["address"]
        return await BleakScanner.find_device_by_address(address, timeout=20)
    print(f"{stamp()} looking for a Bangle.js...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: (adv.local_name or d.name or "").startswith("Bangle.js"), timeout=30)
    if device:
        CONFIG.write_text(json.dumps({"address": device.address, "name": device.name}, indent=2))
        print(f"{stamp()} found {device.name} ({device.address}), saved to {CONFIG}")
    return device


async def send(client: BleakClient, text: str) -> None:
    data = text.encode()
    for i in range(0, len(data), 20):
        await client.write_gatt_char(UART_RX, data[i:i + 20], response=True)


async def run_session(device, recorder: Recorder) -> None:
    buffer = bytearray()
    disconnected = asyncio.Event()

    def on_data(_, data: bytearray) -> None:
        buffer.extend(data)
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer[:] = rest
            recorder.handle_line(line.decode(errors="replace").strip())

    async with BleakClient(device, disconnected_callback=lambda _: disconnected.set()) as client:
        await client.start_notify(UART_TX, on_data)
        # \x10 stops the watch echoing the command back
        await send(client, f"\x10setTime({time.time():.3f})\n")
        print(f"{stamp()} connected to the watch and set its clock")
        recorder.last_row = time.monotonic()
        while not disconnected.is_set():
            try:
                await asyncio.wait_for(disconnected.wait(), NO_DATA_WARNING_SECONDS)
            except TimeoutError:
                if time.monotonic() - recorder.last_row > NO_DATA_WARNING_SECONDS:
                    print(f"{stamp()} connected but no data for a minute. "
                          "Is Pi Sleep Log switched ON on the watch?")
    print(f"{stamp()} watch disconnected")


async def main() -> None:
    if "--forget" in sys.argv:
        CONFIG.unlink(missing_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    recorder = Recorder()
    delay = 5
    while True:
        device = await find_watch()
        if device is None:
            print(f"{stamp()} watch not found. Is it nearby, and is your phone or laptop "
                  "disconnected from it? Trying again in 30 s.")
            await asyncio.sleep(30)
            continue
        rows_before = recorder.rows
        try:
            await run_session(device, recorder)
        except Exception as e:  # BlueZ errors vary a lot; just retry
            print(f"{stamp()} connection problem: {e}")
        if recorder.rows > rows_before:
            delay = 5
        print(f"{stamp()} reconnecting in {delay} s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped.")
