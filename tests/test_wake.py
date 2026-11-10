import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.make_test_night import generate  # noqa: E402
from analysis.sleep_analysis import DEFAULT_TUNING, simulate_wake  # noqa: E402
from wake.wake import (  # noqa: E402
    FADING, IDLE, WINDOW, NightFeed, Paths, SnoozFader, SunriseLight, WakeController, WatchCommander,
    WatchStatus, clean_settings, clock12, next_alarm_unix, night_date, plan_alarm, show_status,
)

DATE = "2026-10-12"
WINDOW_MINUTES = 20
FADE_MINUTES = 8
CSV_HEADER = "time,watch_unix,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery,pi_unix"
LOG_KEYS = {"date", "alarm_time", "alarm_unix", "window_minutes", "fade_minutes", "window_start", "latest_start",
            "fade_start", "fade_end", "reason", "trigger", "trigger_t", "snooz", "buzz", "light", "tuning",
            "finished"}
_nights = {}


def night(turnover_at=None):
    """(rows, events) of the same synthetic night, optionally with a planted turnover."""
    if turnover_at not in _nights:
        _nights[turnover_at] = generate(DATE, 1, "normal", turnover_at)
    return _nights[turnover_at]


def quiet_alarm() -> int:
    """A whole-minute alarm whose 20-minute window lies in deep sleep with nothing planned in it."""
    events = night()[1]
    busy = events["turnovers"] + [a for a, _ in events["awakenings"]]
    for start, end, stage in events["stages"]:
        if stage == "deep" and end - start >= 20 * 60 and not any(start - 300 <= t <= end for t in busy):
            window_start = start + 180 + (-(start + 180)) % 60
            return window_start + WINDOW_MINUTES * 60
    raise AssertionError("test night has no quiet deep sleep")


def hhmm(unix: int) -> str:
    return datetime.fromtimestamp(unix).strftime("%H:%M")


def csv_line(r) -> str:
    """One row in watch_logger.py's format."""
    def number(value, digits):
        return "" if value is None else f"{value:.{digits}f}"
    return ",".join([datetime.fromtimestamp(r.t).isoformat(timespec="seconds"), str(r.t), str(r.samples),
                     f"{r.mov_sum:.3f}", f"{r.mov_max:.3f}", number(r.ax, 2), number(r.ay, 2), number(r.az, 2),
                     number(r.hr, 0), number(r.hr_conf, 0), number(r.battery, 0), str(r.t + 1)])


class LockedPath(Path):
    """A path that exists but can't be read, e.g. owned by another user."""

    def stat(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    def open(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))


class FakeClock:
    def __init__(self, now):
        self.now = int(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSnooz:
    def __init__(self, clock):
        self.clock = clock
        self.fades = []  # (start time, seconds)
        self._result = None

    def start(self, seconds):
        if self._result is not None and self._result["finished"] is None:
            return False
        self.fades.append((self.clock(), seconds))
        self._result = {"started": self.clock(), "finished": None, "ok": None, "returncode": None, "output": []}
        return True

    def result(self):
        return None if self._result is None else dict(self._result)

    def finish(self, ok=True):
        self._result.update(finished=self.clock(), ok=ok, returncode=0 if ok else 1, output=["Done in 0:08:00"])


class FakeLight:
    """Stands in for SunriseLight: like sunrise.py it runs until its off time, unless stopped or made to exit."""

    def __init__(self, clock, fail_starts=False):
        self.clock = clock
        self.fail_starts = fail_starts
        self.starts = []  # (time, (start, full, off, brightness))
        self.stops = []  # (time, turn_off)
        self._result = None
        self._off = None

    def start(self, start, full, off, brightness):
        if self.running():
            return False
        self.starts.append((self.clock(), (start, full, off, brightness)))
        if self.fail_starts:
            self._result = {"running": False, "returncode": None, "output": ["could not start sunrise.py: nope"]}
            return False
        self._off = off
        self._result = {"running": True, "returncode": None, "output": ["waiting for the start"]}
        return True

    def result(self):
        if self._result is None:
            return None
        if self._result["running"] and self.clock() >= self._off:
            self._result.update(running=False, returncode=0, output=["off"])
        return dict(self._result)

    def running(self):
        result = self.result()
        return bool(result and result["running"])

    def stop(self, turn_off=True):
        self.stops.append((self.clock(), turn_off))
        if self._result is not None and self._result["running"]:
            self._result.update(running=False, returncode=0)

    def exit(self, returncode=2, line="could not open the LED strip"):
        self._result.update(running=False, returncode=returncode, output=[line])


class RaisingLight:
    def __init__(self, clock):
        pass

    def start(self, *args):
        raise RuntimeError("the strip is on fire")

    stop = result = start


class FakeWatch:
    def __init__(self, clock, connected=True):
        self.clock = clock
        self.connected = connected
        self.awake = None
        self.sent = []  # (cmd, time)

    def send(self, cmd):
        self.sent.append((cmd, self.clock()))

    def status(self):
        return WatchStatus(self.connected, None, self.awake)


class Night:
    """A temp ~/sleeptracker and a WakeController on a fake clock. Before each tick the rows whose
    time has passed are appended to the night's CSV, the way the logger does."""

    def __init__(self, test, rows, alarm, start, connected=True, light=None, **settings):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.paths = Paths.under(Path(tmp.name))
        self.paths.data.mkdir(parents=True)
        self.clock = FakeClock(start)
        self.snooz = FakeSnooz(self.clock)
        self.watch = FakeWatch(self.clock, connected)
        self.light = light(self.clock) if light else None  # light: a class taking the clock, e.g. FakeLight
        self.lines = []
        self.controller = WakeController(self.paths, self.clock, self.snooz, self.watch, self.lines.append,
                                         light=self.light)
        self.rows = list(rows)
        self.next_row = 0
        self.settings = {"enabled": True, "alarm_time": hhmm(alarm), "window_minutes": WINDOW_MINUTES,
                         "fade_minutes": FADE_MINUTES, "updated": 1792000000}
        self.version = 0
        self.write_settings(**settings)

    def write_settings(self, **changes):
        self.settings.update(changes)
        self.paths.settings.write_text(json.dumps(self.settings))
        self.version += 1
        mtime = (1_700_000_000 + self.version) * 1_000_000_000
        os.utime(self.paths.settings, ns=(mtime, mtime))  # a new mtime whatever the file system's resolution

    def append_due_rows(self):
        due = []
        while self.next_row < len(self.rows) and self.rows[self.next_row].t <= self.clock.now:
            due.append(self.rows[self.next_row])
            self.next_row += 1
        if not due:
            return
        path = self.paths.night_csv(DATE)
        new = not path.exists()
        with path.open("a", newline="") as f:
            if new:
                f.write(CSV_HEADER + "\n")
            f.writelines(csv_line(r) + "\n" for r in due)

    def step(self):
        self.append_due_rows()
        self.controller.tick()
        self.clock.advance(10)

    def run_until(self, t):
        """Tick every 10 s up to and including time t."""
        while self.clock.now <= t:
            self.step()

    def wake_log(self):
        return json.loads(self.paths.wake_log(DATE).read_text())

    def light_log(self):
        return self.wake_log()["light"]

    def restart(self, light=FakeLight):
        """wake.py restarted: systemd's SIGTERM also stopped sunrise.py, so a new light runner."""
        self.light = light(self.clock)
        self.controller = WakeController(self.paths, self.clock, self.snooz, self.watch, self.lines.append,
                                         light=self.light)

    def send_light_off(self, sent):
        self.paths.light_cmd.write_text(json.dumps({"cmd": "off", "sent": sent}))


class AlarmRunTests(unittest.TestCase):
    def setUp(self):
        self.alarm = quiet_alarm()
        self.window_start = self.alarm - WINDOW_MINUTES * 60
        self.latest_start = self.alarm - FADE_MINUTES * 60
        self.planted = self.window_start + 6 * 60 + 5

    def test_fade_starts_at_a_planted_turnover(self):
        rows = night(self.planted)[0]
        n = Night(self, rows, self.alarm, self.window_start - 300)
        n.run_until(self.latest_start + 60)
        log = n.wake_log()
        expected = simulate_wake(rows, self.alarm, WINDOW_MINUTES, FADE_MINUTES)
        self.assertEqual(log["reason"], "light_sleep", n.lines)
        # the live alarm and the web app's simulation agree on the trigger
        self.assertEqual((log["trigger"], log["trigger_t"]), (expected["trigger"], expected["trigger_t"]))
        self.assertTrue(0 <= log["fade_start"] - self.planted <= 20, log["fade_start"] - self.planted)
        self.assertEqual(n.snooz.fades, [(log["fade_start"], FADE_MINUTES * 60)])
        self.assertLessEqual(log["fade_end"], self.alarm)
        self.assertEqual(n.controller.alarm.state, FADING)

    def test_quiet_window_falls_back_at_the_latest_start(self):
        rows = night()[0]
        n = Night(self, rows, self.alarm, self.window_start - 300)
        n.run_until(self.latest_start + 60)
        log = n.wake_log()
        self.assertEqual(simulate_wake(rows, self.alarm, WINDOW_MINUTES, FADE_MINUTES)["reason"], "fallback")
        self.assertEqual((log["reason"], log["trigger"], log["trigger_t"]), ("fallback", None, None))
        self.assertEqual(log["fade_start"], self.latest_start)
        self.assertEqual(log["fade_end"], self.alarm)
        self.assertEqual(n.snooz.fades, [(self.latest_start, FADE_MINUTES * 60)])

    def test_no_window_fades_before_the_alarm_whatever_the_watch_shows(self):
        n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300, window_minutes=0)
        n.run_until(self.alarm - 10)
        self.assertEqual(n.snooz.fades, [(self.alarm - FADE_MINUTES * 60, FADE_MINUTES * 60)])
        self.assertEqual(n.wake_log()["reason"], "fallback")

    def test_no_watch_data(self):
        for connected in (False, True):
            with self.subTest(connected=connected):
                n = Night(self, [], self.alarm, self.window_start - 300, connected=connected)
                n.run_until(self.alarm + 11 * 60)
                log = n.wake_log()
                self.assertIn("no watch data", log["reason"])
                self.assertEqual(n.snooz.fades, [(self.latest_start, FADE_MINUTES * 60)])
                warnings = [line for line in n.lines if "not connected" in line]
                if connected:
                    self.assertEqual(n.watch.sent[0], ("alarm", self.alarm))
                    self.assertEqual(warnings, [])
                else:
                    self.assertEqual(n.watch.sent, [])
                    self.assertEqual(len(warnings), 1, n.lines)
                    self.assertEqual(log["buzz"]["resends"], 0)
                    self.assertIs(log["buzz"]["watch_connected"], False)
                    self.assertEqual(log["buzz"]["stopped"], "not connected")
                self.assertEqual(log["finished"], self.alarm + 600)

    def test_buzz_is_resent_until_the_button_is_pressed(self):
        n = Night(self, [], self.alarm, self.alarm - 60)
        n.run_until(self.alarm)
        self.assertEqual(n.snooz.fades, [(self.alarm - 60, 60)])  # a late start still ends the fade by the alarm
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])
        n.run_until(self.alarm + 130)
        self.assertEqual(n.watch.sent, [("alarm", self.alarm), ("alarm", self.alarm + 120)])
        n.watch.awake = self.alarm + 135
        n.run_until(self.alarm + 400)
        self.assertEqual(len(n.watch.sent), 2)
        log = n.wake_log()
        self.assertEqual(log["buzz"], {"first_sent": self.alarm, "resends": 1, "awake": self.alarm + 135,
                                       "stopped": "button", "watch_connected": True})
        self.assertEqual(log["finished"], self.alarm + 140)

    def test_buzz_stops_after_ten_minutes(self):
        n = Night(self, [], self.alarm, self.alarm - 10)
        n.watch.awake = self.alarm - 3600  # an old button press, e.g. from a test buzz, doesn't count
        n.run_until(self.alarm + 900)
        self.assertEqual(n.snooz.fades, [(self.alarm - 10, 30)])  # never shorter than 30 s
        # the stop is a backstop for the watch's own 10-minute timer
        self.assertEqual([(cmd, t - self.alarm) for cmd, t in n.watch.sent],
                         [("alarm", 0), ("alarm", 120), ("alarm", 240), ("alarm", 360), ("alarm", 480),
                          ("stop", 600)])
        log = n.wake_log()
        self.assertEqual((log["buzz"]["resends"], log["buzz"]["stopped"]), (4, "timeout"))
        self.assertEqual(log["finished"], self.alarm + 600)

    def test_no_stop_at_the_timeout_when_the_watch_dropped_out(self):
        n = Night(self, [], self.alarm, self.alarm - 10)
        n.run_until(self.alarm + 130)
        n.watch.connected = False
        n.run_until(self.alarm + 900)
        self.assertEqual(n.watch.sent, [("alarm", self.alarm), ("alarm", self.alarm + 120)])
        self.assertEqual(n.wake_log()["buzz"]["stopped"], "timeout")

    def test_switching_off_in_the_window_cancels(self):
        n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300)
        n.run_until(self.window_start + 120)
        self.assertEqual(n.controller.alarm.state, WINDOW)
        n.write_settings(enabled=False)
        n.run_until(self.alarm + 900)
        self.assertEqual(n.snooz.fades, [])
        self.assertEqual(n.watch.sent, [])
        log = n.wake_log()
        self.assertEqual(log["reason"], "disabled")
        self.assertIsNone(log["fade_start"])
        self.assertEqual(log["finished"], self.window_start + 130)

    def restart(self, n):
        n.controller = WakeController(n.paths, n.clock, n.snooz, n.watch, n.lines.append)

    def test_switching_off_and_back_on_in_the_window_keeps_this_mornings_alarm(self):
        rows = night(self.planted)[0]
        expected = simulate_wake(rows, self.alarm, WINDOW_MINUTES, FADE_MINUTES)
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                n = Night(self, rows, self.alarm, self.window_start - 300)
                n.run_until(self.window_start + 120)
                n.write_settings(enabled=False)
                n.step()
                self.assertEqual(n.wake_log()["reason"], "disabled")
                if restarted:
                    self.restart(n)
                    n.step()
                n.write_settings(enabled=True)
                n.run_until(self.alarm)
                log = n.wake_log()
                self.assertEqual((log["reason"], log["trigger"], log["trigger_t"]),
                                 ("light_sleep", expected["trigger"], expected["trigger_t"]), n.lines)
                self.assertEqual(n.snooz.fades, [(log["fade_start"], FADE_MINUTES * 60)])
                self.assertEqual(n.watch.sent, [("alarm", self.alarm)])
                self.assertIsNone(log["finished"])

    def test_switching_off_and_back_on_during_the_fade_still_buzzes(self):
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300)
                n.run_until(self.latest_start)
                fade_start = n.wake_log()["fade_start"]
                n.write_settings(enabled=False)
                n.step()
                if restarted:
                    self.restart(n)
                    n.step()
                n.write_settings(enabled=True)
                n.run_until(self.alarm)
                self.assertEqual(n.snooz.fades, [(fade_start, FADE_MINUTES * 60)])  # no second fade
                self.assertEqual(n.watch.sent, [("alarm", self.alarm)])
                log = n.wake_log()
                self.assertEqual((log["reason"], log["fade_start"], log["finished"]),
                                 ("light_sleep", fade_start, None))

    def test_switching_off_during_the_buzz_is_final(self):
        n = Night(self, [], self.alarm, self.alarm - 60)
        n.run_until(self.alarm + 30)
        n.write_settings(enabled=False)
        n.step()
        n.write_settings(enabled=True)
        n.run_until(self.alarm + 300)
        self.assertEqual(n.watch.sent, [("alarm", self.alarm), ("stop", self.alarm + 40)])
        log = n.wake_log()
        self.assertEqual((log["reason"], log["buzz"]["stopped"], log["finished"]),
                         ("disabled", "disabled", self.alarm + 40))
        tomorrow = int((datetime.fromtimestamp(self.alarm) + timedelta(days=1)).timestamp())
        self.assertEqual(n.controller.alarm.plan.alarm_unix, tomorrow)

    def test_switched_off_alarm_is_over_ten_minutes_after_its_time(self):
        n = Night(self, [], self.alarm, self.window_start - 300)
        n.run_until(self.window_start + 60)
        n.write_settings(enabled=False)
        n.run_until(self.alarm + 610)
        n.write_settings(enabled=True)
        n.step()
        tomorrow = int((datetime.fromtimestamp(self.alarm) + timedelta(days=1)).timestamp())
        self.assertEqual((n.controller.alarm.state, n.controller.alarm.plan.alarm_unix), (IDLE, tomorrow))
        self.assertEqual((n.snooz.fades, n.watch.sent), ([], []))
        self.assertEqual(n.wake_log()["reason"], "disabled")

    def test_unreadable_settings_and_csv_still_fall_back_and_buzz(self):
        n = Night(self, night()[0], self.alarm, self.window_start - 300)
        n.step()
        n.controller.paths = replace(n.paths, settings=LockedPath(n.paths.settings), data=LockedPath(n.paths.data))
        n.run_until(self.alarm + 10)
        log = n.wake_log()
        self.assertEqual(log["reason"], "fallback (no watch data)")
        self.assertEqual(n.snooz.fades, [(self.latest_start, FADE_MINUTES * 60)])
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])
        self.assertEqual(len([line for line in n.lines if "could not be read" in line]), 1, n.lines)

    def test_window_shorter_than_the_fade(self):
        n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300, window_minutes=5)
        n.run_until(self.alarm - 10)
        self.assertEqual(n.snooz.fades, [(self.alarm - 300, 300)])
        self.assertFalse([line for line in n.lines if "window open" in line], n.lines)

    def test_next_alarm_after_done_is_the_following_day(self):
        n = Night(self, [], self.alarm, self.alarm - 60)
        n.run_until(self.alarm)
        n.watch.awake = self.alarm + 5
        n.run_until(self.alarm + 20)
        self.assertEqual(n.wake_log()["finished"], self.alarm + 10)
        tomorrow = int((datetime.fromtimestamp(self.alarm) + timedelta(days=1)).timestamp())
        self.assertEqual(n.controller.alarm.state, IDLE)
        self.assertEqual(n.controller.alarm.plan.alarm_unix, tomorrow)
        # a restart a few minutes later must not run this morning's alarm again
        restarted = WakeController(n.paths, n.clock, FakeSnooz(n.clock), FakeWatch(n.clock), n.lines.append)
        n.clock.advance(120)
        restarted.tick()
        self.assertEqual(restarted.alarm.plan.alarm_unix, tomorrow)

    def test_restart_during_the_fade_carries_on(self):
        n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300)
        n.run_until(self.latest_start + 60)
        fade_start = n.wake_log()["fade_start"]
        snooz, watch = FakeSnooz(n.clock), FakeWatch(n.clock)
        restarted = WakeController(n.paths, n.clock, snooz, watch, n.lines.append)
        restarted_at = n.clock.now
        restarted.tick()
        self.assertEqual(restarted.alarm.state, FADING)
        while n.clock.now <= self.alarm:
            n.clock.advance(10)
            restarted.tick()
        # the restart also stopped snooz_ctl.py, so the fade starts again, still ending by the alarm
        self.assertEqual(snooz.fades, [(restarted_at, self.alarm - restarted_at)])
        self.assertEqual(watch.sent, [("alarm", self.alarm)])
        log = n.wake_log()
        self.assertEqual((log["reason"], log["fade_start"], log["fade_end"]), ("light_sleep", fade_start, self.alarm))
        self.assertEqual((log["snooz"]["started"], log["snooz"]["attempts"]), (restarted_at, 2))

    def test_failed_fade_is_tried_again(self):
        n = Night(self, [], self.alarm, self.window_start - 300)
        n.run_until(self.latest_start)
        for _ in range(3):  # e.g. snooz_ctl.py's scan missed the SNOOZ
            n.snooz.finish(ok=False)
            n.step()
        ls = self.latest_start
        self.assertEqual(n.snooz.fades, [(ls, 480), (ls + 10, 470), (ls + 20, 460)])  # three tries in all
        self.assertEqual(len([line for line in n.lines if "Trying again" in line]), 2, n.lines)
        n.run_until(self.alarm)
        log = n.wake_log()
        self.assertEqual((log["snooz"]["ok"], log["snooz"]["attempts"], log["fade_start"]), (False, 3, ls))
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])

    def test_no_fade_retry_once_switched_off_or_too_close_to_the_alarm(self):
        for case in ("switched off", "too close"):
            with self.subTest(case=case):
                n = Night(self, [], self.alarm, self.window_start - 300)
                n.run_until(self.latest_start)
                if case == "switched off":
                    n.write_settings(enabled=False)
                    n.step()
                else:
                    n.run_until(self.alarm - 20)
                n.snooz.finish(ok=False)
                n.step()
                self.assertEqual(n.snooz.fades, [(self.latest_start, FADE_MINUTES * 60)])

    def tomorrow(self, unix):
        return int((datetime.fromtimestamp(unix) + timedelta(days=1)).timestamp())

    def test_clock_jump_past_the_alarm_skips_it(self):
        # No clock battery: the Pi can boot on an old time, plan that alarm, then NTP moves the clock on
        for in_window in (False, True):
            with self.subTest(in_window=in_window):
                start = self.window_start + 60 if in_window else self.window_start - 300
                n = Night(self, [], self.alarm, start)
                n.step()
                self.assertEqual(n.controller.alarm.state, WINDOW if in_window else IDLE)
                n.clock.now = self.alarm + 12 * 3600
                n.step()
                self.assertEqual((n.snooz.fades, n.watch.sent), ([], []))
                self.assertEqual((n.controller.alarm.state, n.controller.alarm.plan.alarm_unix),
                                 (IDLE, self.tomorrow(self.alarm)))
                if in_window:
                    self.assertEqual(n.wake_log()["reason"], "missed")
                else:
                    self.assertFalse(n.paths.wake_log(DATE).exists())

    def test_time_that_had_passed_when_saved_is_for_tomorrow(self):
        # switched on 5 minutes after the alarm time, after being off all night
        n = Night(self, [], self.alarm, self.alarm - 600, enabled=False)
        n.run_until(self.alarm + 300)
        n.write_settings(enabled=True, updated=n.clock.now)
        n.step()
        self.assertEqual(n.controller.alarm.plan.alarm_unix, self.tomorrow(self.alarm))
        self.assertEqual((n.snooz.fades, n.watch.sent), ([], []))
        self.assertFalse(n.paths.wake_log(DATE).exists())
        # an earlier time saved just after this morning's alarm ran must not run now or replace its log
        n = Night(self, [], self.alarm, self.alarm - 60)
        n.run_until(self.alarm)
        n.watch.awake = self.alarm + 5
        n.run_until(self.alarm + 180)
        ran = n.wake_log()
        n.write_settings(alarm_time=hhmm(self.alarm - 300), updated=n.clock.now)
        n.step()
        self.assertEqual(n.controller.alarm.plan.alarm_unix, self.tomorrow(self.alarm - 300))
        self.assertEqual((len(n.snooz.fades), n.watch.sent), (1, [("alarm", self.alarm)]))
        self.assertEqual(n.wake_log(), ran)

    def test_alarm_saved_before_its_time_still_runs_late_after_a_reboot(self):
        n = Night(self, [], self.alarm, self.alarm + 300, updated=self.alarm - 3600)
        n.step()
        self.assertEqual(n.snooz.fades, [(self.alarm + 300, 30)])
        self.assertEqual(n.watch.sent, [("alarm", self.alarm + 300)])

    def test_switching_back_on_just_after_the_alarm_time_brings_it_back(self):
        n = Night(self, [], self.alarm, self.window_start - 300)
        n.run_until(self.window_start + 60)
        n.write_settings(enabled=False, updated=n.clock.now)
        n.run_until(self.alarm + 120)
        n.write_settings(enabled=True, updated=n.clock.now)
        n.step()
        self.assertEqual(n.snooz.fades, [(self.alarm + 130, 30)])
        self.assertEqual(n.watch.sent, [("alarm", self.alarm + 130)])

    def test_settings_change_is_picked_up_on_the_next_tick(self):
        n = Night(self, night()[0], self.alarm, self.window_start - 600)
        n.step()
        self.assertEqual(n.controller.alarm.plan.alarm_unix, self.alarm)
        n.write_settings(alarm_time=hhmm(self.alarm + 300))
        n.step()
        self.assertEqual(n.controller.alarm.plan.alarm_unix, self.alarm + 300)
        n.write_settings(alarm_time=hhmm(self.alarm))
        n.run_until(self.window_start + 60)
        self.assertEqual(n.controller.alarm.state, WINDOW)
        # inside the window a longer fade moves the latest start, which has not passed yet
        n.write_settings(fade_minutes=12)
        n.step()
        self.assertEqual(n.controller.alarm.plan.latest_start, self.alarm - 12 * 60)
        n.run_until(self.alarm - 10)
        self.assertEqual(n.snooz.fades, [(self.alarm - 12 * 60, 12 * 60)])

    def test_wake_log_after_a_full_run(self):
        n = Night(self, night(self.planted)[0], self.alarm, self.window_start - 300)
        n.run_until(self.latest_start)
        n.snooz.finish()
        n.run_until(self.alarm + 30)
        n.watch.awake = self.alarm + 35
        n.run_until(self.alarm + 60)
        log = n.wake_log()
        self.assertEqual(set(log), LOG_KEYS)
        self.assertEqual((log["date"], log["alarm_time"], log["alarm_unix"]), (DATE, hhmm(self.alarm), self.alarm))
        self.assertEqual((log["window_minutes"], log["fade_minutes"]), (WINDOW_MINUTES, FADE_MINUTES))
        self.assertEqual((log["window_start"], log["latest_start"]), (self.window_start, self.latest_start))
        self.assertEqual(log["reason"], "light_sleep")
        self.assertTrue(log["trigger"])
        self.assertTrue(self.window_start <= log["trigger_t"] <= log["fade_start"] < log["fade_end"] <= self.alarm)
        snooz = log["snooz"]
        self.assertEqual(set(snooz), {"started", "finished", "ok", "returncode", "output", "attempts"})
        self.assertEqual(snooz["started"], log["fade_start"])
        self.assertTrue(snooz["ok"])
        self.assertEqual(snooz["attempts"], 1)
        self.assertTrue(log["fade_start"] < snooz["finished"] <= self.alarm)
        self.assertEqual(log["buzz"], {"first_sent": self.alarm, "resends": 0, "awake": self.alarm + 35,
                                       "stopped": "button", "watch_connected": True})
        self.assertEqual(log["tuning"], DEFAULT_TUNING)
        self.assertEqual(log["finished"], self.alarm + 40)


class SunriseTests(unittest.TestCase):
    """The light ramps over the 30 minutes before the alarm, then holds for an hour, alongside the alarm."""

    def setUp(self):
        self.alarm = int(datetime(2026, 10, 13, 6, 30).timestamp())  # in the night of DATE
        self.start, self.off = self.alarm - 1800, self.alarm + 3600
        self.args = (self.start, self.alarm, self.off, 100)

    def night(self, start_offset, light=FakeLight, **settings):
        return Night(self, [], self.alarm, self.alarm + start_offset, light=light, **settings)

    def wake_up(self, n, awake_offset=5):
        """Run to the alarm and press the watch button: the alarm is DONE but the light holds."""
        n.run_until(self.alarm)
        n.watch.awake = self.alarm + awake_offset
        n.run_until(self.alarm + 20)
        self.assertEqual(n.wake_log()["finished"], self.alarm + 10)

    def test_starts_half_an_hour_before_the_alarm_whatever_the_window(self):
        for window in (0, 10, 20, 45):
            with self.subTest(window=window):
                n = self.night(-2400, window_minutes=window)
                n.run_until(self.start - 10)
                self.assertEqual(n.light.starts, [])
                n.step()
                self.assertEqual(n.light.starts, [(self.start, self.args)])
                light = n.light_log()  # the wake log exists from then, even before the window opens
                self.assertEqual((light["started"], light["finished"], light["attempts"]), (self.start, None, 1))
                self.assertEqual((light["start"], light["full"], light["off_at"], light["brightness"]), self.args)

    def test_holds_past_the_button_press_until_an_hour_after_the_alarm(self):
        n = self.night(-2400)
        self.wake_up(n)
        tomorrow = int((datetime.fromtimestamp(self.alarm) + timedelta(days=1)).timestamp())
        self.assertEqual(n.controller.alarm.plan.alarm_unix, tomorrow)
        n.run_until(self.off - 20)
        self.assertTrue(n.light.running())
        self.assertEqual((len(n.light.starts), n.light.stops), (1, []))
        n.step()
        self.assertIsNone(n.light_log()["finished"])
        n.step()  # sunrise.py exits by itself at the off time
        log = n.wake_log()
        self.assertEqual(set(log), LOG_KEYS)
        self.assertEqual(log["light"], {"start": self.start, "full": self.alarm, "off_at": self.off, "brightness": 100,
                                        "started": self.start, "finished": self.off, "stopped": "timeout", "ok": True,
                                        "returncode": 0, "output": ["off"], "attempts": 1})
        self.assertEqual((log["finished"], log["buzz"]["stopped"]), (self.alarm + 10, "button"))
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])
        n.run_until(self.off + 600)
        self.assertEqual(len(n.light.starts), 1)

    def test_web_app_button_turns_it_off_for_this_alarm(self):
        n = self.night(-2400)
        n.run_until(self.alarm + 300)
        n.send_light_off(n.clock.now - 30)
        n.step()
        self.assertFalse(n.paths.light_cmd.exists())
        self.assertEqual(n.light.stops, [(self.alarm + 310, True)])
        light = n.light_log()
        self.assertEqual((light["stopped"], light["finished"], light["ok"]), ("web", self.alarm + 310, True))
        n.write_settings(sunrise_brightness=50)  # nothing brings it back for this alarm, not even a restart
        n.step()
        n.restart()
        n.run_until(self.off + 60)
        self.assertEqual(n.light.starts, [])
        self.assertEqual(n.light_log()["stopped"], "web")

    def test_stale_web_command_is_ignored_and_deleted(self):
        n = self.night(-2400)
        n.run_until(self.alarm - 600)
        n.send_light_off(n.clock.now - 300)
        n.step()
        self.assertFalse(n.paths.light_cmd.exists())
        self.assertTrue(n.light.running())
        self.assertEqual((n.light.stops, n.light_log()["stopped"]), ([], None))
        self.assertEqual(len([line for line in n.lines if "ignored a light-off command" in line]), 1, n.lines)

    def test_odd_web_commands_are_ignored_and_deleted(self):
        for sent in (float("nan"), float("inf"), float("-inf"), "soon", None, True):
            with self.subTest(sent=sent):
                n = self.night(-600)
                n.step()
                n.send_light_off(sent)  # json.dumps writes NaN and Infinity, and json.loads reads them back
                n.step()
                self.assertFalse(n.paths.light_cmd.exists())
                self.assertTrue(n.light.running())
                self.assertEqual((n.light.stops, n.light_log()["stopped"]), ([], None))
                self.assertFalse([line for line in n.lines if "error" in line], n.lines)

    def test_web_command_from_the_future_is_ignored_unless_close(self):
        for ahead, stopped in ((300, None), (60, "web")):  # e.g. the phone's clock a little fast
            with self.subTest(ahead=ahead):
                n = self.night(-600)
                n.step()
                n.send_light_off(n.clock.now + ahead)
                n.step()
                self.assertFalse(n.paths.light_cmd.exists())
                self.assertEqual(n.light_log()["stopped"], stopped)

    def test_web_command_with_no_light_on_is_just_deleted(self):
        n = self.night(-7200)
        n.send_light_off(n.clock.now)
        n.step()
        self.assertFalse(n.paths.light_cmd.exists())
        n.run_until(self.start)
        self.assertEqual(n.light.starts, [(self.start, self.args)])

    def test_switching_the_alarm_off_turns_it_off_and_back_on_restarts_it(self):
        for when, offset, window in (("ramp before the window", -1500, 10), ("ramp in the window", -900, 20),
                                     ("hold after the button", 1200, 20)):
            with self.subTest(when=when):
                n = self.night(-2400, window_minutes=window)
                if offset > 0:
                    self.wake_up(n)
                n.run_until(self.alarm + offset)
                n.write_settings(enabled=False)
                n.step()
                self.assertEqual(n.light.stops, [(self.alarm + offset + 10, True)])
                self.assertFalse(n.light.running())
                self.assertEqual(n.light_log()["stopped"], "disabled")
                n.run_until(self.alarm + offset + 120)
                self.assertEqual(len(n.light.starts), 1)
                n.write_settings(enabled=True)
                n.step()
                back = self.alarm + offset + 130
                self.assertEqual(n.light.starts[1], (back, self.args), n.lines)
                light = n.light_log()
                self.assertEqual((light["started"], light["finished"], light["stopped"], light["attempts"]),
                                 (back, None, None, 2))

    def test_sunrise_switch_off_and_on(self):
        n = self.night(-2400)
        n.run_until(self.alarm - 600)
        n.write_settings(sunrise_enabled=False)
        n.step()
        self.assertEqual(n.light_log()["stopped"], "setting")
        self.assertFalse(n.light.running())
        n.write_settings(sunrise_enabled=True)
        n.step()
        self.assertEqual(n.light.starts, [(self.start, self.args), (self.alarm - 580, self.args)])
        n.run_until(self.alarm)
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])

    def test_switched_off_before_it_starts_it_never_does(self):
        n = self.night(-2400, sunrise_enabled=False)
        n.run_until(self.off + 60)
        self.assertEqual(n.light.starts, [])
        self.assertIsNone(n.light_log()["started"])

    def test_brightness_change_restarts_it_with_the_new_brightness(self):
        n = self.night(-2400)
        n.run_until(self.alarm - 600)
        n.write_settings(sunrise_brightness=40)
        n.step()
        self.assertEqual(n.light.stops, [(self.alarm - 590, False)])  # no `sunrise.py off` in between
        self.assertEqual(n.light.starts[-1], (self.alarm - 590, (self.start, self.alarm, self.off, 40)))
        self.assertTrue(n.light.running())
        light = n.light_log()
        self.assertEqual((light["brightness"], light["started"], light["finished"], light["attempts"]),
                         (40, self.start, None, 2))

    def test_failures_are_retried_then_given_up(self):
        n = self.night(-2400, window_minutes=WINDOW_MINUTES)
        n.run_until(self.start)
        for _ in range(3):
            n.light.exit(2, "no LED strip found")
            n.step()
        self.assertEqual([t for t, _ in n.light.starts], [self.start, self.start + 10, self.start + 20])
        light = n.light_log()
        self.assertEqual((light["stopped"], light["ok"], light["returncode"], light["output"], light["attempts"]),
                         ("failed", False, 2, ["no LED strip found"], 3))
        self.assertEqual(len([line for line in n.lines if "Starting it again" in line]), 2, n.lines)
        n.run_until(self.alarm + 30)
        self.assertEqual(len(n.light.starts), 3)
        latest = self.alarm - FADE_MINUTES * 60
        self.assertEqual(n.snooz.fades, [(latest, FADE_MINUTES * 60)])
        self.assertEqual(n.watch.sent, [("alarm", self.alarm)])

    def test_restart_turns_it_back_on(self):
        for when, offset in (("ramp, window", -900), ("ramp, fade", -300), ("hold, after the button", 1200)):
            with self.subTest(when=when):
                n = self.night(-2400)
                if offset > 0:
                    self.wake_up(n)
                n.run_until(self.alarm + offset)
                n.restart()
                n.step()
                self.assertEqual(n.light.starts, [(self.alarm + offset + 10, self.args)], n.lines)
                light = n.light_log()
                self.assertEqual((light["started"], light["finished"], light["attempts"]), (self.start, None, 2))
                n.run_until(self.off)
                self.assertEqual((n.light_log()["stopped"], n.light_log()["finished"]), ("timeout", self.off))
                self.assertEqual(n.watch.sent[0], ("alarm", self.alarm))
                self.assertEqual(n.wake_log()["buzz"]["stopped"], "button" if offset > 0 else "timeout")

    def test_switched_off_then_restarted_in_the_hold_switching_on_brings_it_back(self):
        for setting, reason in (("enabled", "disabled"), ("sunrise_enabled", "setting")):
            with self.subTest(setting=setting):
                n = self.night(-2400)
                self.wake_up(n)
                n.run_until(self.alarm + 1200)
                n.write_settings(**{setting: False})
                n.step()
                self.assertEqual(n.light_log()["stopped"], reason)
                n.restart()
                n.run_until(self.alarm + 1300)
                self.assertEqual(n.light.starts, [])
                n.write_settings(**{setting: True})
                n.step()
                self.assertEqual(n.light.starts, [(self.alarm + 1310, self.args)], n.lines)
                light = n.light_log()
                self.assertEqual((light["started"], light["finished"], light["stopped"]),
                                 (self.alarm + 1310, None, None))
                n.run_until(self.off)
                self.assertEqual(n.light_log()["stopped"], "timeout")

    def test_web_or_failed_stays_off_after_a_restart_in_the_hold(self):
        for final in ("web", "failed"):
            with self.subTest(final=final):
                n = self.night(-2400)
                self.wake_up(n)
                n.run_until(self.alarm + 1200)
                if final == "web":
                    n.send_light_off(n.clock.now)
                    n.step()
                else:
                    for _ in range(3):
                        n.light.exit()
                        n.step()
                self.assertEqual(n.light_log()["stopped"], final)
                n.restart()
                n.write_settings(sunrise_brightness=50)
                n.run_until(self.off + 60)
                self.assertEqual(n.light.starts, [])

    def test_restart_in_the_hold_then_tomorrows_alarm_replanned(self):
        n = self.night(-2400)
        self.wake_up(n)
        n.run_until(self.alarm + 1200)
        n.restart()
        n.step()
        self.assertTrue(n.light.running())
        n.write_settings(window_minutes=10)  # tomorrow's IDLE alarm is planned again
        n.step()
        n.write_settings(sunrise_brightness=70)
        n.run_until(self.off)
        self.assertEqual(n.light.starts, [(self.alarm + 1210, self.args),
                                          (self.alarm + 1230, (self.start, self.alarm, self.off, 70))])
        self.assertEqual(n.light.stops, [(self.alarm + 1230, False)])
        light = n.light_log()
        self.assertEqual((light["stopped"], light["finished"], light["brightness"]), ("timeout", self.off, 70))
        self.assertEqual(n.wake_log()["alarm_unix"], self.alarm)

    def test_restart_after_it_finished_leaves_it_off(self):
        n = self.night(-2400)
        self.wake_up(n)
        n.run_until(self.off)
        n.restart()
        n.run_until(self.off + 60)
        self.assertEqual(n.light.starts, [])

    def test_alarm_moved_turns_it_off_and_the_new_alarm_has_its_own(self):
        later = self.alarm + 2 * 3600
        for when, offset, window in (("before the window", -1500, 10), ("in the fade", -300, 20)):
            with self.subTest(when=when):
                n = self.night(-2400, window_minutes=window)
                n.run_until(self.alarm + offset)
                n.write_settings(alarm_time=hhmm(later))
                n.step()
                self.assertEqual(n.light.stops, [(self.alarm + offset + 10, True)], n.lines)
                self.assertEqual(n.light_log()["stopped"], "rescheduled")
                n.run_until(later - 1800)
                self.assertEqual(n.light.starts[-1], (later - 1800, (later - 1800, later, later + 3600, 100)))
                self.assertEqual(n.light_log()["started"], later - 1800)

    def test_moved_alarm_whose_window_is_open_keeps_its_own_log(self):
        # On for this alarm; 20 minutes before it the alarm moves 30 minutes later with a 60-minute
        # window, which is open already, so the new alarm writes the same night's file at once
        later = self.alarm + 1800
        n = self.night(-2400, window_minutes=10)
        n.run_until(self.alarm - 1210)
        self.assertEqual(n.controller.alarm.state, IDLE)
        n.write_settings(alarm_time=hhmm(later), window_minutes=60)
        n.step()
        self.assertEqual(n.light.stops, [(self.alarm - 1200, True)])
        log = n.wake_log()
        self.assertEqual((log["alarm_unix"], log["window_minutes"]), (later, 60))
        self.assertEqual(log["light"]["started"], None)
        self.assertEqual(log["light"]["off_at"], later + 3600)
        n.run_until(later - 1800)
        self.assertEqual(n.light.starts[-1], (later - 1800, (later - 1800, later, later + 3600, 100)))
        self.assertEqual((n.wake_log()["alarm_unix"], n.light_log()["started"]), (later, later - 1800))

    def test_replanning_the_same_alarm_leaves_it_running(self):
        n = self.night(-2400, window_minutes=10)
        n.run_until(self.alarm - 1500)
        n.write_settings(window_minutes=5, fade_minutes=5)  # the alarm is planned again, at the same time
        n.run_until(self.alarm - 1000)
        self.assertEqual((len(n.light.starts), n.light.stops), (1, []))
        self.assertTrue(n.light.running())
        self.assertEqual((n.light_log()["started"], n.wake_log()["window_minutes"]), (self.start, 5))

    def test_no_light_for_a_missed_alarm(self):
        n = self.night(-3 * 3600)
        n.step()
        n.clock.now = self.alarm + 1200  # the clock jumped past the whole alarm
        n.run_until(self.alarm + 1300)
        self.assertEqual(n.light.starts, [])
        # on when the clock jumps: it goes off with the skipped alarm
        n = self.night(-1100)
        n.step()
        self.assertTrue(n.light.running())
        n.clock.now = self.alarm + 1200
        n.run_until(self.alarm + 1300)
        self.assertEqual(n.wake_log()["reason"], "missed")
        self.assertEqual((len(n.light.starts), n.light.stops), (1, [(self.alarm + 1200, True)]))

    def test_the_fade_and_buzz_are_the_same_whatever_the_light_does(self):
        def run(light):
            n = self.night(-2400, light=light)
            n.run_until(self.alarm + 130)
            n.watch.awake = self.alarm + 135
            n.run_until(self.alarm + 400)
            log = n.wake_log()
            del log["light"]
            return n, (n.snooz.fades, n.watch.sent, log)

        expected = run(None)[1]
        for light in (RaisingLight, lambda clock: FakeLight(clock, fail_starts=True)):
            with self.subTest(light=light):
                n, outcome = run(light)
                self.assertEqual(outcome, expected)
                if light is RaisingLight:
                    errors = [line for line in n.lines if "sunrise light error" in line]
                    self.assertEqual(len(errors), 1, n.lines)
                else:
                    self.assertEqual(n.light_log()["stopped"], "failed")


class PlanningTests(unittest.TestCase):
    def at(self, *parts):
        return int(datetime(*parts).timestamp())

    def test_next_alarm(self):
        self.assertEqual(next_alarm_unix("06:30", self.at(2026, 10, 12, 23, 0)), self.at(2026, 10, 13, 6, 30))
        self.assertEqual(next_alarm_unix("06:30", self.at(2026, 10, 13, 6, 35)), self.at(2026, 10, 13, 6, 30))
        self.assertEqual(next_alarm_unix("06:30", self.at(2026, 10, 13, 6, 41)), self.at(2026, 10, 14, 6, 30))
        done = self.at(2026, 10, 13, 6, 30)
        self.assertEqual(next_alarm_unix("06:30", self.at(2026, 10, 13, 6, 35), done),
                         self.at(2026, 10, 14, 6, 30))

    def test_plan_alarm(self):
        now = self.at(2026, 10, 12, 23, 0)
        alarm = self.at(2026, 10, 13, 6, 30)
        plan = plan_alarm(clean_settings({"alarm_time": "06:30", "window_minutes": 30, "fade_minutes": 15}), now)
        self.assertEqual((plan.alarm_unix, plan.window_start, plan.latest_start),
                         (alarm, alarm - 1800, alarm - 900))
        short = plan_alarm(clean_settings({"alarm_time": "06:30", "window_minutes": 10, "fade_minutes": 15}), now)
        self.assertEqual(short.latest_start, short.window_start)  # never before the window opens
        self.assertIsNone(plan_alarm(clean_settings({"enabled": False}), now))
        self.assertIsNone(plan_alarm(clean_settings({"alarm_time": "6:30pm"}), now))

    def test_settings_defaults_and_limits(self):
        settings = clean_settings({"window_minutes": 500, "fade_minutes": "abc", "tuning": {"active_ratio": 3}})
        self.assertEqual((settings["enabled"], settings["alarm_time"], settings["window_minutes"],
                          settings["fade_minutes"]), (True, "06:30", 90, 15))
        self.assertEqual(settings["tuning"]["active_ratio"], 3.0)


class StatusTests(unittest.TestCase):
    def status_text(self, window_minutes, fade_minutes, **settings):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.under(Path(tmp))
            paths.settings.write_text(json.dumps({"enabled": True, "alarm_time": "06:30",
                                                  "window_minutes": window_minutes, "fade_minutes": fade_minutes,
                                                  **settings}))
            out = io.StringIO()
            with redirect_stdout(out):
                show_status(paths)
        return out.getvalue()

    def test_shows_the_fade_that_will_run(self):
        self.assertIn("6:15 AM  starts the 15-minute SNOOZ fade if it saw none by then", self.status_text(30, 15))
        short = self.status_text(10, 15)  # the fade can only start when the window opens
        self.assertIn("6:20 AM  starts the 10-minute SNOOZ fade (no smart window", short)
        self.assertNotIn("15-minute", short)
        self.assertIn("Watch: not connected", short)

    def test_shows_this_mornings_light_while_it_is_on_or_stopped_early(self):
        now = int(time.time())
        on = {"start": now - 1200, "full": now + 600, "off_at": now + 4200, "brightness": 100,
              "started": now - 1200, "finished": None, "stopped": None}
        cases = ((on, f"Sunrise light: on now, until {clock12(now + 4200)}"),
                 ({**on, "finished": now - 60, "stopped": "web"},
                  f"Sunrise light: off since {clock12(now - 60)} (turned off in the web app)"),
                 ({**on, "off_at": now - 60, "finished": now - 60, "stopped": "timeout"}, None))
        for light, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                paths = Paths.under(Path(tmp))
                paths.data.mkdir()
                paths.wake_log(night_date(now)).write_text(json.dumps({"alarm_unix": now + 600, "light": light}))
                out = io.StringIO()
                with redirect_stdout(out):
                    show_status(paths)
                lines = [line for line in out.getvalue().splitlines() if line.startswith("Sunrise light:")]
                self.assertEqual(lines, [expected] if expected else [])

    def test_shows_the_sunrise_light(self):
        text = self.status_text(10, 15)
        light = "6:00 AM  the sunrise light starts, full by 6:30 AM, off at 7:30 AM"
        self.assertIn(light, text)
        self.assertLess(text.index(light), text.index("6:20 AM  starts the 10-minute"))  # in time order
        self.assertIn("full by 6:30 AM (60% brightness)", self.status_text(30, 15, sunrise_brightness=60))
        off = self.status_text(30, 15, sunrise_enabled=False)
        self.assertNotIn("the sunrise light starts", off)
        self.assertIn("Sunrise light: switched off in the web app", off)


class NightFeedTests(unittest.TestCase):
    def test_reads_only_complete_new_lines(self):
        rows = night()[0][:3]
        now = rows[2].t
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.under(Path(tmp))
            paths.data.mkdir()
            feed = NightFeed(paths, {})
            self.assertEqual(feed.read_new(now), [])  # no file yet
            path = paths.night_csv(DATE)
            path.write_bytes(f"{CSV_HEADER}\n{csv_line(rows[0])}\n{csv_line(rows[1])[:20]}".encode())
            self.assertEqual(feed.read_new(now), [rows[0]])
            with path.open("ab") as f:
                f.write(f"{csv_line(rows[1])[20:]}\n{csv_line(rows[2])}\n".encode())
            self.assertEqual(feed.read_new(now), rows[1:3])
            self.assertEqual(feed.read_new(now), [])
            self.assertEqual(feed.last_t, rows[2].t)


class SnoozFaderTests(unittest.TestCase):
    def fader(self, script_body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "fake_snooz_ctl.py"
        script.write_text(textwrap.dedent(script_body))
        return SnoozFader(script)

    def run_fade(self, script_body):
        fader = self.fader(script_body)
        fader.start(5)
        fader.wait()
        return fader.result()

    def test_runs_fadeout_in_the_background_one_at_a_time(self):
        fader = self.fader("""
            import sys, time
            time.sleep(0.3)
            print("args", *sys.argv[1:])
            print("Done in 0:00:05")
        """)
        self.assertTrue(fader.start(5))
        self.assertFalse(fader.start(5))
        fader.wait()
        result = fader.result()
        self.assertEqual((result["ok"], result["returncode"]), (True, 0))
        self.assertEqual(result["output"], ["args fadeout 5", "Done in 0:00:05"])
        self.assertIsNotNone(result["finished"])

    def test_failures_are_reported(self):
        # snooz_ctl.py exits 0 when the SNOOZ rejects the command
        self.assertFalse(self.run_fade('print("Command failed: FAILED")\n')["ok"])
        result = self.run_fade('import sys\nsys.exit("Could not find the SNOOZ")\n')
        self.assertEqual((result["ok"], result["returncode"]), (False, 1))
        self.assertEqual(result["output"], ["Could not find the SNOOZ"])


class SunriseLightTests(unittest.TestCase):
    """SunriseLight with a stand-in for sunrise.py, never the real one."""

    STAYS_ON = """
        import pathlib, signal, sys, time
        here = pathlib.Path(__file__)
        if sys.argv[1] == "off":
            here.with_name("off-ran").write_text("yes")
            sys.exit()
        def blank(*_):
            here.with_name("blanked").write_text("yes")  # sunrise.py turns the strip off on SIGTERM
            sys.exit()
        signal.signal(signal.SIGTERM, signal.SIG_IGN if "--ignore-term" in sys.argv else blank)
        print("args", *sys.argv[1:])
        time.sleep(60)
    """

    def light(self, script_body, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        script = self.dir / "fake_sunrise.py"
        script.write_text(textwrap.dedent(script_body))
        light = SunriseLight(script, **kwargs)
        self.addCleanup(light.stop, False)
        return light

    def test_restart_kills_without_blanking_or_turning_off(self):
        light = self.light(self.STAYS_ON)
        light.start(100, 200, 300, 40)
        self.wait_for(light, lambda r: r["output"])
        started = time.monotonic()
        light.stop(turn_off=False)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(light.result()["running"])
        self.assertFalse((self.dir / "blanked").exists())
        self.assertFalse((self.dir / "off-ran").exists())

    def test_a_child_ignoring_sigterm_is_killed_then_turned_off(self):
        light = self.light(self.STAYS_ON.replace('"--ignore-term" in sys.argv', "True"), stop_wait=0.3)
        light.start(100, 200, 300, 40)
        self.wait_for(light, lambda r: r["output"])
        if os.name == "nt":  # TerminateProcess can't be ignored, so stand in for a child that ignores SIGTERM
            light._proc.terminate = lambda: None
        started = time.monotonic()
        light.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(light.result()["running"])
        self.assertTrue((self.dir / "off-ran").exists())

    def wait_for(self, light, test):
        deadline = time.monotonic() + 15
        while not test(light.result()):
            self.assertLess(time.monotonic(), deadline, light.result())
            time.sleep(0.05)
        return light.result()

    def test_runs_until_stopped_then_turns_the_strip_off(self):
        light = self.light("""
            import pathlib, sys, time
            if sys.argv[1] == "off":
                pathlib.Path(__file__).with_name("off-ran").write_text("yes")
                sys.exit()
            print("args", *sys.argv[1:])
            time.sleep(60)
        """)
        self.assertIsNone(light.result())
        self.assertTrue(light.start(100, 200, 300, 40))
        self.assertFalse(light.start(100, 200, 300, 40))  # one at a time
        result = self.wait_for(light, lambda r: r["output"])
        self.assertTrue(result["running"])
        self.assertEqual(result["output"], ["args run --start 100 --full 200 --off 300 --brightness 40"])
        light.stop()
        self.assertFalse(light.result()["running"])
        self.assertTrue((self.dir / "off-ran").exists())

    def test_an_exit_is_reported_with_its_last_lines(self):
        light = self.light("""
            import sys
            for n in range(8):
                print(f"line {n}")
            sys.exit(2)
        """)
        light.start(100, 200, 300, 100)
        result = self.wait_for(light, lambda r: not r["running"])
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["output"], [f"line {n}" for n in range(3, 8)])
        self.assertTrue(light.start(100, 200, 300, 100))  # can start again once it has exited


class WatchCommanderTests(unittest.TestCase):
    def test_command_file_and_status_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.under(Path(tmp))
            clock = FakeClock(1792000000)
            watch = WatchCommander(paths, clock)
            self.assertEqual(watch.status(), WatchStatus(False, None, None))
            watch.send("alarm")
            self.assertEqual(json.loads(paths.cmd.read_text()), {"cmd": "alarm", "sent": clock.now})
            paths.status.write_text(json.dumps({"connected": True, "last_row": clock.now - 5, "awake": None,
                                                "updated": clock.now - 30}))
            self.assertEqual(watch.status(), WatchStatus(True, clock.now - 5, None))
            clock.advance(31)
            self.assertFalse(watch.status().connected)  # the logger stopped updating it
            paths.status.write_text("{half")
            self.assertEqual(watch.status(), WatchStatus(False, None, None))


if __name__ == "__main__":
    unittest.main()
