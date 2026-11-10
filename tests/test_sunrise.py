import io
import signal
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sunrise.sunrise import (  # noqa: E402
    AMPS_IDLE_PER_LED, DEFAULT_STRIP, FRAME_REPEATS, RAMP_HOLD_SECONDS, Light, NeoPixelStrip, dither,
    estimate_amps, frame, intensity, level_at, main, ramp_color, strip_config, target_levels,
)

START = 1_792_000_000
FULL = START + 30 * 60
OFF = FULL + 60 * 60


class FakeClock:
    def __init__(self, now, on_sleep=None):
        self.now = now
        self.on_sleep = on_sleep

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


class RecordingStrip:
    def __init__(self, clock, after_show=None):
        self.clock = clock
        self.after_show = after_show
        self.shown = []  # (time, pixels)

    def show(self, pixels):
        self.shown.append((self.clock(), pixels))
        if self.after_show:
            self.after_show(pixels)

    def lit_at(self, t):
        """The pixels showing at time t."""
        current = None
        for when, pixels in self.shown:
            if when > t:
                break
            current = pixels
        return current


def lit(pixels):
    return pixels is not None and any(any(p) for p in pixels)


def mean_level(pixels):
    return sum(sum(p) for p in pixels) / len(pixels)


def run_main(argv, clock, settings=None, after_show=None):
    strip = RecordingStrip(clock.clock, after_show)
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv, clock=clock.clock, sleep=clock.sleep, settings=settings or {}, strip=strip)
    return code, strip, out.getvalue()


class ColourTest(unittest.TestCase):
    def test_endpoints(self):
        r, g, b, w = ramp_color(0.0, rgbw=True)
        self.assertEqual((g, b, w), (0, 0, 0))
        self.assertGreater(r, 200)
        r, g, b, w = ramp_color(1.0, rgbw=True)
        self.assertEqual(w, 255)
        self.assertGreater(w, r + b)
        self.assertGreater(r, g)
        self.assertGreater(g, b)
        r, g, b = ramp_color(1.0, rgbw=False)
        self.assertEqual(r, 255)
        self.assertGreater(g, 100)
        self.assertLess(b, g / 2)

    def test_stays_in_range(self):
        for rgbw in (True, False):
            for i in range(-5, 106):
                self.assertTrue(all(0 <= c <= 255 for c in ramp_color(i / 100, rgbw)))

    def test_intensity_rises_slowly_then_reaches_full(self):
        self.assertEqual(intensity(0), 0)
        self.assertEqual(intensity(1), 1)
        self.assertLess(intensity(0.5), 0.3)
        values = [intensity(i / 100) for i in range(101)]
        self.assertEqual(values, sorted(values))

    def test_frame_brightness_never_drops_during_ramp(self):
        config = dict(DEFAULT_STRIP)
        for order in ("GRBW", "RGB"):
            config["pixel_order"] = order
            totals = [sum(target_levels(i / 200, 100, config)) for i in range(201)]
            self.assertEqual(totals, sorted(totals))


class CurrentTest(unittest.TestCase):
    def test_full_strip_never_over_max_amps(self):
        for order in ("GRBW", "RGB"):
            for max_amps in (0.5, 1.0, 2.0, 3.0):
                config = {"leds": 60, "pixel_order": order, "max_amps": max_amps}
                for i in range(101):
                    self.assertLessEqual(estimate_amps(frame(i / 100, 100, config)), max_amps)

    def test_cap_bites_on_a_long_strip(self):
        config = {"leds": 300, "pixel_order": "GRBW", "max_amps": 3.0}
        pixels = frame(1.0, 100, config)
        self.assertLessEqual(estimate_amps(pixels), 3.0)
        self.assertGreater(estimate_amps(pixels), 2.8)

    def test_full_peak_is_the_keyframe_when_it_fits(self):
        self.assertEqual(frame(1.0, 100, dict(DEFAULT_STRIP))[0], (170, 75, 5, 255))

    def test_brightness_scales_every_channel(self):
        config = dict(DEFAULT_STRIP)
        full = target_levels(1.0, 100, config)
        half = target_levels(1.0, 50, config)
        tenth = target_levels(1.0, 10, config)
        for f, h, t in zip(full, half, tenth):
            self.assertAlmostEqual(h, f / 2)
            self.assertAlmostEqual(t, f / 10)


class DitherTest(unittest.TestCase):
    def test_mean_matches_fractional_target(self):
        for leds in (1, 7, 60):
            for level in (0.0, 0.03, 0.25, 0.5, 1.37, 12.9, 254.6, 255.0):
                pixels = dither((level,), leds)
                self.assertLessEqual(abs(mean_level(pixels) - level), 0.5 / leds + 1e-9, (leds, level))
                self.assertTrue(all(int(level) <= p[0] <= min(255, int(level) + 1) for p in pixels))

    def test_raised_leds_are_spread_out(self):
        pixels = dither((0.25,), 60)
        raised = [i for i, p in enumerate(pixels) if p[0] == 1]
        self.assertEqual(len(raised), 15)
        self.assertEqual({b - a for a, b in zip(raised, raised[1:])}, {4})

    def test_low_end_is_smooth(self):
        config = dict(DEFAULT_STRIP)
        means = [sum(p[0] for p in frame(i / 600, 100, config)) / 60 for i in range(61)]
        steps = [b - a for a, b in zip(means, means[1:])]
        self.assertGreater(means[-1], 1.5)
        self.assertLess(max(steps), 0.2)


class ExtremeConfigTest(unittest.TestCase):
    CONFIGS = [{"leds": 1000, "pixel_order": "GRBW", "max_amps": 0.5},
               {"leds": 1000, "pixel_order": "GRBW", "max_amps": 1.0},
               {"leds": 1000, "pixel_order": "RGB", "max_amps": 1.0},
               {"leds": 1, "pixel_order": "GRBW", "max_amps": 20.0}]

    def test_frames_stay_within_budget(self):
        for config in self.CONFIGS:
            idle = config["leds"] * AMPS_IDLE_PER_LED
            for i in range(0, 101, 5):
                pixels = frame(i / 100, 100, config)
                self.assertEqual(len(pixels), config["leds"])
                self.assertLessEqual(estimate_amps(pixels), max(config["max_amps"], idle), config)

    def test_strip_that_cannot_light_says_so(self):
        settings = {"sunrise": {"leds": 1000, "max_amps": 1.0}}
        for argv in (["test"], ["ramp", "--minutes", "1"],
                     ["run", "--start", str(START), "--full", str(FULL), "--off", str(OFF), "--brightness", "50"]):
            code, strip, out = run_main(argv, FakeClock(START), settings)
            self.assertEqual(code, 2)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertIn("can't light", out)
        code, strip, _ = run_main(["off"], FakeClock(START), settings)
        self.assertEqual(code, 0)
        self.assertFalse(lit(strip.shown[-1][1]))

    def test_one_led_at_twenty_amps_reaches_its_peak(self):
        config = self.CONFIGS[-1]
        self.assertEqual(frame(1.0, 100, config), [(170, 75, 5, 255)])


class LevelAtTest(unittest.TestCase):
    def test_phases(self):
        self.assertIsNone(level_at(START - 1, START, FULL, OFF))
        self.assertEqual(level_at(START, START, FULL, OFF), 0.0)
        self.assertAlmostEqual(level_at(START + 15 * 60, START, FULL, OFF), 0.5)
        self.assertEqual(level_at(FULL, START, FULL, OFF), 1.0)
        self.assertEqual(level_at(FULL + 30 * 60, START, FULL, OFF), 1.0)
        self.assertIsNone(level_at(OFF, START, FULL, OFF))
        self.assertIsNone(level_at(OFF + 3600, START, FULL, OFF))

    def test_zero_length_ramp_goes_straight_to_peak(self):
        self.assertEqual(level_at(START, START, START, OFF), 1.0)


class StripConfigTest(unittest.TestCase):
    def test_defaults(self):
        for settings in (None, {}, {"sunrise": None}, {"sunrise": "big"}, []):
            self.assertEqual(strip_config(settings), DEFAULT_STRIP)

    def test_good_values(self):
        config = strip_config({"sunrise": {"leds": 144, "pixel_order": "rgb", "max_amps": 8}})
        self.assertEqual(config, {"leds": 144, "pixel_order": "RGB", "max_amps": 8.0})

    def test_bad_values_fall_back(self):
        for raw in ({"leds": 0}, {"leds": 1001}, {"leds": 2.5}, {"leds": "60"}, {"leds": True},
                    {"pixel_order": "BGR"}, {"pixel_order": 4}, {"max_amps": 0.4}, {"max_amps": 21},
                    {"max_amps": "3"}, {"max_amps": float("nan")}, {"max_amps": False}):
            self.assertEqual(strip_config({"sunrise": raw}), DEFAULT_STRIP, raw)

    def test_one_bad_value_keeps_the_others(self):
        config = strip_config({"sunrise": {"leds": 30, "pixel_order": "XYZ"}})
        self.assertEqual(config, {"leds": 30, "pixel_order": "GRBW", "max_amps": 3.0})


class RunTest(unittest.TestCase):
    def run_args(self, brightness=100):
        return ["run", "--start", str(START), "--full", str(FULL), "--off", str(OFF),
                "--brightness", str(brightness)]

    def test_full_night(self):
        clock = FakeClock(START - 120)
        code, strip, out = run_main(self.run_args(), clock)
        self.assertEqual(code, 0)
        self.assertFalse(lit(strip.lit_at(START - 1)))
        self.assertFalse(lit(strip.lit_at(START)))
        quarter, half = mean_level(strip.lit_at(START + 450)), mean_level(strip.lit_at(START + 900))
        self.assertTrue(0 < quarter < half < mean_level(strip.lit_at(FULL)))
        self.assertEqual(strip.lit_at(FULL), strip.lit_at(OFF - 1))
        self.assertEqual(strip.lit_at(FULL)[0], (170, 75, 5, 255))
        self.assertFalse(lit(strip.shown[-1][1]))
        self.assertGreaterEqual(strip.shown[-1][0], OFF)
        self.assertLess(clock.now, OFF + 5)
        for word in ("waiting", "ramp", "hold", "off"):
            self.assertIn(word, out)

    def test_restart_mid_ramp_resumes(self):
        clock = FakeClock(START + 900)
        _, strip, out = run_main(self.run_args(), clock)
        first = run_main(self.run_args(), FakeClock(START - 5))[1]
        self.assertEqual(strip.shown[0][1], first.lit_at(START + 900))
        self.assertNotIn("waiting", out)

    def test_after_off_exits_at_once(self):
        code, strip, _ = run_main(self.run_args(), FakeClock(OFF + 10))
        self.assertEqual(code, 0)
        self.assertEqual(len(strip.shown), 1)
        self.assertFalse(lit(strip.shown[0][1]))

    def test_brightness_halves_the_peak(self):
        _, strip, _ = run_main(self.run_args(50), FakeClock(FULL))
        self.assertEqual(strip.shown[0][1][0][3], 128)

    def test_bad_times(self):
        args = ["run", "--start", str(FULL), "--full", str(START), "--off", str(OFF), "--brightness", "50"]
        code, strip, out = run_main(args, FakeClock(START))
        self.assertEqual(code, 2)
        self.assertEqual(strip.shown, [])

    def test_sigterm_turns_strip_off(self):
        def kill(now):
            if now >= START + 600:
                signal.raise_signal(signal.SIGTERM)

        previous = signal.getsignal(signal.SIGTERM)
        clock = FakeClock(START, on_sleep=kill)
        code, strip, out = run_main(self.run_args(), clock)
        self.assertEqual(code, 0)
        self.assertIn("stopped", out)
        self.assertTrue(lit(strip.shown[-2][1]))
        self.assertFalse(lit(strip.shown[-1][1]))
        self.assertLess(clock.now, START + 602)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_signal_during_a_write_still_ends_off(self):
        def kill_on_lit(pixels):
            if lit(pixels):
                signal.raise_signal(signal.SIGTERM)

        code, strip, out = run_main(self.run_args(), FakeClock(START - 3), after_show=kill_on_lit)
        self.assertEqual(code, 0)
        self.assertEqual([lit(pixels) for _, pixels in strip.shown], [False, True, False])

    def test_second_signal_during_cleanup(self):
        def kill_every_write(pixels):
            signal.raise_signal(signal.SIGTERM)

        previous = signal.getsignal(signal.SIGTERM)
        code, strip, out = run_main(self.run_args(), FakeClock(START + 600), after_show=kill_every_write)
        self.assertEqual(code, 0)
        self.assertFalse(lit(strip.shown[-1][1]))
        self.assertEqual(out.count("stopped"), 1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_frames_are_tuples_of_ints(self):
        for order in ("GRBW", "RGB"):
            settings = {"sunrise": {"pixel_order": order}}
            _, strip, _ = run_main(["ramp", "--minutes", "1"], FakeClock(START), settings)
            for _, pixels in strip.shown:
                for p in pixels:
                    self.assertIs(type(p), tuple)
                    self.assertEqual(len(p), 4 if order == "GRBW" else 3)
                    self.assertTrue(all(type(c) is int for c in p))

    def test_sigint_turns_strip_off(self):
        clock = FakeClock(START + 600, on_sleep=lambda now: signal.raise_signal(signal.SIGINT))
        code, strip, _ = run_main(self.run_args(), clock)
        self.assertEqual(code, 0)
        self.assertFalse(lit(strip.shown[-1][1]))


class CommandTest(unittest.TestCase):
    def test_ramp_minutes_compresses_the_ramp(self):
        clock = FakeClock(START)
        code, strip, _ = run_main(["ramp", "--minutes", "1"], clock, settings={"sunrise_brightness": 100})
        self.assertEqual(code, 0)
        night = run_main(RunTest().run_args(), FakeClock(START))[1]
        self.assertEqual(strip.lit_at(START + 30), night.lit_at(START + 15 * 60))
        self.assertEqual(strip.lit_at(START + 60)[0], (170, 75, 5, 255))
        self.assertTrue(lit(strip.lit_at(START + 60 + RAMP_HOLD_SECONDS - 1)))
        self.assertFalse(lit(strip.shown[-1][1]))
        self.assertLess(clock.now, START + 60 + RAMP_HOLD_SECONDS + 2)

    def test_ramp_uses_saved_brightness(self):
        clock = FakeClock(START)
        _, strip, _ = run_main(["ramp", "--minutes", "1"], clock, settings={"sunrise_brightness": 50})
        self.assertEqual(strip.lit_at(START + 60)[0][3], 128)

    def test_test_is_dim_then_off(self):
        clock = FakeClock(START)
        code, strip, _ = run_main(["test"], clock)
        self.assertEqual(code, 0)
        self.assertEqual(len(strip.shown), 2)
        dim = strip.shown[0][1]
        self.assertTrue(lit(dim))
        self.assertLess(max(max(p) for p in dim), 40)
        self.assertFalse(lit(strip.shown[1][1]))
        self.assertEqual(clock.now, START + 5)

    def test_off(self):
        code, strip, out = run_main(["off"], FakeClock(START))
        self.assertEqual(code, 0)
        self.assertEqual(len(strip.shown), 1)
        self.assertFalse(lit(strip.shown[0][1]))

    def test_rgb_strip_gets_three_channels(self):
        _, strip, _ = run_main(["off"], FakeClock(START), settings={"sunrise": {"pixel_order": "GRB", "leds": 10}})
        self.assertEqual(strip.shown[0][1], [(0, 0, 0)] * 10)

    def test_unopenable_strip_exits_2(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sunrise.sunrise.NeoPixelStrip", side_effect=OSError("no SPI")):
            code = main(["off"], settings={})
        self.assertEqual(code, 2)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_fake_strip_prints_colour_amps_and_time(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["test", "--fake"], clock=lambda: START, sleep=lambda s: None, settings={})
        self.assertEqual(code, 0)
        first = out.getvalue().splitlines()[1]
        self.assertRegex(first, r"^\d{1,2}:\d\d:\d\d [AP]M  rgbw [\d. ]+  \d+\.\d\d A$")


class NeoPixelStripTest(unittest.TestCase):
    def fake_modules(self):
        made = []

        class FakeNeoPixel:
            def __init__(self, spi, n, pixel_order=None, auto_write=True):
                self.spi, self.n, self.pixel_order, self.auto_write = spi, n, pixel_order, auto_write
                self.buf = [None] * n
                self.shows = 0
                made.append(self)

            def __setitem__(self, i, value):
                self.buf[i] = value

            def show(self):
                self.shows += 1

        board = types.ModuleType("board")
        board.SPI = lambda: "spi0"
        neopixel_spi = types.ModuleType("neopixel_spi")
        neopixel_spi.GRBW, neopixel_spi.RGB = "GRBW", "RGB"
        neopixel_spi.NeoPixel_SPI = FakeNeoPixel
        return {"board": board, "neopixel_spi": neopixel_spi}, made

    def test_opens_and_writes(self):
        modules, made = self.fake_modules()
        with mock.patch.dict(sys.modules, modules):
            light = Light(NeoPixelStrip(dict(DEFAULT_STRIP), sleep=lambda s: None), dict(DEFAULT_STRIP))
        (np,) = made
        self.assertEqual((np.spi, np.n, np.pixel_order, np.auto_write), ("spi0", 60, "GRBW", False))
        light.show(frame(0.5, 100, DEFAULT_STRIP))
        self.assertEqual(np.shows, FRAME_REPEATS)
        self.assertTrue(all(type(p) is tuple and len(p) == 4 and all(type(c) is int for c in p) for p in np.buf))
        light.show(frame(0.5, 100, DEFAULT_STRIP))
        self.assertEqual(np.shows, FRAME_REPEATS)
        light.show(frame(1.0, 100, DEFAULT_STRIP))
        self.assertEqual(np.shows, 2 * FRAME_REPEATS)
        self.assertEqual(np.buf[0], (170, 75, 5, 255))
        light.off()
        self.assertEqual(np.shows, 3 * FRAME_REPEATS)
        self.assertEqual(np.buf, [(0, 0, 0, 0)] * 60)

    def test_main_drives_the_real_strip_class(self):
        modules, made = self.fake_modules()
        out = io.StringIO()
        with mock.patch.dict(sys.modules, modules), redirect_stdout(out):
            code = main(["test"], clock=lambda: START, sleep=lambda s: None, settings={})
        self.assertEqual(code, 0)
        self.assertEqual(made[0].shows, 2 * FRAME_REPEATS)
        self.assertEqual(made[0].buf, [(0, 0, 0, 0)] * 60)


if __name__ == "__main__":
    unittest.main()
