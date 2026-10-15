import argparse
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.make_test_night import clock_time, generate, write_csv  # noqa: E402
from analysis.sleep_analysis import (  # noqa: E402
    DEFAULT_TUNING, Detector, Row, alarm_unix_for_night, analyze, parse_csv_line, read_rows,
    simulate_wake, tuning_from_settings,
)

DATE = "2026-10-12"
PI_HEADER = "time,watch_unix,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery,pi_unix".split(",")
NIGHTS = [(profile, seed) for profile in ("normal", "restless", "short") for seed in range(1, 9)]
_cache = {}


def night(profile="normal", seed=1, turnover_at=None):
    key = (profile, seed, turnover_at)
    if key not in _cache:
        _cache[key] = generate(DATE, seed, profile, turnover_at)
    return _cache[key]


def still_row(t, mov=1.0, vec=(0.0, -0.7, 0.71), hr=55.0):
    return Row(t, 125, mov, 0.03, vec[0], vec[1], vec[2], hr, 80.0, 90.0)


def asleep_detector(minutes=60):
    """A Detector after `minutes` quiet minutes at 55 bpm, and the minute-aligned time after them."""
    detector = Detector()
    start = 1791879960
    for i in range(minutes * 6):
        detector.add(still_row(start + 10 * i))
    return detector, start + minutes * 60


def quiet_deep_stretch(events, minutes):
    """Start and end of the first deep-sleep stretch at least `minutes` long with nothing planned in it."""
    busy = events["turnovers"] + [a for a, _ in events["awakenings"]]
    for start, end, stage in events["stages"]:
        if stage == "deep" and end - start >= minutes * 60 and not any(start - 300 <= t <= end for t in busy):
            return start, end
    raise AssertionError("test night has no quiet deep sleep")


class ParseTests(unittest.TestCase):
    def test_accepts_a_logger_line(self):
        row = parse_csv_line("2026-10-12T23:24:19,1791872659,125,8.065,0.229,-0.37,-0.74,0.57,70,61,99,1791872660",
                             PI_HEADER)
        self.assertEqual(row, Row(1791872659, 125, 8.065, 0.229, -0.37, -0.74, 0.57, 70.0, 61.0, 99.0))

    def test_blank_heart_rate_is_none(self):
        row = parse_csv_line("2026-10-12T23:24:19,1791872659,125,1.0,0.02,0.1,-0.7,0.7,,0,99,1791872660\n",
                             PI_HEADER)
        self.assertIsNotNone(row)
        self.assertIsNone(row.hr)
        self.assertEqual(row.hr_conf, 0.0)

    def test_rejects_header_garbled_and_short_lines(self):
        bad = [
            ",".join(PI_HEADER),
            "",
            "# start 1791872659 t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery",
            "2026-10-12T23:24:19,1791872659,125,8.0",
            "2026-10-12T23:24:19,17918726xx,125,8.065,0.229,-0.37,-0.74,0.57,70,61,99,1791872660",
            "2026-10-12T23:24:19,1791872659,125,nan,0.229,-0.37,-0.74,0.57,70,61,99,1791872660",
            "2026-10-12T23:24:19,1791872659,125,8.065,0.229,-0.37,-0.74,0.57,70,61,99,1791872660,extra",
            "1970-01-01T00:00:10,10,125,8.065,0.229,-0.37,-0.74,0.57,70,61,99,1791872660",
        ]
        for line in bad:
            self.assertIsNone(parse_csv_line(line, PI_HEADER), line)

    def test_watch_pilog_column_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilog.csv"
            path.write_text("# start 1791872600 t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery\n"
                            "1791872659,125,8.065,0.229,-0.37,-0.74,0.57,,0,99\n"
                            "1791872669,124,1.5\n")
            rows = read_rows(path)
        self.assertEqual(rows, [Row(1791872659, 125, 8.065, 0.229, -0.37, -0.74, 0.57, None, 0.0, 99.0)])

    def test_file_round_trip_skips_damage(self):
        rows, _ = night()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"watch-{DATE}.csv"
            write_csv(rows, path)
            with path.open("a") as f:
                f.write("garbage line\n2026-10-13T07:20:00,1791901200,125,1.0")  # half-written last line
            self.assertEqual(read_rows(path), rows)


class TuningTests(unittest.TestCase):
    def test_defaults_without_tuning(self):
        self.assertEqual(tuning_from_settings({"enabled": True}), DEFAULT_TUNING)
        self.assertEqual(tuning_from_settings({"tuning": "junk"}), DEFAULT_TUNING)

    def test_coerces_and_ignores_junk(self):
        tuning = tuning_from_settings({"tuning": {
            "active_floor": "12.5", "onset_minutes": "15", "light_minutes": 4.0, "hr_rise_bpm": 6,
            "posture_degrees": "abc", "active_ratio": None, "awake_minutes": True, "baseline_minutes": 0,
            "hr_min_confidence": -5, "unknown": 3,
        }})
        self.assertEqual(tuning["active_floor"], 12.5)
        self.assertEqual(tuning["onset_minutes"], 15)
        self.assertIsInstance(tuning["onset_minutes"], int)
        self.assertEqual(tuning["light_minutes"], 4)
        self.assertIsInstance(tuning["light_minutes"], int)
        self.assertEqual(tuning["hr_rise_bpm"], 6.0)
        self.assertIsInstance(tuning["hr_rise_bpm"], float)
        for key in ("posture_degrees", "active_ratio", "awake_minutes", "baseline_minutes", "hr_min_confidence"):
            self.assertEqual(tuning[key], DEFAULT_TUNING[key], key)
        self.assertNotIn("unknown", tuning)


class AnalyzeTests(unittest.TestCase):
    def test_onset_and_final_wake_match_the_generator(self):
        for profile, seed in NIGHTS:
            with self.subTest(profile=profile, seed=seed):
                rows, events = night(profile, seed)
                result = analyze(rows)
                self.assertLessEqual(abs(result["onset"] - events["onset"]), 600)
                self.assertLessEqual(abs(result["final_wake"] - events["final_wake"]), 600)
                slept = (events["final_wake"] - events["onset"]) / 60
                self.assertTrue(0.8 * slept <= result["asleep_minutes"] <= slept + 10)

    def test_spans_do_not_overlap_and_stay_inside_the_data(self):
        for profile, seed in NIGHTS:
            with self.subTest(profile=profile, seed=seed):
                rows, _ = night(profile, seed)
                result = analyze(rows)
                first = rows[0].t - rows[0].t % 60
                last = rows[-1].t - rows[-1].t % 60 + 60
                light, awake = result["light_spans"], result["awake_spans"]
                for spans in (light, awake):
                    for (s, e), nxt in zip(spans, spans[1:] + [None]):
                        self.assertTrue(first <= s < e <= last)
                        if nxt:
                            self.assertLess(e, nxt[0] + 1)
                for ls, le in light:
                    for as_, ae in awake:
                        self.assertTrue(le <= as_ or ae <= ls, f"{ls}-{le} overlaps {as_}-{ae}")
                self.assertEqual(result["light_minutes"], sum((e - s) // 60 for s, e in light))
                self.assertEqual(result["rows"], len(rows))

    def test_row_by_row_matches_analyze(self):
        for profile, seed in (("normal", 1), ("restless", 2)):
            rows, _ = night(profile, seed)
            detector = Detector()
            for row in rows:
                detector.add(row)
                detector.arousal_now()  # asking must not change anything
            detector.finish()
            result = analyze(rows)
            self.assertEqual([m.as_dict() for m in detector.minutes], result["minutes"])
            self.assertEqual(detector.onset, result["onset"])

    def test_later_rows_never_change_settled_minutes(self):
        # Cutting a night short may only change its last few minutes; anything more is look-ahead.
        # Deciding onset labels the minutes since it, which reach back onset_minutes.
        recent = DEFAULT_TUNING["onset_minutes"]
        for profile, seed in (("normal", 1), ("restless", 2)):
            rows, _ = night(profile, seed)
            full = analyze(rows)
            for k in range(40, len(rows), 37):
                with self.subTest(profile=profile, seed=seed, rows=k):
                    part = analyze(rows[:k])
                    settled = part["minutes"][:-recent]
                    self.assertEqual(settled, full["minutes"][:len(settled)])
                    if rows[k - 1].t >= full["onset"] + 20 * 60:
                        self.assertEqual(part["onset"], full["onset"])

    def test_onset_is_not_inside_an_awake_span(self):
        for profile, seed in NIGHTS:
            with self.subTest(profile=profile, seed=seed):
                result = analyze(night(profile, seed)[0])
                self.assertFalse(any(s <= result["onset"] < e for s, e in result["awake_spans"]), result["onset"])

    def test_onset_right_after_activity_is_light(self):
        start = 1791879960
        rows = [still_row(start + 10 * i, mov=20.0) for i in range(30)]  # five active minutes
        rows += [still_row(start + 300 + 10 * i) for i in range(73)]   # twelve still minutes
        result = analyze(rows)
        by_t = {m["t"]: m for m in result["minutes"]}
        last_active = start + 4 * 60
        self.assertEqual(result["onset"], last_active + 3 * 60)
        self.assertTrue(by_t[result["onset"]]["light"])
        self.assertFalse(by_t[result["onset"] + 60]["light"])

    def test_bluetooth_gap_counts_as_asleep(self):
        rows, events = night()
        start, _ = quiet_deep_stretch(events, 30)
        start -= start % 60
        cut = [r for r in rows if not start + 5 * 60 <= r.t < start + 25 * 60]
        full, gapped = analyze(rows), analyze(cut)
        self.assertEqual(len(full["minutes"]) - len(gapped["minutes"]), 20)
        # not exact: the 20 missing quiet minutes nudge the resting-movement baseline
        self.assertLessEqual(abs(full["asleep_minutes"] - gapped["asleep_minutes"]), 2)

    def test_a_long_gap_is_not_counted_as_asleep(self):
        rows, _ = night()
        full = analyze(rows)
        # an evening test with the watch lying still on a desk, three hours before bed, in the same file
        desk_start = rows[0].t - rows[0].t % 60 - 3 * 3600
        desk = [still_row(desk_start + 10 * i) for i in range(15 * 6)]
        with_test = analyze(desk + rows)
        self.assertEqual(with_test["onset"], desk_start)
        # the desk minutes and the minutes in bed before falling asleep count; the three hours between don't
        in_bed = (full["onset"] - rows[0].t) // 60 + 1
        self.assertLessEqual(with_test["asleep_minutes"], full["asleep_minutes"] + 15 + in_bed)

    def test_real_watch_noise_floor_is_still(self):
        # The first real night (Oct 2026): lying still reads about 2.0 per row, 12 per minute, all
        # night long, with minutes of 20-100 at bedtime, one awakening and brief stirs. With the
        # original floor of 10 every minute was active and the night had no onset at all.
        start = 1791879960
        rng = random.Random(4)
        rows = []
        for minute in range(8 * 60):
            if minute < 12 or 240 <= minute < 255:
                level = rng.uniform(4.0, 15.0)   # settling down, and an awakening
            elif minute in (90, 150, 210, 330, 400):
                level = rng.uniform(4.5, 5.5)    # a stir: 27-33 per minute
            else:
                level = rng.gauss(2.0, 0.1)
            rows += [still_row(start + 60 * minute + 10 * i, mov=round(max(1.6, rng.gauss(level, 0.1)), 3))
                     for i in range(6)]
        result = analyze(rows)
        by_t = {m["t"]: m for m in result["minutes"]}
        self.assertEqual(result["onset"], start + 14 * 60)  # three still minutes after the last active one
        self.assertTrue(by_t[start + 90 * 60]["active"])
        self.assertFalse(by_t[start + 91 * 60]["active"])
        self.assertEqual(result["awake_spans"][1:], [[start + 240 * 60, start + 257 * 60]])
        self.assertEqual(result["asleep_minutes"], 8 * 60 - 14 - 17)  # minus settling down and the awakening
        self.assertTrue(by_t[start + 330 * 60]["light"])

    def test_no_rows(self):
        result = analyze([])
        self.assertEqual((result["minutes"], result["onset"], result["final_wake"]), ([], None, None))
        self.assertEqual(result["asleep_minutes"], 0)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = Detector()
        self.t = 1791880000
        for _ in range(18):  # three quiet minutes
            self.t += 10
            self.detector.add(still_row(self.t))

    def test_nothing_before_data(self):
        self.assertIsNone(Detector().arousal_now())

    def test_quiet_rows_and_one_small_blip_do_not_fire(self):
        self.assertIsNone(self.detector.arousal_now())
        self.detector.add(still_row(self.t + 10, mov=3.0))
        self.assertIsNone(self.detector.arousal_now())

    def test_movement_fires_on_the_same_row(self):
        self.detector.add(still_row(self.t + 10, mov=20.0))
        self.assertTrue(self.detector.arousal_now().startswith("movement"))

    def test_quiet_turn_over_fires_within_30_seconds(self):
        reasons = []
        for i in range(1, 4):
            self.detector.add(still_row(self.t + 10 * i, vec=(0.6, -0.7, 0.35)))
            reasons.append(self.detector.arousal_now())
        self.assertTrue(any(r and r.startswith("turned over") for r in reasons), reasons)

    def test_heart_rate_rise_fires_while_fresh(self):
        detector, t = asleep_detector()
        for i in range(13):  # two whole minutes at 66 bpm, then the row that closes the second
            detector.add(still_row(t + 10 * i, hr=66.0))
        self.assertTrue((detector.arousal_now() or "").startswith("heart rate"), detector.arousal_now())

    def test_heart_rate_rise_is_not_reported_after_a_gap(self):
        detector, t = asleep_detector()
        for i in range(12):
            detector.add(still_row(t + 10 * i, hr=66.0))
        detector.add(still_row(t + 40 * 60))  # reconnecting 40 minutes later closes the old minute
        self.assertIsNone(detector.arousal_now())

    def add_minute(self, detector, t, mov=1.0):
        for i in range(6):
            detector.add(still_row(t + 10 * i, mov=mov))

    def test_awake_rule_does_not_reach_across_a_gap(self):
        detector, t = asleep_detector()
        self.add_minute(detector, t, mov=20.0)
        self.add_minute(detector, t + 60, mov=20.0)
        self.add_minute(detector, t + 40 * 60, mov=20.0)
        detector.add(still_row(t + 41 * 60))
        moved = [m for m in detector.minutes if m.t in (t, t + 60, t + 40 * 60)]
        self.assertEqual([m.active for m in moved], [True, True, True])
        self.assertEqual([m.awake for m in moved], [False, False, False])

    def test_light_rule_does_not_reach_across_a_gap(self):
        detector, t = asleep_detector()
        self.add_minute(detector, t, mov=20.0)
        self.add_minute(detector, t + 40 * 60)
        detector.add(still_row(t + 41 * 60))
        self.assertEqual(detector.minutes[-1].t, t + 40 * 60)
        self.assertFalse(detector.minutes[-1].light)

    def test_onset_needs_still_minutes_without_a_gap(self):
        detector = Detector()
        start = 1791879960
        for i in range(6):
            self.add_minute(detector, start + 60 * i)
        for i in range(46, 56):
            self.add_minute(detector, start + 60 * i)
            self.assertIsNone(detector.onset, i)
        detector.add(still_row(start + 56 * 60))
        self.assertEqual(detector.onset, start + 46 * 60)

    def test_resting_heart_rate_leaves_out_a_restless_wake_bout(self):
        detector = Detector()
        start = 1791879960

        def minute(i, hr, mov=1.0):
            for k in range(6):
                detector.add(still_row(start + 60 * i + 10 * k, mov=mov, hr=hr))

        for i in range(60):
            minute(i, 52.0)
        for i in range(60, 95):  # lying awake at 72 bpm, moving every other minute
            minute(i, 72.0, mov=20.0 if i % 2 == 0 else 1.0)
        for i in range(95, 120):
            minute(i, 52.0)
        reasons = []
        for i in range(120, 123):  # back asleep, then a still heart rate rise
            for k in range(6):
                detector.add(still_row(start + 60 * i + 10 * k, hr=63.0))
                reasons.append(detector.arousal_now())
        self.assertLessEqual(detector.resting_hr, 53)
        self.assertTrue(any(r and r.startswith("heart rate") for r in reasons), reasons)

    def test_heart_rate_rise_needs_consecutive_minutes(self):
        detector, t = asleep_detector()
        for i in range(6):
            detector.add(still_row(t + 10 * i, hr=66.0))
        for i in range(7):  # ten minutes later, another high minute and the row that closes it
            detector.add(still_row(t + 600 + 10 * i, hr=66.0))
        self.assertIsNone(detector.arousal_now())


class SimulateWakeTests(unittest.TestCase):
    def setUp(self):
        _, self.events = night()
        start, _ = quiet_deep_stretch(self.events, 20)
        self.window_start = start + 3 * 60
        self.alarm = self.window_start + 20 * 60  # 20-minute window, 8-minute fade
        self.latest_start = self.alarm - 8 * 60

    def test_fires_on_a_planted_turnover(self):
        planted = self.window_start + 6 * 60
        rows, _ = night(turnover_at=planted)
        result = simulate_wake(rows, self.alarm, 20, 8)
        self.assertEqual(result["reason"], "light_sleep", result)
        self.assertTrue(0 <= result["trigger_t"] - planted <= 70, result)
        self.assertEqual(result["fade_start"], result["trigger_t"])
        self.assertEqual(result["fade_end"], result["fade_start"] + 8 * 60)

    def test_quiet_window_falls_back(self):
        rows, _ = night()
        result = simulate_wake(rows, self.alarm, 20, 8)
        self.assertEqual(result["reason"], "fallback", result)
        self.assertEqual(result["fade_start"], self.latest_start)
        self.assertEqual(result["fade_end"], self.alarm)
        self.assertIsNone(result["trigger"])

    def test_no_data_when_recording_stops_early(self):
        rows, _ = night()
        early = [r for r in rows if r.t < self.window_start - 600]
        self.assertEqual(simulate_wake(early, self.alarm, 20, 8)["reason"], "no_data")
        self.assertEqual(simulate_wake([], self.alarm, 20, 8)["reason"], "no_data")

    def test_fade_never_ends_after_the_alarm(self):
        rows, events = night("restless", 3)
        for alarm in range(events["onset"] + 3600, events["end"], 1700):
            for window in (0, 10, 30, 90):
                for fade in (1, 15, 30):
                    result = simulate_wake(rows, alarm, window, fade)
                    self.assertLessEqual(result["fade_end"], alarm)
                    self.assertTrue(result["window_start"] <= result["fade_start"] <= result["latest_start"])
                    self.assertLessEqual(result["fade_start"], result["fade_end"])

    def test_no_window_fades_before_the_alarm(self):
        rows, _ = night()
        result = simulate_wake(rows, self.alarm, 0, 15)
        self.assertEqual(result["fade_start"], self.alarm - 15 * 60)
        self.assertEqual(result["fade_end"], self.alarm)
        self.assertEqual(result["reason"], "fallback")

    def test_short_window_never_starts_before_it_opens(self):
        rows, _ = night()
        result = simulate_wake(rows, self.alarm, 5, 15)
        self.assertEqual(result["latest_start"], self.alarm - 5 * 60)
        self.assertEqual(result["fade_end"], self.alarm)


class AlarmTimeTests(unittest.TestCase):
    def at(self, *parts):
        return int(datetime(*parts).timestamp())

    def test_crosses_midnight(self):
        rows = [still_row(self.at(2026, 10, 13, 23, 30))]
        self.assertEqual(alarm_unix_for_night("2026-10-13", "06:30", rows), self.at(2026, 10, 14, 6, 30))

    def test_first_row_after_midnight(self):
        rows = [still_row(self.at(2026, 10, 14, 0, 40))]
        self.assertEqual(alarm_unix_for_night("2026-10-13", "06:30", rows), self.at(2026, 10, 14, 6, 30))

    def test_same_evening_alarm(self):
        rows = [still_row(self.at(2026, 10, 13, 23, 30))]
        self.assertEqual(alarm_unix_for_night("2026-10-13", "23:45", rows), self.at(2026, 10, 13, 23, 45))

    def test_without_rows_uses_noon(self):
        self.assertEqual(alarm_unix_for_night("2026-10-13", "06:30", []), self.at(2026, 10, 14, 6, 30))

    def test_wrong_watch_clock_uses_noon(self):
        rows = [still_row(self.at(2026, 10, 20, 23, 30))]
        self.assertEqual(alarm_unix_for_night("2026-10-13", "06:30", rows), self.at(2026, 10, 14, 6, 30))


class GeneratorTests(unittest.TestCase):
    def test_same_seed_same_night(self):
        self.assertEqual(generate(DATE, 7, "short"), generate(DATE, 7, "short"))

    def test_planting_a_turnover_keeps_the_structure(self):
        _, plain = night()
        _, planted = night(turnover_at=plain["onset"] + 7200)
        for key in ("in_bed", "onset", "final_wake", "end", "cycles", "awakenings", "turnovers", "stages"):
            self.assertEqual(plain[key], planted[key], key)
        self.assertEqual(planted["planted_turnover"], plain["onset"] + 7200)

    def test_turnover_at_must_be_a_24_hour_time(self):
        self.assertEqual(clock_time("5:50"), "05:50")
        for bad in ("6pm", "24:00", "06:60", "0630", ""):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                clock_time(bad)

    def test_cli_rejects_a_bad_turnover_time_cleanly(self):
        script = Path(__file__).resolve().parents[1] / "analysis" / "make_test_night.py"
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(script), "--date", DATE, "--out", tmp,
                                   "--turnover-at", "6pm"], capture_output=True, text=True)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(done.returncode, 2)
        self.assertIn("HH:MM", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_cli_rejects_a_turnover_outside_the_night(self):
        script = Path(__file__).resolve().parents[1] / "analysis" / "make_test_night.py"
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(script), "--date", DATE, "--out", tmp,
                                   "--turnover-at", "13:00"], capture_output=True, text=True)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(done.returncode, 1)
        self.assertIn("1:00 PM is outside this night", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
