#!/usr/bin/env python3
"""Smart alarm: fade the SNOOZ out at the first sign of light sleep, then buzz the watch.

Usage:
  python -u wake.py                   run the alarm (the sleepwake service does this)
  python wake.py --status             the next alarm's timeline and whether the watch is connected
  python wake.py --simulate DATE      what it would have done on the night of DATE (YYYY-MM-DD)
  python wake.py --test-buzz          ask the watch logger to buzz the watch once
  python wake.py --test-fade SECONDS  fade the SNOOZ out now, over SECONDS
  python wake.py --dry-run            run the alarm with the SNOOZ and watch replaced by printed messages

alarm_time in ~/sleeptracker/settings.json is the time to be awake by. From window_minutes before it,
tonight's watch CSV goes through the same Detector as the web app's chart, and the first arousal starts
the SNOOZ fade. With none by alarm - fade_minutes the fade starts anyway; a window no longer than the fade
means no watching, and a window_minutes fade from when the window opens. The fade is timed to end by the
alarm, and a failed one is tried again while there is time. At the alarm, watch_logger.py is asked
(through watch-cmd.json) to buzz the watch until its button is pressed, for up to 10 minutes. Each night
is logged to ~/sleeptracker/data/wake-DATE.json.

Settings are re-read whenever settings.json changes. Before an alarm's window opens a change just replans it;
inside it only what has not happened yet moves (a started fade runs on; "off" then "on" before the buzz resumes).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import (  # noqa: E402
    Detector, Row, alarm_unix_for_night, clean_settings, parse_csv_line, read_rows, simulate_wake, wake_window,
)

REPO = Path(__file__).resolve().parents[1]
BASE = Path.home() / "sleeptracker"
SNOOZ_SCRIPT = REPO / "snooz" / "snooz_ctl.py"

POLL_SECONDS = 10                 # the service loop runs tick() this often
MIN_FADE_SECONDS = 30             # shortest fade, e.g. when the alarm is already due
FADE_ATTEMPTS = 3                 # tries at the SNOOZ fade, e.g. when snooz_ctl.py's scan misses it
NO_DATA_SECONDS = 180             # no watch rows this recent means the logger isn't delivering
BUZZ_RESEND_SECONDS = 120         # ask again this often while connected and the button isn't pressed
BUZZ_GIVE_UP_SECONDS = 600        # stop asking this long after the alarm (the watch gives up too)
AWAKE_SLACK_SECONDS = 60          # a button press this soon before the alarm still counts as awake
STATUS_MAX_AGE_SECONDS = 60       # an older watch-status.json means the logger is gone
FADE_TIMEOUT_SLACK_SECONDS = 120  # on top of the fade itself: finding and connecting to the SNOOZ
OUTPUT_LINES = 5                  # lines of snooz_ctl.py output kept in the wake log
TEST_BUZZ_WAIT_SECONDS = 20       # --test-buzz waits this long for the logger to take the command

IDLE, WINDOW, FADING, BUZZING, DONE = "idle", "window", "fading", "buzzing", "done"
CANCELLED = "cancelled"  # switched off in the window or fade: back on before the alarm is over resumes it


# ---------- small helpers ----------

@dataclass(frozen=True)
class Paths:
    base: Path
    settings: Path
    data: Path
    status: Path
    cmd: Path
    logs: Path  # where wake-DATE.json goes: data, except for --dry-run

    @classmethod
    def under(cls, base: Path, logs: Path | None = None) -> "Paths":
        data = base / "data"
        return cls(base, base / "settings.json", data, base / "watch-status.json", base / "watch-cmd.json",
                   logs or data)

    def night_csv(self, date: str) -> Path:
        return self.data / f"watch-{date}.csv"

    def wake_log(self, date: str) -> Path:
        return self.logs / f"wake-{date}.json"


def night_date(unix: float) -> str:
    # Same rule as watch_logger.py: anything before noon belongs to the previous evening
    return f"{datetime.fromtimestamp(unix) - timedelta(hours=12):%Y-%m-%d}"


def clock12(unix: float) -> str:
    return datetime.fromtimestamp(unix).strftime("%I:%M %p").lstrip("0")


def stamp(unix: float) -> str:
    return datetime.fromtimestamp(unix).strftime("%I:%M:%S %p")


def day_and_time(unix: float) -> str:
    when = datetime.fromtimestamp(unix)
    return f"{when:%a %b} {when.day}, {clock12(unix)}"


def time_words(alarm_time: str) -> str:
    return datetime.strptime(alarm_time, "%H:%M").strftime("%I:%M %p").lstrip("0")


def duration_words(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    if not minutes:
        return f"{rest} s"
    return f"{minutes} min" + (f" {rest} s" if rest else "")


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


# ---------- settings and planning ----------

def with_saved_time(settings: dict, mtime: float | None) -> dict:
    """A settings.json written by hand has no "updated": when the file changed stands in for it."""
    if settings["updated"] is None and mtime is not None:
        settings["updated"] = int(mtime)
    return settings


def load_settings(paths: Paths) -> dict:
    try:
        mtime = paths.settings.stat().st_mtime
    except OSError:
        mtime = None
    return with_saved_time(clean_settings(read_json(paths.settings) or {}), mtime)


def describe_settings(settings: dict) -> str:
    if settings["problem"]:
        return f"alarm off: {settings['problem']}"
    if not settings["enabled"]:
        return "alarm off"
    window = f"window {settings['window_minutes']} min" if settings["window_minutes"] else "no smart window"
    return f"alarm on for {time_words(settings['alarm_time'])}, {window}, fade {settings['fade_minutes']} min"


class Plan(NamedTuple):
    alarm_time: str
    alarm_unix: int
    window_minutes: int
    fade_minutes: int
    window_start: int
    latest_start: int


def next_alarm_unix(alarm_time: str, now: float, last_done: int | None = None, grace: bool = True) -> int:
    """Today's alarm_time, unless it already ran or has passed (by more than 10 minutes, with grace);
    else the next day's."""
    hour, minute = (int(part) for part in alarm_time.split(":"))
    day = datetime.fromtimestamp(now).date()
    late = BUZZ_GIVE_UP_SECONDS if grace else 0
    while True:
        alarm = int(datetime.combine(day, dtime(hour, minute)).timestamp())
        if now < alarm + late and alarm != last_done:
            return alarm
        day += timedelta(days=1)


def plan_alarm(settings: dict, now: float, last_done: int | None = None, grace: bool = True) -> Plan | None:
    if not settings["enabled"]:
        return None
    alarm = next_alarm_unix(settings["alarm_time"], now, last_done, grace)
    window, fade = settings["window_minutes"], settings["fade_minutes"]
    window_start, latest_start = wake_window(alarm, window, fade)
    return Plan(settings["alarm_time"], alarm, window, fade, window_start, latest_start)


def revivable(log: dict) -> bool:
    """True for the wake log of an alarm switched off before its buzz: switching it back on resumes it."""
    buzz = log.get("buzz") if isinstance(log.get("buzz"), dict) else {}
    return log.get("reason") == "disabled" and not buzz.get("stopped")


def upcoming_alarm(paths: Paths, settings: dict, now: float, last_done: int | None = None) -> Plan | None:
    """The next alarm to run, skipping one whose wake log says it already ran (e.g. before a restart).

    An alarm time up to 10 minutes past still runs late after a restart or reboot, but not when the
    settings were saved after it: a time that had already passed then means the next day's.
    """
    plan = plan_alarm(settings, now, last_done)
    if plan is None:
        return None
    log = read_json(paths.wake_log(night_date(plan.window_start)))
    started = bool(log) and log.get("alarm_unix") == plan.alarm_unix
    saved_after = settings["updated"] is not None and settings["updated"] >= plan.alarm_unix
    if plan.alarm_unix <= now and saved_after and not started:
        return plan_alarm(settings, now, last_done, grace=False)
    if started and log.get("finished") and not revivable(log):
        plan = plan_alarm(settings, now, plan.alarm_unix)
    return plan


def smart_window(plan: Plan) -> bool:
    return plan.window_minutes > 0 and plan.window_start < plan.latest_start


def timeline_words(plan: Plan) -> str:
    if smart_window(plan):
        return f"watching from {clock12(plan.window_start)}, fade by {clock12(plan.latest_start)} at the latest"
    return f"no smart window, fade from {clock12(plan.latest_start)}"


def fade_seconds(plan: Plan, now: int) -> int:
    return max(MIN_FADE_SECONDS, min(plan.fade_minutes * 60, plan.alarm_unix - now))


def new_wake_log(plan: Plan, date: str, tuning: dict) -> dict:
    return {
        "date": date,
        **plan._asdict(),
        "fade_start": None,
        "fade_end": None,
        "reason": None,
        "trigger": None,
        "trigger_t": None,
        "snooz": {"started": None, "finished": None, "ok": None, "returncode": None, "output": [], "attempts": 0},
        "buzz": {"first_sent": None, "resends": 0, "awake": None, "stopped": None, "watch_connected": None},
        "tuning": tuning,
        "finished": None,
    }


# ---------- tonight's watch data ----------

def csv_header(line: str) -> list[str] | None:
    columns = [c.strip() for c in line.strip().split(",")]
    return columns if "samples" in columns and "mov_sum" in columns else None


class NightFeed:
    """Tonight's watch CSV, read a few complete lines at a time as the logger appends them."""

    def __init__(self, paths: Paths, tuning: dict) -> None:
        self.paths = paths
        self.detector = Detector(tuning)
        self.path: Path | None = None
        self.offset = 0
        self.header: list[str] | None = None
        self.last_t: int | None = None

    def read_new(self, now: float) -> list[Row]:
        """Rows appended since the last call. The caller gives them to the detector."""
        path = self.paths.night_csv(night_date(now))
        if path != self.path:
            self.path, self.offset, self.header = path, 0, None
        try:
            with path.open("rb") as f:
                if os.fstat(f.fileno()).st_size < self.offset:
                    self.offset, self.header = 0, None  # the file was replaced
                f.seek(self.offset)
                chunk = f.read()
        except OSError:  # missing or unreadable: the latest-start fallback still runs
            return []
        complete = chunk[:chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next read
        self.offset += len(complete)
        rows = []
        for line in complete.decode("utf-8", errors="replace").splitlines():
            columns = csv_header(line)
            if columns:
                self.header = columns
            elif self.header and (row := parse_csv_line(line, self.header)):
                rows.append(row)
                self.last_t = row.t if self.last_t is None else max(self.last_t, row.t)
        return rows


# ---------- the watch (through watch_logger.py) ----------

class WatchStatus(NamedTuple):
    connected: bool
    last_row: int | None
    awake: int | None


def _unix_or_none(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def read_watch_status(path: Path, now: float) -> WatchStatus:
    data = read_json(path) or {}
    updated = _unix_or_none(data.get("updated"))
    fresh = updated is not None and now - updated <= STATUS_MAX_AGE_SECONDS
    return WatchStatus(data.get("connected") is True and fresh,
                       _unix_or_none(data.get("last_row")), _unix_or_none(data.get("awake")))


class WatchCommander:
    """Talks to the watch through the logger, which is the only process connected to it."""

    def __init__(self, paths: Paths, clock=time.time) -> None:
        self.paths = paths
        self.clock = clock

    def send(self, cmd: str) -> None:
        write_json_atomic(self.paths.cmd, {"cmd": cmd, "sent": int(self.clock())})

    def status(self) -> WatchStatus:
        return read_watch_status(self.paths.status, self.clock())


class DryRunWatch(WatchCommander):
    def send(self, cmd: str) -> None:
        _print(f"{stamp(self.clock())} [dry run] would ask the watch logger to send {cmd!r}")


# ---------- the SNOOZ ----------

def _last_lines(text) -> list[str]:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return [line.rstrip() for line in (text or "").splitlines() if line.strip()][-OUTPUT_LINES:]


def run_fade(command: list[str], timeout: int) -> tuple[int | None, list[str]]:
    """(return code or None, last output lines) of one snooz_ctl.py run."""
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", timeout=timeout, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except subprocess.TimeoutExpired as e:
        return None, _last_lines(e.output)[-(OUTPUT_LINES - 1):] + [f"timed out after {timeout} s"]
    except OSError as e:
        return None, [f"could not start snooz_ctl.py: {e}"]
    return done.returncode, _last_lines(done.stdout)


def fade_ok(returncode: int | None, output: list[str]) -> bool:
    # snooz_ctl.py exits 0 even when the SNOOZ rejects the command, so read what it printed too
    return returncode == 0 and not any(line.startswith("Command failed") for line in output)


class SnoozFader:
    """Runs `snooz_ctl.py fadeout SECONDS` in a background thread, one fade at a time."""

    def __init__(self, script: Path = SNOOZ_SCRIPT, python: str = sys.executable, clock=time.time) -> None:
        self.command = [str(python), str(script)]
        self.clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: dict | None = None

    def start(self, seconds: int) -> bool:
        """False if a fade is still running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._result = {"started": int(self.clock()), "finished": None, "ok": None, "returncode": None,
                            "output": []}
            self._thread = threading.Thread(target=self._run, args=(int(seconds),), daemon=True)
            self._thread.start()
        return True

    def result(self) -> dict | None:
        with self._lock:
            return None if self._result is None else {**self._result, "output": list(self._result["output"])}

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def _run(self, seconds: int) -> None:
        try:
            returncode, output = run_fade(self.command + ["fadeout", str(seconds)],
                                          seconds + FADE_TIMEOUT_SLACK_SECONDS)
        except Exception as e:  # never leave the result unfinished
            returncode, output = None, [f"error: {e}"]
        with self._lock:
            self._result.update(finished=int(self.clock()), ok=fade_ok(returncode, output),
                                returncode=returncode, output=output)


class DryRunSnooz:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._result: dict | None = None

    def start(self, seconds: int) -> bool:
        now = int(self.clock())
        _print(f"{stamp(now)} [dry run] would fade the SNOOZ out over {duration_words(seconds)}")
        self._result = {"started": now, "finished": now, "ok": True, "returncode": 0, "output": ["dry run"]}
        return True

    def result(self) -> dict | None:
        return self._result


# ---------- the alarm ----------

@dataclass
class Alarm:
    """One alarm instance, keyed by its alarm_unix, from IDLE to DONE."""
    plan: Plan
    date: str  # the night it belongs to, which names its wake log
    tuning: dict
    log: dict
    state: str = IDLE
    logging: bool = False  # the wake log is written from the moment the window opens
    last_sent: int | None = None
    warned_disconnected: bool = False
    cancelled: tuple[str, str | None] | None = None  # (state, reason) when switched off, to resume them


class WakeController:
    """Runs one alarm at a time; tick() is called every POLL_SECONDS.

    snooz needs start(seconds) -> bool and result() -> dict | None (see SnoozFader);
    watch needs send(cmd) and status() -> WatchStatus (see WatchCommander).
    """

    def __init__(self, paths: Paths, clock=time.time, snooz=None, watch=None, log=_print) -> None:
        self.paths = paths
        self.clock = clock
        self.snooz = snooz if snooz is not None else SnoozFader(clock=clock)
        self.watch = watch if watch is not None else WatchCommander(paths, clock)
        self.log = log
        self.settings = clean_settings({})
        self.alarm: Alarm | None = None
        self.last_done: int | None = None  # alarm_unix of the last alarm that reached DONE
        self.feed: NightFeed | None = None
        self._settings_key: tuple | None = ()  # matches no real key, so the first tick reads the file
        self._settings_error = False
        self._fade_owner: Alarm | None = None

    def tick(self) -> None:
        now = int(self.clock())
        if self._reload_settings(now) and self.alarm is not None and self.alarm.state != DONE:
            self._apply_settings(now)
        self._collect_fade(now)
        self._ensure_alarm(now)
        for _ in range(6):  # a late start can pass through several states in one tick
            alarm = self.alarm
            if alarm is None or alarm.state == DONE:
                break
            state = alarm.state
            self._step(now)
            if self.alarm is alarm and alarm.state == state:
                break
        self._ensure_alarm(now)

    def _say(self, now: int, text: str) -> None:
        self.log(f"{stamp(now)} {text}")

    # ----- settings -----

    def _reload_settings(self, now: int) -> bool:
        try:
            info = self.paths.settings.stat()
            key = (info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            key = None
        except OSError as e:
            if not self._settings_error:
                self._say(now, f"settings.json could not be read ({e}), keeping the previous settings")
            self._settings_error = True
            return False
        self._settings_error = False
        if key == self._settings_key:
            return False
        self._settings_key = key
        raw = {} if key is None else read_json(self.paths.settings)
        if raw is None:
            self._say(now, "settings.json could not be read, keeping the previous settings")
            return False
        self.settings = with_saved_time(clean_settings(raw), info.st_mtime if key else None)
        source = "settings" if key else "no settings.json yet, using the web app's defaults"
        self._say(now, f"{source}: {describe_settings(self.settings)}")
        return True

    def _apply_settings(self, now: int) -> None:
        alarm = self.alarm
        if not self.settings["enabled"]:
            if alarm.state != CANCELLED:
                self._cancel(now)
            return
        if alarm.state == CANCELLED:
            if now >= alarm.plan.alarm_unix + BUZZ_GIVE_UP_SECONDS:
                return  # over: _step closes it and _ensure_alarm plans the next one
            self._revive(now)
        if alarm.state == BUZZING:
            return  # the buzz has started: only switching the alarm off stops it
        plan = upcoming_alarm(self.paths, self.settings, now, self.last_done)
        tuning = self.settings["tuning"]
        if plan == alarm.plan and tuning == alarm.tuning:
            return
        if alarm.state == IDLE:
            self.alarm = None  # _ensure_alarm plans it again
        elif alarm.state == WINDOW:
            self._replan_window(alarm, plan, tuning, now)
        elif plan.alarm_unix != alarm.plan.alarm_unix:  # FADING: only the buzz can still move
            if plan.window_start <= now:
                alarm.plan = alarm.plan._replace(alarm_time=plan.alarm_time, alarm_unix=plan.alarm_unix)
                alarm.log.update(alarm_time=plan.alarm_time, alarm_unix=plan.alarm_unix)
                self._write_log(alarm)
                self._say(now, f"alarm moved: the watch buzzes at {clock12(plan.alarm_unix)}")
            else:
                self._say(now, f"alarm moved to {day_and_time(plan.alarm_unix)}: no buzz this time")
                self._finish(now)

    def _replan_window(self, alarm: Alarm, plan: Plan, tuning: dict, now: int) -> None:
        if plan.window_start > now:
            alarm.log["reason"] = "rescheduled"
            self._write_log(alarm)
            self.alarm = self.feed = None  # _ensure_alarm plans it again
            self._say(now, f"alarm moved to {day_and_time(plan.alarm_unix)}: closing this window")
            return
        tuning_changed = tuning != alarm.tuning
        alarm.plan, alarm.tuning = plan, tuning
        alarm.log.update(plan._asdict(), tuning=tuning)
        self._write_log(alarm)
        if not plan.window_minutes:
            self.feed = None
        elif tuning_changed or self.feed is None:
            self.feed = NightFeed(self.paths, tuning)  # re-reads tonight's rows with the new tuning
        self._say(now, f"settings changed in the window: alarm {clock12(plan.alarm_unix)}, {timeline_words(plan)}")

    def _cancel(self, now: int) -> None:
        alarm = self.alarm
        if alarm.state == IDLE:
            self.alarm = None
            return
        if alarm.state == BUZZING:  # the owner is up and has silenced it: final, even if switched back on
            if alarm.log["buzz"]["first_sent"]:
                self._send_stop(now)
            alarm.log["buzz"]["stopped"] = "disabled"
            alarm.log["reason"] = "disabled"
            self._say(now, "alarm switched off: buzz stopped")
            self._finish(now)
            return
        alarm.cancelled = (alarm.state, alarm.log["reason"])
        alarm.state = CANCELLED
        alarm.log.update(reason="disabled", finished=now)
        self._write_log(alarm)
        self.feed = None
        note = " (the SNOOZ fade already under way runs to the end)" if alarm.cancelled[0] == FADING else ""
        self._say(now, f"alarm switched off: cancelled{note}. Switching it back on before "
                       f"{clock12(alarm.plan.alarm_unix + BUZZ_GIVE_UP_SECONDS)} brings it back")

    def _revive(self, now: int) -> None:
        alarm = self.alarm
        alarm.state, reason = alarm.cancelled
        alarm.cancelled = None
        alarm.log.update(reason=reason, finished=None)
        if alarm.state == WINDOW and alarm.plan.window_minutes:
            self.feed = NightFeed(self.paths, alarm.tuning)  # re-reads tonight's rows from the start
        self._write_log(alarm)
        self._say(now, f"alarm switched back on: carrying on with the {clock12(alarm.plan.alarm_unix)} alarm")

    # ----- planning -----

    def _ensure_alarm(self, now: int) -> None:
        if self.alarm is not None and self.alarm.state != DONE:
            return
        plan = upcoming_alarm(self.paths, self.settings, now, self.last_done)
        if plan is None or (self.alarm is not None and plan.alarm_unix == self.alarm.plan.alarm_unix):
            return
        self.alarm = self._new_alarm(plan, now)

    def _new_alarm(self, plan: Plan, now: int) -> Alarm:
        date = night_date(plan.window_start)
        tuning = self.settings["tuning"]
        alarm = Alarm(plan, date, tuning, new_wake_log(plan, date, tuning))
        old = read_json(self.paths.wake_log(date))
        if old and old.get("alarm_unix") == plan.alarm_unix and self._resume(alarm, old, now):
            return alarm
        self._say(now, f"next alarm {day_and_time(plan.alarm_unix)}: {timeline_words(plan)}")
        return alarm

    def _resume(self, alarm: Alarm, old: dict, now: int) -> bool:
        """Carry on with an alarm whose fade or buzz started before wake.py restarted."""
        buzz_started = isinstance(old.get("buzz"), dict) and old["buzz"].get("first_sent")
        if not (buzz_started or old.get("fade_start")):
            return False
        for key, value in old.items():
            if isinstance(alarm.log.get(key), dict) and isinstance(value, dict):
                alarm.log[key] = {**alarm.log[key], **value}
            elif key in alarm.log:
                alarm.log[key] = value
        snooz = alarm.log["snooz"]
        cut_short = snooz["started"] and snooz["finished"] is None
        if cut_short:  # the restart also stopped snooz_ctl.py, leaving the SNOOZ part faded
            snooz.update(ok=False, output=["wake.py restarted before the fade finished"])
        if revivable(old):  # switched off during the fade, then back on; the log no longer says why it faded
            alarm.log.update(reason="light_sleep" if old.get("trigger") else "fallback", finished=None)
        alarm.state = BUZZING if buzz_started else FADING
        alarm.logging = True
        self._say(now, f"wake.py restarted during the {clock12(alarm.plan.alarm_unix)} alarm: carrying on")
        if (cut_short and alarm.state == FADING and snooz["attempts"] < FADE_ATTEMPTS
                and now < alarm.plan.alarm_unix and self._start_snooz(alarm, now)):
            self._say(now, f"starting the SNOOZ fade again, until {clock12(alarm.log['fade_end'])}")
        self._write_log(alarm)
        return True

    # ----- states -----

    def _step(self, now: int) -> None:
        alarm = self.alarm
        if alarm.state in (IDLE, WINDOW) and now >= alarm.plan.alarm_unix + BUZZ_GIVE_UP_SECONDS:
            self._skip_missed(now)
        elif alarm.state == IDLE and now >= alarm.plan.window_start:
            self._open_window(now)
        elif alarm.state == WINDOW:
            self._watch_window(now)
        elif alarm.state == FADING and now >= alarm.plan.alarm_unix:
            alarm.state = BUZZING
        elif alarm.state == BUZZING:
            self._buzz(now)
        elif alarm.state == CANCELLED and now >= alarm.plan.alarm_unix + BUZZ_GIVE_UP_SECONDS:
            alarm.state = DONE  # the wake log already says "disabled"
            self.last_done = alarm.plan.alarm_unix

    def _skip_missed(self, now: int) -> None:
        """The clock jumped past the whole alarm, e.g. NTP correcting the old time the Pi (no clock
        battery) booted with. Fading or buzzing now would happen at a random time of day."""
        alarm = self.alarm
        self._say(now, f"the clock jumped past the {day_and_time(alarm.plan.alarm_unix)} alarm: skipping it")
        if alarm.state == IDLE:
            self.alarm = None  # _ensure_alarm plans the next one
            return
        alarm.log["reason"] = "missed"
        self._finish(now)

    def _open_window(self, now: int) -> None:
        alarm = self.alarm
        alarm.state = WINDOW
        alarm.logging = True
        self._write_log(alarm)
        if alarm.plan.window_minutes:
            self.feed = NightFeed(self.paths, alarm.tuning)  # as simulate_wake, even for a zero-length window
        if smart_window(alarm.plan):
            self._say(now, f"window open: watching for light sleep until {clock12(alarm.plan.latest_start)}")

    def _watch_window(self, now: int) -> None:
        plan = self.alarm.plan
        if self.feed is not None:
            detector = self.feed.detector
            for row in self.feed.read_new(now):
                detector.add(row)
                if plan.window_start <= row.t <= plan.latest_start:
                    trigger = detector.arousal_now()
                    if trigger:
                        self._start_fade(now, "light_sleep", trigger, row.t)
                        return
        if now < plan.latest_start:
            return
        last_t = self.feed.last_t if self.feed is not None else None
        if plan.window_minutes and (last_t is None or last_t < now - NO_DATA_SECONDS):
            self._start_fade(now, "fallback (no watch data)")
        else:
            self._start_fade(now, "fallback")

    def _start_fade(self, now: int, reason: str, trigger: str | None = None, trigger_t: int | None = None) -> None:
        alarm = self.alarm
        seconds = fade_seconds(alarm.plan, now)
        alarm.state = FADING
        self.feed = None
        alarm.log.update(fade_start=now, fade_end=now + seconds, reason=reason, trigger=trigger,
                         trigger_t=trigger_t)
        if reason == "light_sleep":
            why = f"light sleep at {clock12(trigger_t)} ({trigger})"
        elif reason != "fallback":
            why = f"no watch data in the last {NO_DATA_SECONDS // 60} minutes"
        elif smart_window(alarm.plan):
            why = f"no sign of light sleep by {clock12(alarm.plan.latest_start)}"
        else:
            why = "no smart window"
        self._say(now, f"{why}: fading the SNOOZ out over {duration_words(seconds)}, "
                       f"until {clock12(now + seconds)}")
        if not self._start_snooz(alarm, now):
            alarm.log["snooz"].update(ok=False, output=["skipped: an earlier SNOOZ fade was still running"])
            self._say(now, "the SNOOZ is still busy with an earlier fade, so this one was skipped")
        self._write_log(alarm)

    def _start_snooz(self, alarm: Alarm, now: int) -> bool:
        """Start (or start again) the alarm's SNOOZ fade, timed to end by the alarm."""
        seconds = fade_seconds(alarm.plan, now)
        if not self.snooz.start(seconds):
            return False
        snooz = alarm.log["snooz"]
        snooz.update(started=now, finished=None, ok=None, returncode=None, output=[],
                     attempts=snooz["attempts"] + 1)
        alarm.log["fade_end"] = now + seconds
        self._fade_owner = alarm
        return True

    def _collect_fade(self, now: int) -> None:
        owner = self._fade_owner
        result = self.snooz.result() if owner is not None else None
        if not result or result.get("finished") is None:
            return
        owner.log["snooz"].update({key: result[key] for key in ("finished", "ok", "returncode", "output")})
        self._fade_owner = None
        self._write_log(owner)
        if result["ok"]:
            self._say(now, "SNOOZ fade finished")
            return
        detail = result["output"][-1] if result["output"] else f"exit code {result['returncode']}"
        # not after the alarm was switched off or replaced, and only while a fade can still end by the alarm
        again = (owner is self.alarm and owner.state == FADING and owner.log["snooz"]["attempts"] < FADE_ATTEMPTS
                 and now < owner.plan.alarm_unix - MIN_FADE_SECONDS)
        self._say(now, f"SNOOZ fade failed: {detail}" + (". Trying again" if again else ""))
        if again and self._start_snooz(owner, now):
            self._write_log(owner)

    def _buzz(self, now: int) -> None:
        alarm = self.alarm
        buzz = alarm.log["buzz"]
        status = self.watch.status()
        if status.awake is not None and status.awake >= alarm.plan.alarm_unix - AWAKE_SLACK_SECONDS:
            buzz.update(awake=status.awake, stopped="button")
            self._say(now, f"watch button pressed at {clock12(status.awake)}: alarm done")
            self._finish(now)
            return
        if now >= alarm.plan.alarm_unix + BUZZ_GIVE_UP_SECONDS:
            buzz["stopped"] = "timeout" if buzz["first_sent"] else "not connected"
            self._say(now, f"no button press after {BUZZ_GIVE_UP_SECONDS // 60} minutes: alarm done")
            if buzz["first_sent"] and status.connected:
                self._send_stop(now)  # a backstop: a late first load restarts the watch's own 10-minute timer
            self._finish(now)
            return
        if not status.connected:
            if not alarm.warned_disconnected:
                alarm.warned_disconnected = True
                if buzz["watch_connected"] is None:
                    buzz["watch_connected"] = False
                self._write_log(alarm)
                self._say(now, "watch not connected, relying on the watch's own alarm")
            return
        if alarm.last_sent is not None and now - alarm.last_sent < BUZZ_RESEND_SECONDS:
            return
        self.watch.send("alarm")
        alarm.last_sent = now
        buzz["watch_connected"] = True
        if buzz["first_sent"] is None:
            buzz["first_sent"] = now
            self._say(now, "alarm time: buzzing the watch until its button is pressed")
        else:
            buzz["resends"] += 1
            self._say(now, "no button press yet: asking the watch to buzz again")
        self._write_log(alarm)

    def _send_stop(self, now: int) -> None:
        try:
            self.watch.send("stop")
        except OSError as e:  # the alarm must still finish
            self._say(now, f"could not ask the watch to stop buzzing: {e}")

    def _finish(self, now: int) -> None:
        alarm = self.alarm
        alarm.state = DONE
        alarm.log["finished"] = now
        self._write_log(alarm)
        self.last_done = alarm.plan.alarm_unix
        self.feed = None

    def _write_log(self, alarm: Alarm) -> None:
        if not alarm.logging:
            return
        try:
            write_json_atomic(self.paths.wake_log(alarm.date), alarm.log)
        except OSError as e:  # the alarm itself must carry on
            self._say(int(self.clock()), f"could not write the wake log: {e}")


# ---------- command line ----------

def show_status(paths: Paths) -> int:
    now = time.time()
    settings = load_settings(paths)
    plan = upcoming_alarm(paths, settings, now)
    if plan is None:
        print(f"Smart alarm: off{': ' + settings['problem'] if settings['problem'] else ''}. "
              "Switch it on in the web app.")
    else:
        print(f"Next alarm: {day_and_time(plan.alarm_unix)} (the time to be awake by)")
        fade = (plan.alarm_unix - plan.latest_start) // 60  # window_minutes when that is the shorter
        if smart_window(plan):
            print(f"  {clock12(plan.window_start):>8}  starts watching the watch data for light sleep")
            print(f"  {clock12(plan.latest_start):>8}  starts the {fade}-minute SNOOZ fade if it saw none by then")
        else:
            why = "the window is no longer than the fade" if plan.window_minutes else "window 0"
            print(f"  {clock12(plan.latest_start):>8}  starts the {fade}-minute SNOOZ fade "
                  f"(no smart window: {why})")
        print(f"  {clock12(plan.alarm_unix):>8}  the SNOOZ is quiet and the watch buzzes until you press its "
              f"button, for up to {BUZZ_GIVE_UP_SECONDS // 60} minutes")
        log = read_json(paths.wake_log(night_date(plan.window_start)))
        if log and log.get("alarm_unix") == plan.alarm_unix and log.get("fade_start"):
            why = str(log.get("reason")).replace("_", " ") + (f": {log['trigger']}" if log.get("trigger") else "")
            print(f"Under way: the fade started at {clock12(log['fade_start'])} ({why})")
    status = read_watch_status(paths.status, now)
    if status.connected:
        last = f", last data {clock12(status.last_row)}" if status.last_row else ""
        print(f"Watch: connected{last}")
    else:
        print("Watch: not connected. Start watch_logger.py at bedtime for light-sleep detection and the buzz.")
    return 0


def show_simulation(paths: Paths, date: str) -> int:
    path = paths.night_csv(date)
    if not path.exists():
        print(f"No recording for the night of {date}: {path} not found.")
        return 1
    settings = load_settings(paths)
    if settings["alarm_time"] is None:
        print(f"Can't simulate: {settings['problem']}.")
        return 1
    rows = read_rows(path)
    window, fade = settings["window_minutes"], settings["fade_minutes"]
    alarm = alarm_unix_for_night(date, settings["alarm_time"], rows)
    result = simulate_wake(rows, alarm, window, fade, settings["tuning"])
    span = f", {clock12(rows[0].t)} to {clock12(rows[-1].t)}" if rows else ""
    print(f"Night of {date}: {len(rows)} watch rows{span}.")
    off = "" if settings["enabled"] else " The alarm is off right now; this uses its saved times."
    print(f"Alarm {clock12(alarm)}, window {window} min, fade {fade} min.{off}")
    if result["reason"] == "light_sleep":
        print(f"Light sleep seen at {clock12(result['trigger_t'])} ({result['trigger']}).")
    elif result["reason"] == "no_data":
        ended = f" (the recording ends at {clock12(rows[-1].t)})" if rows else ""
        print(f"No watch data in the window{ended}, so it would have used the latest start.")
    elif result["window_start"] < result["latest_start"]:
        print(f"No sign of light sleep between {clock12(result['window_start'])} and "
              f"{clock12(result['latest_start'])}.")
    else:
        print("No smart window.")
    print(f"The SNOOZ fade would have run from {clock12(result['fade_start'])} to {clock12(result['fade_end'])}, "
          f"and the watch would have buzzed at {clock12(alarm)}.")
    return 0


def send_test_buzz(paths: Paths) -> int:
    watch = WatchCommander(paths)
    if not watch.status().connected:
        print("watch-status.json says the watch is not connected (is watch_logger.py running?). Trying anyway.")
    watch.send("buzz")
    sent = read_json(paths.cmd)
    started = time.monotonic()
    while time.monotonic() - started < TEST_BUZZ_WAIT_SECONDS:
        time.sleep(0.5)
        if not paths.cmd.exists():
            print(f"The logger took the command after {time.monotonic() - started:.0f} s: "
                  "the watch should buzz once.")
            return 0
    if read_json(paths.cmd) == sent:
        paths.cmd.unlink(missing_ok=True)  # so it can't buzz later out of the blue
    print(f"The logger did not take the command within {TEST_BUZZ_WAIT_SECONDS} s, so it was withdrawn. "
          "Start watch_logger.py and check the watch is connected: tail ~/sleeptracker/data/logger.log")
    return 1


def run_test_fade(seconds: int) -> int:
    fader = SnoozFader()
    print(f"Fading the SNOOZ out over {duration_words(seconds)}...", flush=True)
    fader.start(seconds)
    fader.wait()
    result = fader.result()
    print("Done." if result["ok"] else f"Failed (exit code {result['returncode']}).")
    for line in result["output"]:
        print(f"  {line}")
    return 0 if result["ok"] else 1


def run_service(paths: Paths, dry_run: bool) -> int:
    if dry_run:
        paths = Paths.under(paths.base, Path(tempfile.gettempdir()) / "sleepwake-dry-run")
        controller = WakeController(paths, snooz=DryRunSnooz(), watch=DryRunWatch(paths))
        _print(f"Dry run: the SNOOZ and watch are replaced by printed messages; wake logs go to {paths.logs}")
    else:
        controller = WakeController(paths)
    _print(f"{stamp(time.time())} wake.py started, reading {paths.settings}")
    last_error = None
    try:
        while True:
            started = time.monotonic()
            try:
                controller.tick()
                last_error = None
            except Exception:  # keep the alarm loop alive; print each new error once
                error = traceback.format_exc()
                if error != last_error:
                    _print(f"{stamp(time.time())} error:\n{error}")
                last_error = error
            time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


def night_arg(text: str) -> str:
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2026-10-13") from None


def seconds_arg(text: str) -> int:
    try:
        seconds = int(text)
    except ValueError:
        seconds = 0
    if not 1 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("SECONDS must be a whole number from 1 to 3600")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smart alarm: SNOOZ fade at light sleep, then the watch buzz.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true",
                       help="show the next alarm and whether the watch is connected")
    group.add_argument("--simulate", metavar="DATE", type=night_arg,
                       help="what the alarm would have done on the night of DATE (the evening, YYYY-MM-DD)")
    group.add_argument("--test-buzz", action="store_true", help="ask the watch logger to buzz the watch once")
    group.add_argument("--test-fade", metavar="SECONDS", type=seconds_arg, help="fade the SNOOZ out now")
    group.add_argument("--dry-run", action="store_true", help="run the alarm loop without the SNOOZ or watch")
    args = parser.parse_args(argv)
    paths = Paths.under(BASE)
    if args.status:
        return show_status(paths)
    if args.simulate:
        return show_simulation(paths, args.simulate)
    if args.test_buzz:
        return send_test_buzz(paths)
    if args.test_fade:
        return run_test_fade(args.test_fade)
    return run_service(paths, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
