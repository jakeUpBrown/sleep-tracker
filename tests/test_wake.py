import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.make_test_night import generate  # noqa: E402
from analysis.sleep_analysis import DEFAULT_TUNING, simulate_wake  # noqa: E402
from wake.wake import (  # noqa: E402
    FADING, IDLE, WINDOW, NightFeed, Paths, SnoozFader, WakeController, WatchCommander, WatchStatus,
    clean_settings, next_alarm_unix, plan_alarm, show_status,
)

DATE = "2026-10-12"
WINDOW_MINUTES = 20
FADE_MINUTES = 8
CSV_HEADER = "time,watch_unix,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery,pi_unix"
LOG_KEYS = {"date", "alarm_time", "alarm_unix", "window_minutes", "fade_minutes", "window_start", "latest_start",
            "fade_start", "fade_end", "reason", "trigger", "trigger_t", "snooz", "buzz", "tuning", "finished"}
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

    def __init__(self, test, rows, alarm, start, connected=True, **settings):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.paths = Paths.under(Path(tmp.name))
        self.paths.data.mkdir(parents=True)
        self.clock = FakeClock(start)
        self.snooz = FakeSnooz(self.clock)
        self.watch = FakeWatch(self.clock, connected)
        self.lines = []
        self.controller = WakeController(self.paths, self.clock, self.snooz, self.watch, self.lines.append)
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
    def status_text(self, window_minutes, fade_minutes):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.under(Path(tmp))
            paths.settings.write_text(json.dumps({"enabled": True, "alarm_time": "06:30",
                                                  "window_minutes": window_minutes, "fade_minutes": fade_minutes}))
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
