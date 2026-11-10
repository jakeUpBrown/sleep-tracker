import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from charge.charge import (  # noqa: E402
    HOLD_SECONDS, LOGGER_STATUS_MAX_AGE, OFF, ON, READING_MAX_AGE, RESEND_SECONDS, RESUME_BELOW,
    SWITCH_MIN_INTERVAL, UNKNOWN, Controller, FakeSwitch, Paths, Reading, SwitchError, UhubctlSwitch,
    desired_power, parse_advert, read_logger_status, reading_from_advert, show_status, switch_from_settings,
    watch_matcher,
)

NOW = 1_792_000_000
BATTERY_UUID = "0000180f-0000-1000-8000-00805f9b34fb"


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class TempPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.paths = Paths.under(base, base / "bangle.json")
        self.clock = FakeClock()
        self.switch = FakeSwitch(self.clock)
        # what the fake scan hands out, in order: (battery, charging[, recording, hold]), None for "nothing
        # seen", or an exception to raise
        self.adverts: list = []
        self.scans = 0

    def scan(self, now):
        self.scans += 1
        if not self.adverts:
            return None
        item = self.adverts.pop(0)
        if isinstance(item, Exception):
            raise item
        return None if item is None else Reading(item[0], item[1], int(now), "advert", *item[2:])

    def controller(self):
        return Controller(self.paths, self.switch, clock=self.clock, scan=self.scan)

    def tick(self, controller, *adverts, advance=60):
        self.clock.now += advance
        self.adverts.extend(adverts)
        out = io.StringIO()
        with redirect_stdout(out):
            controller.tick()
        return out.getvalue()

    def state(self) -> dict:
        return json.loads(self.paths.state.read_text())

    def changes(self) -> list:
        """The switch's calls without the re-sends of the same state."""
        out = []
        for _, on in self.switch.calls:
            if not out or out[-1] != on:
                out.append(on)
        return out

    def write_settings(self, **settings):
        self.paths.base.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_text(json.dumps(settings))

    def write_logger_status(self, **status):
        self.paths.base.mkdir(parents=True, exist_ok=True)
        self.paths.status.write_text(json.dumps({"connected": True, "updated": int(self.clock.now), **status}))


class DesiredPowerTest(unittest.TestCase):
    def test_off_at_the_cap_on_below_the_band_and_unchanged_inside_it(self):
        self.assertEqual(desired_power(ON, 80, 80), OFF)
        self.assertEqual(desired_power(ON, 95, 80), OFF)
        self.assertEqual(desired_power(OFF, 70, 80), ON)
        self.assertEqual(desired_power(OFF, 71, 80), OFF)
        self.assertEqual(desired_power(ON, 75, 80), ON)
        self.assertEqual(desired_power(OFF, 79, 80), OFF)

    def test_a_watch_that_needs_the_charger_gets_it_at_any_level(self):
        self.assertEqual(desired_power(OFF, 79, 80, needs_charger=True), ON)
        self.assertEqual(desired_power(OFF, 100, 80, needs_charger=True), ON)
        self.assertEqual(desired_power(OFF, 100, 80, needs_charger=False), OFF)

    def test_unsure_means_on(self):
        self.assertEqual(desired_power(UNKNOWN, None, 80), ON)
        self.assertEqual(desired_power(OFF, None, 80), ON)
        self.assertEqual(desired_power(UNKNOWN, 75, 80), ON)
        self.assertEqual(desired_power(UNKNOWN, 80, 80), OFF)

    def test_band_follows_the_cap(self):
        self.assertEqual(RESUME_BELOW, 10)
        self.assertEqual(desired_power(OFF, 60, 70), ON)
        self.assertEqual(desired_power(OFF, 61, 70), OFF)
        self.assertEqual(desired_power(ON, 100, 100), OFF)
        self.assertEqual(desired_power(ON, 99, 100), ON)


class ReadingsTest(TempPaths):
    def test_logger_status_counts_only_while_connected_and_fresh(self):
        self.write_logger_status(battery=77, charging=True)
        reading = read_logger_status(self.paths.status, self.clock.now + 10)
        self.assertEqual(reading, Reading(77, True, int(self.clock.now), "logger"))
        self.assertIsNone(read_logger_status(self.paths.status, self.clock.now + LOGGER_STATUS_MAX_AGE + 1))
        self.write_logger_status(battery=77, charging=True, connected=False)
        self.assertIsNone(read_logger_status(self.paths.status, self.clock.now))
        self.write_logger_status(battery=None)
        self.assertIsNone(read_logger_status(self.paths.status, self.clock.now))
        self.write_logger_status(battery=150)
        self.assertIsNone(read_logger_status(self.paths.status, self.clock.now))
        self.write_logger_status(battery=60, charging="yes")
        self.assertEqual(read_logger_status(self.paths.status, self.clock.now).charging, None)
        self.write_logger_status(battery=60, charging=False, recording=True, hold=False)
        reading = read_logger_status(self.paths.status, self.clock.now)
        self.assertEqual((reading.recording, reading.hold, reading.needs_charger), (True, False, True))
        self.write_logger_status(battery=60, hold=True)
        self.assertEqual(read_logger_status(self.paths.status, self.clock.now).needs_charger, True)
        self.write_logger_status(battery=60, hold="yes")
        self.assertEqual(read_logger_status(self.paths.status, self.clock.now).needs_charger, False)
        self.assertIsNone(read_logger_status(self.paths.base / "missing.json", self.clock.now))

    def test_parse_advert(self):
        self.assertEqual(parse_advert({BATTERY_UUID: b"\x4e"}, {0x0590: b"\x01"}), (78, 1))
        self.assertEqual(parse_advert({BATTERY_UUID.upper(): bytes([80])}, {0x0590: b"\x06"}), (80, 6))
        self.assertEqual(parse_advert({BATTERY_UUID: b"\x4e"}, {}), (78, None))
        self.assertEqual(parse_advert({BATTERY_UUID: b"\x4e"}, {0x0590: b""}), (78, None))
        self.assertEqual(parse_advert({}, {0x0590: b"\x01"}), (None, 1))
        self.assertEqual(parse_advert({BATTERY_UUID: b"\xff"}, {}), (None, None))  # 255% is not a level
        self.assertEqual(parse_advert(None, None), (None, None))

    def test_reading_from_advert_unpacks_the_flags(self):
        self.assertEqual(reading_from_advert(78, 1, NOW), Reading(78, True, NOW, "advert", False, False))
        self.assertEqual(reading_from_advert(78, 2, NOW), Reading(78, False, NOW, "advert", True, False))
        self.assertEqual(reading_from_advert(78, 5, NOW), Reading(78, True, NOW, "advert", False, True))
        self.assertEqual(reading_from_advert(78, 0, NOW), Reading(78, False, NOW, "advert"))
        self.assertEqual(reading_from_advert(78, None, NOW), Reading(78, None, NOW, "advert"))

    def test_watch_matcher_uses_the_saved_address_or_the_name(self):
        class Device:
            def __init__(self, address, name=None):
                self.address, self.name = address, name

        class Adv:
            def __init__(self, local_name=None):
                self.local_name = local_name

        match = watch_matcher(self.paths.bangle)  # nothing saved yet
        self.assertTrue(match(Device("AA:BB"), Adv("Bangle.js 1234")))
        self.assertTrue(match(Device("AA:BB", "Bangle.js 1234"), Adv()))
        self.assertFalse(match(Device("AA:BB", "Puck.js"), Adv()))
        self.paths.bangle.write_text(json.dumps({"address": "aa:bb:cc:dd:ee:ff"}))
        match = watch_matcher(self.paths.bangle)
        self.assertTrue(match(Device("AA:BB:CC:DD:EE:FF"), Adv("something else")))
        self.assertFalse(match(Device("11:22:33:44:55:66"), Adv("Bangle.js 1234")))


class ControllerTest(TempPaths):
    def test_starts_by_powering_the_dock_when_there_is_no_reading(self):
        c = self.controller()
        out = self.tick(c)
        self.assertEqual(self.switch.calls, [(self.clock.now, True)])
        self.assertEqual(c.power, ON)
        self.assertIn("no recent battery reading", out)
        state = self.state()
        self.assertEqual((state["battery"], state["power"], state["source"], state["read_at"]), (None, ON, None, None))
        self.assertEqual(state["charge_max"], 80)
        self.assertIsNone(state["problem"])
        self.assertEqual(state["updated"], self.clock.now)

    def test_cuts_power_at_the_cap_and_restores_it_below_the_band(self):
        c = self.controller()
        self.tick(c, (60, True))
        self.assertEqual(c.power, ON)
        self.tick(c, (79, True), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, ON)
        out = self.tick(c, (80, True), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, OFF)
        self.assertIn("dock power off (battery 80%, cap 80%)", out)
        self.assertEqual(self.state()["power"], OFF)
        self.assertEqual((self.state()["battery"], self.state()["charging"], self.state()["source"]),
                         (80, True, "advert"))
        for level in (82, 78, 71):  # resting after the cut, then slowly draining: still off
            self.tick(c, (level, False), advance=SWITCH_MIN_INTERVAL)
            self.assertEqual(c.power, OFF, level)
        out = self.tick(c, (70, False), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, ON)
        self.assertIn("dock power on (battery 70%, cap 80%)", out)
        self.assertEqual(self.changes(), [True, False, True])

    def test_a_recording_or_hand_stopped_watch_gets_a_powered_dock(self):
        c = self.controller()
        self.tick(c, (90, True))
        self.assertEqual(c.power, OFF)
        # back in the dock in the morning above the resume level, still recording: only a live charger stops it
        out = self.tick(c, (79, False, True), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, ON)
        self.assertIn("the watch is recording", out)
        self.assertIs(self.state()["recording"], True)
        self.tick(c, (79, True, False), advance=SWITCH_MIN_INTERVAL)  # charging now, recording stopped
        self.assertEqual(c.power, ON)  # inside the band: unchanged
        self.tick(c, (80, True, False), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, OFF)
        out = self.tick(c, (80, False, False, True), advance=SWITCH_MIN_INTERVAL)  # stopped by hand
        self.assertEqual(c.power, ON)
        self.assertIn("stopped by hand", out)
        self.assertIs(self.state()["hold_by_hand"], True)

    def test_resends_the_wanted_state_now_and_then(self):
        c = self.controller()
        self.tick(c, (90, True))  # a first reading at the cap: straight to off, no "on" first
        self.assertEqual([on for _, on in self.switch.calls], [False])
        for _ in range(RESEND_SECONDS // 60 - 1):
            self.tick(c, (90, True))
        self.assertEqual(len(self.switch.calls), 1)
        out = self.tick(c, (90, True))
        self.assertEqual([on for _, on in self.switch.calls], [False, False])
        self.assertNotIn("dock power off (", out)  # a re-send is not a change, so no controller line
        self.tick(c, (60, True))  # a change right after a re-send does not wait for the switch interval
        self.assertEqual(c.power, ON)

    def test_waits_between_switches_so_a_charging_reading_can_settle(self):
        c = self.controller()
        self.tick(c, (80, True))
        self.assertEqual(c.power, OFF)
        cut_at = self.clock.now
        self.tick(c, (60, False))  # a wild reading a minute later changes nothing yet
        self.assertEqual(c.power, OFF)
        self.tick(c, (60, False), advance=SWITCH_MIN_INTERVAL - 120)
        self.assertEqual(c.power, OFF)
        self.tick(c, (60, False), advance=60)
        self.assertEqual(c.power, ON)
        self.assertEqual(self.switch.calls[-1][0], cut_at + SWITCH_MIN_INTERVAL)

    def test_a_stale_reading_powers_the_dock(self):
        c = self.controller()
        self.tick(c, (85, True))
        self.assertEqual(c.power, OFF)
        for _ in range(READING_MAX_AGE // 60 - 1):
            self.tick(c)  # nothing seen: the last reading still counts
            self.assertEqual(c.power, OFF)
        out = self.tick(c, advance=120)
        self.assertEqual(c.power, ON)
        self.assertIn("no recent battery reading", out)
        self.assertEqual(self.state()["battery"], 85)  # the last reading stays on show, with its time
        self.assertEqual(self.state()["read_at"], self.state()["updated"] - READING_MAX_AGE - 60)

    def test_any_scan_error_is_a_problem_and_the_dock_is_still_decided(self):
        c = self.controller()
        self.tick(c, (90, True))
        self.assertEqual(c.power, OFF)
        for _ in range(READING_MAX_AGE // 60 + 1):
            out = self.tick(c, RuntimeError("No Bluetooth adapters found"))
        self.assertEqual(c.power, ON)  # the last good reading aged out, so the dock is powered
        self.assertEqual(self.state()["problem"], "scan failed: RuntimeError: No Bluetooth adapters found")
        self.assertEqual(out.count("scan failed"), 0)  # printed once, on the first failure

    def test_reads_the_cap_from_settings_each_tick(self):
        self.write_settings(charge_max=70)
        c = self.controller()
        self.tick(c, (72, True))
        self.assertEqual(c.power, OFF)
        self.assertEqual(self.state()["charge_max"], 70)
        self.write_settings(charge_max=90)
        self.tick(c, (72, False), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, ON)
        self.write_settings(charge_max=5)  # below the floor: clamped, not obeyed
        self.tick(c, (72, True), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(self.state()["charge_max"], 50)
        self.assertEqual(c.power, OFF)

    def test_prefers_the_logger_while_it_is_connected(self):
        c = self.controller()
        self.write_logger_status(battery=81, charging=False)
        self.tick(c, (50, True))
        self.assertEqual(c.power, OFF)
        self.assertEqual(self.scans, 0)
        self.assertEqual(self.state()["source"], "logger")
        self.assertEqual(self.adverts, [(50, True)])  # never asked for
        self.tick(c, advance=LOGGER_STATUS_MAX_AGE + 60)  # the logger has gone: back to scanning
        self.assertEqual(self.scans, 1)
        self.assertEqual(self.state()["source"], "advert")

    def test_logger_rows_without_a_charger_flag_keep_the_last_one_unknown(self):
        c = self.controller()
        self.write_logger_status(battery=55)
        self.tick(c)
        self.assertIsNone(self.state()["charging"])

    def test_switch_failure_is_reported_and_retried(self):
        self.switch.fail = True
        c = self.controller()
        out = self.tick(c, (90, True))
        self.assertEqual(c.power, UNKNOWN)
        self.assertIn("could not switch the dock power off: fake switch failure", out)
        self.assertEqual(self.state()["problem"], "could not switch the dock power off: fake switch failure")
        out = self.tick(c, (90, True))
        self.assertEqual(out.count("could not switch"), 0)  # the same problem is printed once
        self.switch.fail = False
        out = self.tick(c, (90, True))
        self.assertEqual(c.power, OFF)
        self.assertIn("the switch works again", out)
        self.assertIsNone(self.state()["problem"])

    def test_scan_failure_is_a_problem_not_a_crash(self):
        c = self.controller()
        out = self.tick(c, SwitchError("bleak is not installed"))
        self.assertIn("bleak is not installed", out)
        self.assertEqual(self.state()["problem"], "bleak is not installed")
        self.assertEqual(c.power, ON)
        self.tick(c, (50, True))  # the next good reading clears it
        self.assertIsNone(self.state()["problem"])
        self.switch.fail = True
        self.tick(c, SwitchError("no bluetooth"), advance=SWITCH_MIN_INTERVAL + READING_MAX_AGE)
        self.assertIn("no bluetooth", self.state()["problem"])
        self.assertIn("could not switch the dock power on", self.state()["problem"])  # both at once
        self.tick(c, (90, True), advance=SWITCH_MIN_INTERVAL)
        self.assertIn("could not switch the dock power off", self.state()["problem"])
        self.assertNotIn("no bluetooth", self.state()["problem"])

    def test_hold_commands(self):
        c = self.controller()
        self.tick(c, (90, True))
        self.assertEqual(c.power, OFF)
        self.paths.cmd.write_text(json.dumps({"cmd": "on", "sent": self.clock.now + 60}))
        out = self.tick(c, (90, True))
        self.assertEqual(c.power, ON)  # a hold doesn't wait for the switch interval
        self.assertFalse(self.paths.cmd.exists())
        self.assertIn("holding the dock power on until", out)
        self.assertEqual(self.state()["hold"], {"power": ON, "until": self.clock.now + HOLD_SECONDS})
        self.tick(c, (95, True), advance=HOLD_SECONDS - 120)
        self.assertEqual(c.power, ON)
        self.tick(c, (95, True), advance=120)  # the hour is over
        self.assertEqual(c.power, OFF)
        self.assertIsNone(self.state()["hold"])
        self.paths.cmd.write_text(json.dumps({"cmd": "off", "sent": self.clock.now + 60}))
        self.tick(c, (40, True))
        self.assertEqual(c.power, OFF)
        self.paths.cmd.write_text(json.dumps({"cmd": "auto", "sent": self.clock.now + 60}))
        self.tick(c, (40, True))
        self.assertEqual(c.power, OFF)  # automatic again, but the switch interval still applies
        self.tick(c, (40, True), advance=SWITCH_MIN_INTERVAL)
        self.assertEqual(c.power, ON)

    def test_bad_and_stale_commands_are_dropped(self):
        c = self.controller()
        self.tick(c, (90, True))
        for content in ("{not json", json.dumps({"cmd": "on", "sent": self.clock.now - 3600}),
                        json.dumps({"cmd": "dance", "sent": self.clock.now + 60}), json.dumps(["on"])):
            self.paths.cmd.write_text(content)
            self.tick(c, (90, True))
            self.assertFalse(self.paths.cmd.exists(), content)
            self.assertEqual(c.power, OFF, content)
            self.assertIsNone(c.hold, content)

    def test_restore_powers_the_dock_on_exit(self):
        c = self.controller()
        self.tick(c, (90, True))
        self.assertEqual(c.power, OFF)
        with redirect_stdout(io.StringIO()):
            c.restore()
        self.assertEqual(c.power, ON)
        self.assertEqual(self.state()["power"], ON)
        self.assertEqual([on for _, on in self.switch.calls], [False, True])

    def test_prints_a_reading_only_when_it_changes(self):
        c = self.controller()
        out = self.tick(c, (64, True))
        self.assertIn("watch battery 64%, charger live (from the advert)", out)
        out = self.tick(c, (64, True))
        self.assertNotIn("watch battery", out)
        out = self.tick(c, (64, False))
        self.assertIn("watch battery 64% (from the advert)", out)


class SwitchConfigTest(unittest.TestCase):
    def test_defaults_to_the_pi_3b_plus_hub(self):
        switch = switch_from_settings(None)
        self.assertIsInstance(switch, UhubctlSwitch)
        self.assertEqual((switch.hub, switch.port), ("1-1", 2))
        switch = switch_from_settings({"watch_charger": {"hub": "1-1.1", "port": 3}})
        self.assertEqual((switch.hub, switch.port), ("1-1.1", 3))
        self.assertEqual(switch_from_settings({"watch_charger": "junk"}).hub, "1-1")

    def test_unknown_switch_is_refused(self):
        with self.assertRaisesRegex(SwitchError, "gpio"):
            switch_from_settings({"watch_charger": {"switch": "gpio"}})


class StatusTest(TempPaths):
    def run_status(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = show_status(self.paths)
        return code, out.getvalue()

    def test_without_state(self):
        code, out = self.run_status()
        self.assertEqual(code, 1)
        self.assertIn("No state yet", out)

    def test_with_state(self):
        c = self.controller()
        self.tick(c, (90, True))
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("Watch battery: 90%, charger live (from the advert at", out)
        self.assertIn("Dock power:    off (cap 80%)", out)
        self.assertNotIn("Problem", out)
