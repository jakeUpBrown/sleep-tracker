#!/usr/bin/env python3
"""Sunrise light: ramp an addressable LED strip from deep red to warm white.

Usage:
  python -u sunrise.py run --start UNIX --full UNIX --off UNIX --brightness PCT [--fake]
                                       off before start, ramp to full, hold at peak, off at off
  python sunrise.py test [--fake]      dim warm white for 5 s, then off
  python sunrise.py ramp [--minutes N] [--fake]
                                       the whole ramp in N minutes (default 30), hold 10 s, then off
  python sunrise.py off [--fake]       strip off

run is a function of the clock alone, so restarting it with the same arguments carries on where it was.
SIGTERM or SIGINT turns the strip off and exits 0; a strip that can't be opened exits 2. --fake prints
the colour and estimated current instead of driving the strip. The strip is set by the optional
"sunrise" object in ~/sleeptracker/settings.json: {"leds": 60, "pixel_order": "GRBW", "max_amps": 3.0}.
"""
import argparse
import json
import math
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import SUNRISE_MIN_BRIGHTNESS, SUNRISE_RAMP_MINUTES, clean_settings  # noqa: E402

SETTINGS = Path.home() / "sleeptracker" / "settings.json"
DEFAULT_STRIP = {"leds": 60, "pixel_order": "GRBW", "max_amps": 3.0}
PIXEL_ORDERS = ("GRBW", "RGBW", "GRB", "RGB")
MAX_LEDS = 1000
MIN_AMPS, MAX_AMPS = 0.5, 20.0

AMPS_PER_CHANNEL = 0.020  # one channel at 255
AMPS_IDLE_PER_LED = 0.001
UPDATE_SECONDS = 1
INTENSITY_EXPONENT = 2.2  # luminance = progress^2.2 looks like a steady rise to the eye
TEST_BRIGHTNESS = 10
TEST_SECONDS = 5
RAMP_HOLD_SECONDS = 10
FRAME_REPEATS = 5  # the strip now and then misses a frame, and one missed "off" would leave it lit
REPEAT_GAP_SECONDS = 0.05

# (progress, colour at full intensity); the intensity curve does the dimming
RGBW_KEYS = [
    (0.00, (255, 0, 0, 0)),       # deep red
    (0.30, (255, 40, 0, 0)),      # red-orange
    (0.55, (255, 100, 5, 20)),    # orange
    (0.80, (230, 120, 15, 130)),  # amber
    (1.00, (170, 75, 5, 255)),    # warm white, mostly the W LED
]
RGB_KEYS = [
    (0.00, (255, 0, 0)),
    (0.30, (255, 40, 0)),
    (0.55, (255, 95, 5)),
    (0.80, (255, 135, 25)),
    (1.00, (255, 160, 50)),       # warm white mixed from red and green
]


class Stop(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def clock12(unix: float, seconds: bool = False) -> str:
    return datetime.fromtimestamp(unix).strftime("%I:%M:%S %p" if seconds else "%I:%M %p").lstrip("0")


def load_settings(path: Path = SETTINGS) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def strip_config(settings) -> dict:
    """The "sunrise" block of settings.json; each missing or bad value takes its default."""
    raw = settings.get("sunrise") if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    config = dict(DEFAULT_STRIP)
    leds = raw.get("leds")
    if _number(leds) and leds == int(leds) and 1 <= leds <= MAX_LEDS:
        config["leds"] = int(leds)
    order = raw.get("pixel_order")
    if isinstance(order, str) and order.upper() in PIXEL_ORDERS:
        config["pixel_order"] = order.upper()
    amps = raw.get("max_amps")
    if _number(amps) and MIN_AMPS <= amps <= MAX_AMPS:
        config["max_amps"] = float(amps)
    return config


def is_rgbw(config: dict) -> bool:
    return "W" in config["pixel_order"]


def ramp_color(progress: float, rgbw: bool) -> tuple:
    """The colour at this point of the ramp, at full intensity."""
    keys = RGBW_KEYS if rgbw else RGB_KEYS
    progress = min(1.0, max(0.0, progress))
    for (p0, c0), (p1, c1) in zip(keys, keys[1:]):
        if progress <= p1:
            t = (progress - p0) / (p1 - p0)
            return tuple(a + (b - a) * t for a, b in zip(c0, c1))
    return tuple(float(c) for c in keys[-1][1])


def intensity(progress: float) -> float:
    return min(1.0, max(0.0, progress)) ** INTENSITY_EXPONENT


def level_at(now: float, start: float, full: float, off: float):
    """Ramp progress 0..1 (1 while holding), or None when the strip should be off."""
    if now < start or now >= off:
        return None
    if now >= full:
        return 1.0
    return (now - start) / (full - start)


def estimate_amps(pixels) -> float:
    return sum(sum(p) for p in pixels) / 255 * AMPS_PER_CHANNEL + len(pixels) * AMPS_IDLE_PER_LED


def channel_budget(config: dict) -> float:
    """Amps left for the colour channels once idle current and dithering's extra step are counted."""
    leds, channels = config["leds"], 4 if is_rgbw(config) else 3
    return config["max_amps"] - leds * AMPS_IDLE_PER_LED - leds * channels / 255 * AMPS_PER_CHANNEL


def cap_scale(levels, config: dict) -> float:
    """Factor (0..1) that keeps every LED at these levels within max_amps."""
    budget = channel_budget(config)
    if budget <= 0:
        return 0.0
    draw = sum(levels) * config["leds"] / 255 * AMPS_PER_CHANNEL
    return 1.0 if draw <= budget else budget / draw


def target_levels(progress: float, brightness: int, config: dict) -> tuple:
    """Per-channel levels (fractional, 0..255) shared by every LED."""
    rgbw = is_rgbw(config)
    peak_scale = cap_scale(ramp_color(1.0, rgbw), config)  # 100% brightness is the safe peak
    k = intensity(progress) * brightness / 100 * peak_scale
    levels = tuple(c * k for c in ramp_color(progress, rgbw))
    scale = cap_scale(levels, config)
    return tuple(min(255.0, c * scale) for c in levels)


def dither(levels, leds: int) -> list:
    """Spread each channel's fraction over the strip: evenly spaced LEDs one step higher, so the mean
    matches the fractional level and the dim end rises smoothly instead of in visible 8-bit steps."""
    pixels = [[0] * len(levels) for _ in range(leds)]
    for ch, level in enumerate(levels):
        base = int(level)
        frac = level - base
        for i in range(leds):
            extra = math.floor((i + 1) * frac + 0.5) - math.floor(i * frac + 0.5)
            pixels[i][ch] = min(255, base + extra)
    return [tuple(p) for p in pixels]


def frame(progress, brightness: int, config: dict) -> list:
    channels = 4 if is_rgbw(config) else 3
    if progress is None:
        return [(0,) * channels] * config["leds"]
    return dither(target_levels(progress, brightness, config), config["leds"])


class FakeStrip:
    def __init__(self, config: dict, clock=time.time):
        self.config = config
        self.clock = clock

    def show(self, pixels) -> None:
        n = len(pixels)
        means = " ".join(f"{sum(p[ch] for p in pixels) / n:.1f}" for ch in range(len(pixels[0])))
        name = "rgbw" if len(pixels[0]) == 4 else "rgb"
        say(f"{clock12(self.clock(), seconds=True)}  {name} {means}  {estimate_amps(pixels):.2f} A")


class NeoPixelStrip:
    def __init__(self, config: dict, sleep=time.sleep):
        import board
        import neopixel_spi
        self.sleep = sleep
        order = getattr(neopixel_spi, config["pixel_order"], config["pixel_order"])
        self.pixels = neopixel_spi.NeoPixel_SPI(board.SPI(), config["leds"], pixel_order=order,
                                                auto_write=False)

    def show(self, pixels) -> None:
        for i, p in enumerate(pixels):
            self.pixels[i] = p
        for _ in range(FRAME_REPEATS):
            self.pixels.show()
            self.sleep(REPEAT_GAP_SECONDS)


def open_strip(config: dict, fake: bool, clock=time.time, sleep=time.sleep):
    return FakeStrip(config, clock) if fake else NeoPixelStrip(config, sleep)


class Light:
    """A strip that is only written when the frame changes, and knows how to switch off."""

    def __init__(self, strip, config: dict):
        self.strip = strip
        self.config = config
        self.last = None

    def show(self, pixels) -> None:
        if pixels != self.last:
            self.last = None  # a write cut short by a signal leaves the strip in an unknown state
            self.strip.show(pixels)
            self.last = pixels

    def off(self) -> None:
        self.show(frame(None, 0, self.config))


def run(light: Light, start: float, full: float, off: float, brightness: int, clock, sleep) -> int:
    phase = None
    while True:
        now = clock()
        if now >= off:
            break
        progress = level_at(now, start, full, off)
        current = "wait" if progress is None else "hold" if now >= full else "ramp"
        if current != phase:
            phase = current
            say({"wait": f"waiting: sunrise starts at {clock12(start)}",
                 "ramp": f"ramp: {brightness}% by {clock12(full)}",
                 "hold": f"hold: {brightness}% until {clock12(off)}"}[current])
        light.show(frame(progress, brightness, light.config))
        sleep(UPDATE_SECONDS)
    light.off()
    say("off")
    return 0


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def ignore_stop_signals() -> None:
    for s in STOP_SIGNALS:
        signal.signal(s, signal.SIG_IGN)


def stop_on_signal(signum, _frame):
    ignore_stop_signals()  # systemd and wake.py may both send SIGTERM; the second must not cut the cleanup
    raise Stop(signum)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Sunrise light on the LED strip")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="off before start, ramp to full, hold at peak, off at off")
    p_run.add_argument("--start", type=float, required=True)
    p_run.add_argument("--full", type=float, required=True)
    p_run.add_argument("--off", type=float, required=True)
    p_run.add_argument("--brightness", type=int, required=True, help="percent of the safe maximum")
    p_test = sub.add_parser("test", help="dim warm white for 5 s, then off")
    p_ramp = sub.add_parser("ramp", help="the whole ramp in N minutes, hold 10 s, then off")
    p_ramp.add_argument("--minutes", type=float, default=SUNRISE_RAMP_MINUTES)
    p_off = sub.add_parser("off", help="strip off")
    for p in (p_run, p_test, p_ramp, p_off):
        p.add_argument("--fake", action="store_true", help="print the colour instead of driving the strip")
    return parser.parse_args(argv)


def main(argv=None, clock=time.time, sleep=time.sleep, settings=None, strip=None) -> int:
    args = parse_args(argv)
    settings = load_settings() if settings is None else settings
    config = strip_config(settings)
    if args.command == "run" and not args.start <= args.full <= args.off:
        say("bad times: need start <= full <= off")
        return 2
    if args.command == "ramp" and args.minutes <= 0:
        say("bad --minutes: must be more than 0")
        return 2
    if args.command != "off" and channel_budget(config) <= 0:
        say(f"can't light {config['leds']} LEDs within max_amps {config['max_amps']:g} A: "
            f"their idle current alone uses it up")
        return 2
    try:
        strip = strip or open_strip(config, args.fake, clock, sleep)
    except Exception as e:  # ImportError off the Pi, OSError/RuntimeError/ValueError from SPI
        say(f"can't open the LED strip: {type(e).__name__}: {e}")
        return 2
    light = Light(strip, config)
    previous = {s: signal.signal(s, stop_on_signal) for s in STOP_SIGNALS}
    try:
        return command(args, light, settings, clock, sleep)
    except Stop:
        say("stopped")
        return 0
    finally:
        ignore_stop_signals()
        light.off()
        for s, handler in previous.items():
            signal.signal(s, handler)


def command(args, light: Light, settings: dict, clock, sleep) -> int:
    if args.command == "run":
        brightness = min(100, max(SUNRISE_MIN_BRIGHTNESS, args.brightness))
        return run(light, args.start, args.full, args.off, brightness, clock, sleep)
    if args.command == "ramp":
        start = clock()
        full = start + args.minutes * 60
        return run(light, start, full, full + RAMP_HOLD_SECONDS, clean_settings(settings)["sunrise_brightness"],
                   clock, sleep)
    if args.command == "test":
        say(f"test: dim warm white for {TEST_SECONDS} s")
        light.show(frame(1.0, TEST_BRIGHTNESS, light.config))
        sleep(TEST_SECONDS)
    light.off()
    say("off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
