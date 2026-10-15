#!/usr/bin/env python3
"""Record the Bangle.js 2 "Pi Sleep Log" stream to a CSV, one file per night.

Usage:
  python -u watch_logger.py            connect to the watch and log until Ctrl+C
  python -u watch_logger.py --forget   forget the saved watch and search again

The first run saves the watch's address to ~/bangle.json. Data goes to
~/sleeptracker/data/watch-YYYY-MM-DD.csv, named for the evening the night started.
It reconnects on its own if the watch drops off, and sets the watch's clock each time.

It also passes commands from wake.py to the watch: ~/sleeptracker/watch-cmd.json is
sent on and deleted, and ~/sleeptracker/watch-status.json says whether the watch is
connected, when data last arrived and when the alarm screen's button was pressed.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # lets the tests import this module without bleak; main() explains
    BleakClient = BleakScanner = None

CONFIG = Path.home() / "bangle.json"
DATA_DIR = Path.home() / "sleeptracker" / "data"
CMD = Path.home() / "sleeptracker" / "watch-cmd.json"
STATUS = Path.home() / "sleeptracker" / "watch-status.json"
UART_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Pi writes to the watch here
UART_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # watch sends to the Pi here
WATCH_FIELDS = ["watch_unix", "samples", "mov_sum", "mov_max", "ax", "ay", "az", "hr", "hr_conf", "battery"]
HEADER = ["time"] + WATCH_FIELDS + ["pi_unix"]
NO_DATA_WARNING_SECONDS = 60
POLL_SECONDS = 2
COMMAND_MAX_AGE = 5 * 60  # an older command missed its moment, so it is dropped unsent
STATUS_REFRESH_SECONDS = 30  # wake.py treats a status older than 60 s as disconnected
# \x10 stops the watch echoing these back. "alarm" leaves a running alarm screen alone (it sets PLA):
# reloading it would restart the screen's own 10-minute give-up timer.
WATCH_JS = {
    "alarm": '\x10if(!global.PLA)load("pilog.alarm.js")\n',
    "stop": "\x10load()\n",
    "buzz": "\x10Bangle.buzz(1000)\n",
}


def clock12(t: datetime, seconds: bool = False) -> str:
    text = f"{t.hour % 12 or 12}:{t.minute:02d}" + (f":{t.second:02d}" if seconds else "")
    return f"{text} {'AM' if t.hour < 12 else 'PM'}"


def stamp() -> str:
    return clock12(datetime.now(), seconds=True)


def night_file(now: datetime) -> Path:
    # Anything before noon belongs to the previous evening's night
    return DATA_DIR / f"watch-{(now - timedelta(hours=12)):%Y-%m-%d}.csv"


def discard(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        print(f"{stamp()} could not delete {path.name}: {e}")
        return False


def read_command(path: Path, now: float) -> str | None:
    """The command waiting in `path`, or None. The file is deleted whatever it held."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        # Raising here would end the session at every 2 s poll and lose the night's rows
        discard(path)
        print(f"{stamp()} dropped an unreadable command file: {e}")
        return None
    if not discard(path):
        return None  # sending it anyway would repeat the command every 2 s
    try:
        message = json.loads(text)
        cmd, sent = str(message["cmd"]), float(message["sent"])
    except (ValueError, KeyError, TypeError):
        print(f"{stamp()} deleted a bad command file: {text[:80]!r}")
        return None
    if now - sent > COMMAND_MAX_AGE:
        print(f"{stamp()} dropped a stale '{cmd}' command, sent {int(now - sent) // 60} min ago")
        return None
    return cmd


def command_js(cmd: str) -> str | None:
    return WATCH_JS.get(cmd)


def write_status(path: Path, connected: bool, last_row: int | None, awake: int | None) -> None:
    status = {"connected": connected, "last_row": last_row, "awake": awake, "updated": int(time.time())}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status))
    os.replace(tmp, path)  # readers never see a half-written file


class Recorder:
    def __init__(self, status_path: Path = STATUS) -> None:
        self.rows = 0
        self.last_row = time.monotonic()
        self.status_path = status_path
        self.connected = False
        self.last_row_unix = None  # Pi time of the last saved row
        self.awake = None  # watch time the alarm screen's button was pressed

    def save_status(self) -> None:
        write_status(self.status_path, self.connected, self.last_row_unix, self.awake)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.save_status()

    def handle_line(self, line: str) -> None:
        if not line.startswith("PL,"):
            return  # watch console chatter, ignore
        values = line[3:].split(",")
        if values[0] == "AWAKE":
            self.mark_awake(values[1:])
            return
        if len(values) != len(WATCH_FIELDS):
            print(f"{stamp()} skipped a garbled line: {line!r}")
            return
        watch_time = datetime.fromtimestamp(int(values[0]))
        path = night_file(datetime.now())
        new = not path.exists()
        pi_unix = int(time.time())
        with path.open("a") as f:
            if new:
                f.write(",".join(HEADER) + "\n")
            f.write(",".join([watch_time.isoformat(timespec="seconds"), *values, str(pi_unix)]) + "\n")
        self.rows += 1
        self.last_row = time.monotonic()
        self.last_row_unix = pi_unix
        self.save_status()
        if self.rows % 6 == 1:  # print about once a minute
            row = dict(zip(WATCH_FIELDS, values))
            print(f"{stamp()} saved {self.rows} rows to {path.name}: movement {row['mov_sum']}, "
                  f"heart rate {row['hr'] or '-'} (confidence {row['hr_conf']}), battery {row['battery']}%")

    def mark_awake(self, values: list[str]) -> None:
        try:
            self.awake = int(values[0])
        except (IndexError, ValueError):
            self.awake = int(time.time())  # the press is real even if its time got garbled
        self.save_status()
        print(f"{stamp()} watch button pressed: awake at {clock12(datetime.fromtimestamp(self.awake))}")


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


async def send_waiting_command(client: BleakClient) -> None:
    cmd = read_command(CMD, time.time())
    if cmd is None:
        return
    js = command_js(cmd)
    if js is None:
        print(f"{stamp()} ignored an unknown watch command: {cmd!r}")
        return
    await send(client, js)
    print(f"{stamp()} sent '{cmd}' to the watch")


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
        recorder.set_connected(True)
        recorder.last_row = time.monotonic()
        warned = status_saved = time.monotonic()
        while not disconnected.is_set():
            try:
                await asyncio.wait_for(disconnected.wait(), POLL_SECONDS)
            except TimeoutError:
                await send_waiting_command(client)
                now = time.monotonic()
                if now - recorder.last_row > NO_DATA_WARNING_SECONDS and now - warned > NO_DATA_WARNING_SECONDS:
                    warned = now
                    print(f"{stamp()} connected but no data for a minute. "
                          "Is Pi Sleep Log switched ON on the watch?")
                # Rows refresh the status every 10 s; this keeps it fresh when the watch isn't recording
                if now - recorder.last_row > STATUS_REFRESH_SECONDS and now - status_saved > STATUS_REFRESH_SECONDS:
                    status_saved = now
                    recorder.save_status()
    print(f"{stamp()} watch disconnected")


async def main() -> None:
    if BleakClient is None:
        sys.exit("bleak is not installed. Run this with ~/snooz-env/bin/python (install.sh sets it up).")
    if "--forget" in sys.argv:
        CONFIG.unlink(missing_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    recorder = Recorder()
    recorder.set_connected(False)
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
        recorder.set_connected(False)
        if recorder.rows > rows_before:
            delay = 5
        print(f"{stamp()} reconnecting in {delay} s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
