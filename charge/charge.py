#!/usr/bin/env python3
"""Keep the watch's battery at a cap while it sits in its dock, by switching the dock's 5 V on and off.

Usage:
  python -u charge.py            run the controller (the sleepcharge service does this)
  python charge.py --status      the last reading, the dock power and any problem
  python charge.py --on          hold the dock power on for an hour, then back to automatic
  python charge.py --off         hold the dock power off for an hour
  python charge.py --auto        end a hold now
  python charge.py --dry-run     run with the USB switch replaced by printed messages

Every minute it reads the watch's battery: from ~/sleeptracker/watch-status.json while watch_logger.py is
connected, otherwise from the watch's Bluetooth advertisement, which the Pi Sleep Log app fills with the
level, whether the charger is live, whether it is recording and whether it was stopped by hand (no
connection, so the Web IDE and the phone stay free). Dock power goes off when the level reaches charge_max
from settings.json (the web app's "Charge the watch to") and back on when it is 10 points below that; an
idle watch loses about a quarter of a percent an hour, so the dock stays off nearly all day and charges for
the half hour after the watch goes back in the morning. A recording or hand-stopped watch gets a powered
dock whatever its level, because only a live charger makes the watch app stop recording or clear a hand
stop. The dock is powered whenever the controller can't be sure (no reading for 10 minutes, at start, at
exit), so a problem never leaves the watch unable to charge. Each reading goes to
~/sleeptracker/watch-charge.json for the web app.

The switch is the Pi's own USB ports, through uhubctl: a Pi 3B+ switches all four together.
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import clean_settings  # noqa: E402

try:
    from bleak import BleakScanner
except ImportError:  # lets the tests import this module without bleak; the scan explains
    BleakScanner = None

BASE = Path.home() / "sleeptracker"
BANGLE_CONFIG = Path.home() / "bangle.json"  # written by watch_logger.py: the watch's address

POLL_SECONDS = 60
SCAN_SECONDS = 15                # one advertisement is enough; the watch sends one every 375 ms
LOGGER_STATUS_MAX_AGE = 90       # the logger refreshes watch-status.json at least every 30 s
READING_MAX_AGE = 600            # with nothing newer the dock is powered, to be safe
SWITCH_MIN_INTERVAL = 300        # a reading taken while charging runs high: let the cell rest before acting again
RESEND_SECONDS = 600             # the wanted state is sent again this often, in case the ports were switched by hand
RESUME_BELOW = 10                # power comes back this many points under charge_max
HOLD_SECONDS = 3600              # --on / --off last this long
CMD_MAX_AGE = 120                # an older charge-cmd.json is ignored (and deleted)
UHUBCTL_TIMEOUT = 30
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"  # service data: [battery %]
ESPRUINO_MANUFACTURER = 0x0590                              # manufacturer data: [flags]
FLAG_CHARGING, FLAG_RECORDING, FLAG_HOLD = 1, 2, 4          # the flags byte, as pilog.boot.js builds it
UNKNOWN, ON, OFF = "unknown", "on", "off"


# ---------- small helpers ----------

@dataclass(frozen=True)
class Paths:
    base: Path
    settings: Path
    status: Path   # watch-status.json, from watch_logger.py
    state: Path    # watch-charge.json, for the web app and --status
    cmd: Path      # charge-cmd.json, from --on / --off / --auto
    bangle: Path   # ~/bangle.json

    @classmethod
    def under(cls, base: Path, bangle: Path = BANGLE_CONFIG) -> "Paths":
        return cls(base, base / "settings.json", base / "watch-status.json", base / "watch-charge.json",
                   base / "charge-cmd.json", bangle)


@dataclass(frozen=True)
class Reading:
    battery: int
    charging: bool | None
    at: int
    source: str  # "logger" or "advert"
    recording: bool = False
    hold: bool = False  # stopped by hand: only a live charger lets it record again

    @property
    def needs_charger(self) -> bool:
        return self.recording or self.hold


class SwitchError(Exception):
    pass


def clock12(unix: float) -> str:
    return datetime.fromtimestamp(unix).strftime("%I:%M %p").lstrip("0")


def stamp(unix: float) -> str:
    return datetime.fromtimestamp(unix).strftime("%I:%M:%S %p")


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # readers never see a half-written file


def _print(line: str) -> None:
    print(line, flush=True)


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _percent(value) -> int | None:
    number = _int(value)
    return number if number is not None and 0 <= number <= 100 else None


# ---------- reading the battery ----------

def read_logger_status(path: Path, now: float) -> Reading | None:
    """The level from watch-status.json while the logger is connected: it hears the watch's PB lines."""
    data = read_json(path) or {}
    updated = _int(data.get("updated"))
    if data.get("connected") is not True or updated is None or now - updated > LOGGER_STATUS_MAX_AGE:
        return None
    battery = _percent(data.get("battery"))
    if battery is None:
        return None
    charging = data.get("charging")
    return Reading(battery, charging if isinstance(charging, bool) else None, updated, "logger",
                   data.get("recording") is True, data.get("hold") is True)


def parse_advert(service_data: dict, manufacturer_data: dict) -> tuple[int | None, int | None]:
    """(battery %, flags byte) from a Bangle.js advertisement, or None for each part that isn't there."""
    battery = None
    for uuid, data in (service_data or {}).items():
        if str(uuid).lower() == BATTERY_SERVICE and len(data) >= 1:
            battery = _percent(data[0])
    data = (manufacturer_data or {}).get(ESPRUINO_MANUFACTURER)
    flags = int(data[0]) if data and len(data) >= 1 else None
    return battery, flags


def reading_from_advert(battery: int, flags: int | None, now: float) -> Reading:
    if flags is None:
        return Reading(battery, None, int(now), "advert")
    return Reading(battery, bool(flags & FLAG_CHARGING), int(now), "advert",
                   bool(flags & FLAG_RECORDING), bool(flags & FLAG_HOLD))


def watch_matcher(bangle_config: Path) -> Callable:
    """Matches the watch saved by watch_logger.py, or any Bangle.js when none is saved yet."""
    saved = read_json(bangle_config) or {}
    address = str(saved.get("address", "")).upper()

    def match(device, adv) -> bool:
        if address:
            return device.address.upper() == address
        return (adv.local_name or device.name or "").startswith("Bangle.js")
    return match


async def _scan_async(match: Callable, timeout: float) -> tuple[int, int | None] | None:
    found = asyncio.Event()
    result = None

    def on_advert(device, adv) -> None:
        nonlocal result
        if result is not None or not match(device, adv):
            return
        battery, flags = parse_advert(adv.service_data, adv.manufacturer_data)
        if battery is None:
            return  # the watch is there but runs an app without the broadcast
        result = (battery, flags)
        found.set()

    async with BleakScanner(on_advert):
        try:
            await asyncio.wait_for(found.wait(), timeout)
        except TimeoutError:
            pass
    return result


def scan_advert(bangle_config: Path, now: float, timeout: float = SCAN_SECONDS) -> Reading | None:
    if BleakScanner is None:
        raise SwitchError("bleak is not installed. Run this with ~/snooz-env/bin/python (install.sh sets it up).")
    result = asyncio.run(_scan_async(watch_matcher(bangle_config), timeout))
    if result is None:
        return None
    return reading_from_advert(result[0], result[1], now)


# ---------- the USB switch ----------

class Switch:
    name = "switch"

    def set(self, on: bool) -> None:
        raise NotImplementedError


class UhubctlSwitch(Switch):
    """The Pi's own USB ports. A Pi 3B+ (hub 1-1, port 2) switches all four at once."""

    def __init__(self, hub: str = "1-1", port: int = 2) -> None:
        self.hub, self.port = hub, port
        self.name = f"uhubctl (hub {hub} port {port})"

    def set(self, on: bool) -> None:
        command = ["uhubctl", "-l", self.hub, "-p", str(self.port), "-a", ON if on else OFF]
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=UHUBCTL_TIMEOUT)
        except FileNotFoundError:
            raise SwitchError("uhubctl is not installed (install.sh installs it)") from None
        except subprocess.TimeoutExpired:
            raise SwitchError(f"uhubctl did not finish in {UHUBCTL_TIMEOUT} s") from None
        if run.returncode != 0:
            output = (run.stderr.strip() or run.stdout.strip()).splitlines()
            raise SwitchError(f"uhubctl failed (exit {run.returncode}): {' | '.join(output[-3:]) or 'no output'}")


class FakeSwitch(Switch):
    name = "fake switch"

    def __init__(self, clock=time.time, fail: bool = False) -> None:
        self.clock = clock
        self.fail = fail
        self.calls: list = []  # (time, on)

    def set(self, on: bool) -> None:
        if self.fail:
            raise SwitchError("fake switch failure")
        self.calls.append((self.clock(), on))
        _print(f"{stamp(self.clock())} [dry run] would switch the dock power {ON if on else OFF}")


def switch_from_settings(raw: dict | None) -> Switch:
    """settings.json may carry "watch_charger": {"switch": "uhubctl", "hub": "1-1", "port": 2}."""
    config = (raw or {}).get("watch_charger")
    config = config if isinstance(config, dict) else {}
    kind = config.get("switch", "uhubctl")
    if kind != "uhubctl":
        raise SwitchError(f"unknown watch_charger switch {kind!r} in settings.json (only \"uhubctl\" exists)")
    port = _int(config.get("port"))
    return UhubctlSwitch(str(config.get("hub", "1-1")), port if port is not None else 2)


# ---------- the decision ----------

def desired_power(power: str, battery: int | None, charge_max: int, needs_charger: bool = False) -> str:
    """Off at the cap, on RESUME_BELOW points under it, otherwise as it is; on whenever unsure
    and whenever the watch needs a live charger (it is recording, or was stopped by hand)."""
    if needs_charger or battery is None or battery <= charge_max - RESUME_BELOW:
        return ON
    if battery >= charge_max:
        return OFF
    return power if power in (ON, OFF) else ON


class Controller:
    def __init__(self, paths: Paths, switch: Switch, clock=time.time, scan: Callable | None = None) -> None:
        self.paths = paths
        self.switch = switch
        self.clock = clock
        self.scan = scan if scan is not None else (lambda now: scan_advert(paths.bangle, now))
        self.power = UNKNOWN
        self.switched_at: float | None = None  # the last change of state
        self.sent_at: float | None = None      # the last command to the switch, changes and re-sends alike
        self.reading: Reading | None = None
        self.hold: tuple[str, int] | None = None  # (power, until)
        self.problems: dict = {}  # "read" / "switch" -> what is wrong, for the state file and the log
        self.charge_max = clean_settings({})["charge_max"]

    # -- inputs --

    def take_command(self, now: float) -> None:
        data = read_json(self.paths.cmd)
        if data is None and not self.paths.cmd.exists():
            return
        try:
            self.paths.cmd.unlink()
        except OSError:
            pass
        if data is None:
            _print(f"{stamp(now)} deleted a bad charge-cmd.json")
            return
        sent = _int(data.get("sent"))
        if sent is None or now - sent > CMD_MAX_AGE:
            _print(f"{stamp(now)} ignored a stale charge command")
            return
        cmd = data.get("cmd")
        if cmd in (ON, OFF):
            self.hold = (cmd, int(now) + HOLD_SECONDS)
            _print(f"{stamp(now)} holding the dock power {cmd} until {clock12(self.hold[1])}")
        elif cmd == "auto":
            self.hold = None
            _print(f"{stamp(now)} back to automatic")
        else:
            _print(f"{stamp(now)} ignored an unknown charge command: {cmd!r}")

    def read(self, now: float) -> None:
        reading = read_logger_status(self.paths.status, now)
        if reading is None:
            try:
                reading = self.scan(now)
            except SwitchError as e:
                self.note_problem(now, "read", str(e))
                return
            except Exception as e:  # a Bluetooth hiccup must not stop the tick: the dock still gets decided
                self.note_problem(now, "read", f"scan failed: {type(e).__name__}: {e}")
                return
        self.problems.pop("read", None)
        if reading is not None:
            if self.reading is None or self.describe(reading) != self.describe(self.reading):
                _print(f"{stamp(now)} watch battery {self.describe(reading)} (from the {reading.source})")
            self.reading = reading

    @staticmethod
    def describe(reading: Reading) -> str:
        words = [f"{reading.battery}%"]
        if reading.charging:
            words.append("charger live")
        if reading.recording:
            words.append("recording")
        if reading.hold:
            words.append("stopped by hand")
        return ", ".join(words)

    def fresh_reading(self, now: float) -> Reading | None:
        if self.reading is None or now - self.reading.at > READING_MAX_AGE:
            return None
        return self.reading

    # -- the tick --

    def tick(self) -> None:
        now = self.clock()
        self.charge_max = clean_settings(read_json(self.paths.settings) or {})["charge_max"]
        self.take_command(now)
        if self.hold and now >= self.hold[1]:
            self.hold = None
            _print(f"{stamp(now)} the hold is over, back to automatic")
        self.read(now)
        if self.hold:
            wanted, why = self.hold[0], "held"
        else:
            reading = self.fresh_reading(now)
            if reading is None:
                wanted, why = desired_power(self.power, None, self.charge_max), "no recent battery reading"
            else:
                wanted = desired_power(self.power, reading.battery, self.charge_max, reading.needs_charger)
                why = f"battery {reading.battery}%, cap {self.charge_max}%"
                if reading.needs_charger:
                    why += ", the watch is " + ("recording" if reading.recording else "stopped by hand")
        if wanted != self.power:
            settled = self.switched_at is None or now - self.switched_at >= SWITCH_MIN_INTERVAL
            if settled or self.hold or self.power == UNKNOWN:
                self.set_power(now, wanted, why)
        elif self.sent_at is None or now - self.sent_at >= RESEND_SECONDS:
            self.set_power(now, wanted, why, change=False)
        self.write_state(now)

    def set_power(self, now: float, wanted: str, why: str, change: bool = True) -> None:
        try:
            self.switch.set(wanted == ON)
        except SwitchError as e:
            self.power = UNKNOWN
            self.note_problem(now, "switch", f"could not switch the dock power {wanted}: {e}")
            return
        self.sent_at = now
        if self.problems.pop("switch", None):
            _print(f"{stamp(now)} the switch works again")
        if change or self.power != wanted:
            self.power = wanted
            self.switched_at = now
            _print(f"{stamp(now)} dock power {wanted} ({why})")

    def note_problem(self, now: float, kind: str, problem: str) -> None:
        if problem != self.problems.get(kind):
            _print(f"{stamp(now)} {problem}")
        self.problems[kind] = problem

    @property
    def problem(self) -> str | None:
        return "; ".join(self.problems[kind] for kind in ("switch", "read") if kind in self.problems) or None

    def write_state(self, now: float) -> None:
        reading = self.reading
        write_json_atomic(self.paths.state, {
            "battery": reading.battery if reading else None,
            "charging": reading.charging if reading else None,
            "recording": reading.recording if reading else None,
            "hold_by_hand": reading.hold if reading else None,
            "power": self.power,
            "source": reading.source if reading else None,
            "read_at": reading.at if reading else None,
            "charge_max": self.charge_max,
            "hold": {"power": self.hold[0], "until": self.hold[1]} if self.hold else None,
            "problem": self.problem,
            "updated": int(now),
        })

    def restore(self) -> None:
        """At exit: leave the dock powered, so a stopped service never keeps the watch from charging."""
        now = self.clock()
        if self.power != ON:
            self.set_power(now, ON, "charge.py stopping")
        self.write_state(now)


# ---------- commands ----------

def run_service(paths: Paths, dry_run: bool) -> int:
    if dry_run:
        switch = FakeSwitch()
        folder = paths.base / "charge-dry-run"  # the real settings and logger status, a separate state file
        paths = replace(paths, state=folder / paths.state.name, cmd=folder / paths.cmd.name)
        _print(f"Dry run: the USB switch is replaced by printed messages; state goes to {paths.state}")
    else:
        try:
            switch = switch_from_settings(read_json(paths.settings))
        except SwitchError as e:
            _print(str(e))
            return 1
    controller = Controller(paths, switch)
    _print(f"{stamp(time.time())} charge.py started: {switch.name}, reading {paths.settings}")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    last_error = None
    try:
        while True:
            started = time.monotonic()
            try:
                controller.tick()
                last_error = None
            except Exception:  # keep going; print each new error once
                error = traceback.format_exc()
                if error != last_error:
                    _print(f"{stamp(time.time())} error:\n{error}")
                last_error = error
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        controller.restore()
        _print("Stopped.")
    return 0


def send_command(paths: Paths, cmd: str) -> int:
    write_json_atomic(paths.cmd, {"cmd": cmd, "sent": int(time.time())})
    if cmd == "auto":
        _print("Asked the sleepcharge service to go back to automatic. It acts within a minute.")
    else:
        _print(f"Asked the sleepcharge service to hold the dock power {cmd} for an hour. It acts within a minute; "
               f"check with --status. (If the service isn't running, nothing happens.)")
    return 0


def show_status(paths: Paths) -> int:
    now = time.time()
    state = read_json(paths.state)
    if state is None:
        _print(f"No state yet ({paths.state} is missing). Is the sleepcharge service running?")
        return 1
    updated = _int(state.get("updated"))
    age = f"{int((now - updated) // 60)} min ago" if updated is not None else "unknown"
    battery = _percent(state.get("battery"))
    if battery is None:
        _print("Watch battery: no reading yet")
    else:
        read_at = _int(state.get("read_at"))
        when = f" at {clock12(read_at)}" if read_at is not None else ""
        words = [f"{battery}%"]
        words += {True: ["charger live"], False: ["not charging"]}.get(state.get("charging"), [])
        words += ["recording"] if state.get("recording") is True else []
        words += ["stopped by hand"] if state.get("hold_by_hand") is True else []
        _print(f"Watch battery: {', '.join(words)} (from the {state.get('source')}{when})")
    _print(f"Dock power:    {state.get('power')} (cap {state.get('charge_max')}%)")
    hold = state.get("hold")
    if isinstance(hold, dict) and _int(hold.get("until")) is not None:
        _print(f"Hold:          {hold.get('power')} until {clock12(hold['until'])}")
    if state.get("problem"):
        _print(f"Problem:       {state['problem']}")
    _print(f"Last updated:  {age}" + ("" if updated is not None and now - updated < 3 * POLL_SECONDS
                                       else " (is the sleepcharge service running?)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch the watch dock's power to keep its battery at a cap.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="the last reading and the dock power")
    group.add_argument("--on", action="store_true", help="hold the dock power on for an hour")
    group.add_argument("--off", action="store_true", help="hold the dock power off for an hour")
    group.add_argument("--auto", action="store_true", help="end a hold now")
    group.add_argument("--dry-run", action="store_true", help="run with the USB switch replaced by printed messages")
    args = parser.parse_args(argv)
    paths = Paths.under(BASE)
    if args.status:
        return show_status(paths)
    for cmd in (ON, OFF, "auto"):
        if getattr(args, cmd):
            return send_command(paths, cmd)
    return run_service(paths, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
