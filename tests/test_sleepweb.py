import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import sleepweb  # noqa: E402

MORNING = datetime(2026, 10, 23, 6, 45).timestamp()  # before noon: belongs to the night of the 22nd


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = {name: getattr(sleepweb, name) for name in ("BASE", "DATA_DIR", "SETTINGS", "DIARY", "WATCH_CHARGE")}
        sleepweb.BASE = base
        sleepweb.DATA_DIR = base / "data"
        sleepweb.SETTINGS = base / "settings.json"
        sleepweb.DIARY = base / "data" / "diary.json"
        sleepweb.WATCH_CHARGE = base / "watch-charge.json"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(sleepweb, name, value)
        self._tmp.cleanup()

    def stored(self) -> dict:
        return json.loads(sleepweb.SETTINGS.read_text())


class SunriseSettingsTest(TempHome):
    def test_good_values_are_saved(self):
        sleepweb.save_settings({"sunrise_enabled": False, "sunrise_brightness": 55})
        self.assertIs(self.stored()["sunrise_enabled"], False)
        self.assertEqual(self.stored()["sunrise_brightness"], 55)
        for value in (10, 100, 40.0):
            sleepweb.save_settings({"sunrise_brightness": value})
            self.assertEqual(self.stored()["sunrise_brightness"], int(value))

    def test_bad_values_raise_and_save_nothing(self):
        sleepweb.save_settings({"sunrise_enabled": True, "sunrise_brightness": 70})
        bad = [{"sunrise_enabled": value} for value in ("yes", 1, 0, None, "false")]
        bad += [{"sunrise_brightness": value} for value in (9, 5, 101, 0, -10, 50.5, True, "abc", None, [50])]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                sleepweb.save_settings(body)
        self.assertIs(self.stored()["sunrise_enabled"], True)
        self.assertEqual(self.stored()["sunrise_brightness"], 70)

    def test_brightness_message_names_the_range(self):
        with self.assertRaisesRegex(ValueError, "peak brightness .* 10 to 100"):
            sleepweb.save_settings({"sunrise_brightness": 150})

    def test_unknown_keys_are_kept(self):
        sleepweb.BASE.mkdir(parents=True, exist_ok=True)
        sleepweb.SETTINGS.write_text(json.dumps({"alarm_time": "07:00", "tuning": {"active_floor": 0.4},
                                                 "sunrise": {"leds": 30}}))
        sleepweb.save_settings({"sunrise_brightness": 25})
        stored = self.stored()
        self.assertEqual(stored["tuning"], {"active_floor": 0.4})
        self.assertEqual(stored["sunrise"], {"leds": 30})
        self.assertEqual(stored["alarm_time"], "07:00")
        self.assertEqual(stored["sunrise_brightness"], 25)

    def test_page_settings_carry_the_sunrise_timings(self):
        settings = sleepweb.page_settings()
        self.assertEqual(settings["sunrise_ramp_minutes"], 30)
        self.assertEqual(settings["sunrise_hold_minutes"], 60)
        self.assertIs(settings["sunrise_enabled"], True)
        self.assertEqual(settings["sunrise_brightness"], 100)


class ChargeSettingsTest(TempHome):
    def test_good_values_are_saved(self):
        for value in (50, 80, 100, 75.0):
            sleepweb.save_settings({"charge_max": value})
            self.assertEqual(self.stored()["charge_max"], int(value))

    def test_bad_values_raise_and_save_nothing(self):
        sleepweb.save_settings({"charge_max": 85})
        for value in (49, 101, 80.5, True, "abc", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sleepweb.save_settings({"charge_max": value})
        self.assertEqual(self.stored()["charge_max"], 85)

    def test_message_names_the_range(self):
        with self.assertRaisesRegex(ValueError, "charge the watch to .* 50 to 100"):
            sleepweb.save_settings({"charge_max": 20})

    def test_page_settings_carry_the_default(self):
        self.assertEqual(sleepweb.page_settings()["charge_max"], 80)


class WatchChargeTest(TempHome):
    NOW = 1792025300
    UNKNOWN = {"battery": None, "charging": None, "power": "unknown", "source": None,
               "read_at": None, "updated": None, "problem": None, "stale": True}

    def write(self, **fields):
        status = {"battery": 78, "charging": True, "power": "on", "source": "advert",
                  "read_at": self.NOW - 77, "updated": self.NOW - 70, "problem": None, **fields}
        sleepweb.BASE.mkdir(parents=True, exist_ok=True)
        sleepweb.WATCH_CHARGE.write_text(json.dumps(status))
        return status

    def test_missing_file(self):
        self.assertEqual(sleepweb.watch_charge_state(self.NOW), self.UNKNOWN)

    def test_malformed_file(self):
        sleepweb.BASE.mkdir(parents=True, exist_ok=True)
        for text in ("{not json", "[1, 2]", "null", ""):
            with self.subTest(text=text):
                sleepweb.WATCH_CHARGE.write_text(text)
                self.assertEqual(sleepweb.watch_charge_state(self.NOW), self.UNKNOWN)

    def test_fresh_file(self):
        status = self.write()
        self.assertEqual(sleepweb.watch_charge_state(self.NOW), {**status, "stale": False})

    def test_stale_file(self):
        status = self.write(updated=self.NOW - 600, problem="uhubctl failed")
        self.assertEqual(sleepweb.watch_charge_state(self.NOW), {**status, "stale": True})

    def test_stale_without_updated(self):
        self.write(updated=None)
        self.assertTrue(sleepweb.watch_charge_state(self.NOW)["stale"])

    def test_bad_fields_are_normalised(self):
        self.write(battery=150, charging="yes", power="maybe", source=3, read_at="soon", problem=["x"])
        state = sleepweb.watch_charge_state(self.NOW)
        self.assertEqual(state, {"battery": None, "charging": None, "power": "unknown", "source": None,
                                 "read_at": None, "updated": self.NOW - 70, "problem": None, "stale": False})
        self.write(battery=True, charging=None, power="off")
        state = sleepweb.watch_charge_state(self.NOW)
        self.assertIsNone(state["battery"])
        self.assertEqual(state["power"], "off")


class LightTest(TempHome):
    def write_light(self, date: str = "2026-10-22", **light):
        block = {"start": int(MORNING) - 900, "full": int(MORNING) + 900, "off_at": int(MORNING) + 4500,
                 "brightness": 80, "started": int(MORNING) - 900, "finished": None, "stopped": None,
                 "ok": None, "returncode": None, "output": [], "attempts": 1, **light}
        sleepweb.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (sleepweb.DATA_DIR / f"wake-{date}.json").write_text(json.dumps({"date": date, "light": block}))
        return block

    def test_on_while_started_and_not_finished(self):
        block = self.write_light()
        self.assertEqual(sleepweb.light_state(MORNING), {"on": True, "off_at": block["off_at"]})

    def test_off_once_finished(self):
        self.write_light(finished=int(MORNING) - 60, stopped="web", ok=True, returncode=0)
        self.assertFalse(sleepweb.light_state(MORNING)["on"])

    def test_off_before_it_started(self):
        self.write_light(started=None)
        self.assertFalse(sleepweb.light_state(MORNING)["on"])

    def test_off_past_off_at(self):
        block = self.write_light()
        self.assertFalse(sleepweb.light_state(block["off_at"])["on"])
        self.assertFalse(sleepweb.light_state(block["off_at"] + 60)["on"])

    def test_off_without_a_wake_log(self):
        self.assertEqual(sleepweb.light_state(MORNING), {"on": False, "off_at": None})

    def test_off_with_a_log_but_no_light_block(self):
        sleepweb.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (sleepweb.DATA_DIR / "wake-2026-10-22.json").write_text(json.dumps({"date": "2026-10-22"}))
        self.assertEqual(sleepweb.light_state(MORNING), {"on": False, "off_at": None})

    def test_reads_last_nights_log_in_the_morning(self):
        self.write_light(date="2026-10-23")  # tonight's log, not this morning's
        self.assertFalse(sleepweb.light_state(MORNING)["on"])

    def test_light_from_a_late_morning_alarm_is_still_found_after_noon(self):
        afternoon = datetime(2026, 10, 23, 12, 20).timestamp()
        self.write_light(off_at=int(afternoon) + 600)
        self.assertEqual(sleepweb.light_state(afternoon), {"on": True, "off_at": int(afternoon) + 600})

    def test_light_off_command_file(self):
        result = sleepweb.send_light_off(MORNING + 0.7)
        command = json.loads((sleepweb.BASE / "light-cmd.json").read_text())
        self.assertEqual(command, {"cmd": "off", "sent": int(MORNING)})
        self.assertEqual(result, command)
        self.assertEqual(sorted(p.name for p in sleepweb.BASE.iterdir()), ["light-cmd.json"])


if __name__ == "__main__":
    unittest.main()
