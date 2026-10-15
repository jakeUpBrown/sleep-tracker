#!/usr/bin/env python3
"""Make a fake but plausible night of watch data, for trying the analysis and the web app.

Usage:
  python analysis/make_test_night.py --date 2026-10-13 --out DIR
         [--seed N] [--profile normal|restless|short] [--turnover-at HH:MM]

Writes DIR/watch-DATE.csv in the watch logger's exact format (DATE is the evening the night
starts). The same seed always gives the same night. --turnover-at plants a position change
at that time, e.g. inside the smart wake window.
"""
import argparse
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import Row, alarm_unix_for_night, cycle_stages  # noqa: E402

HEADER = "time,watch_unix,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery,pi_unix"

PROFILES = {
    # bed: minutes after midnight at the start of the evening date (over 1440 = after midnight)
    "normal": {"bed": 23 * 60 + 30, "sleep_hours": 7.5, "awakenings": (1, 2), "extra_turns": (1, 3)},
    "restless": {"bed": 23 * 60 + 30, "sleep_hours": 7.0, "awakenings": (4, 6), "extra_turns": (6, 10)},
    "short": {"bed": 24 * 60 + 45, "sleep_hours": 5.0, "awakenings": (1, 1), "extra_turns": (0, 2)},
}
# heart rate per stage: target range for a stretch, row-to-row jitter
STAGE_HR = {"deep": (49, 53, 0.6), "light": (55, 58, 1.0), "rem": (60, 66, 3.0), "awake": (64, 72, 2.5)}
# still-body movement per 10 s row: mean, noise, chance of a small twitch. The mean is mostly the
# accelerometer's noise floor: a real still body reads about 2.0 per row (12 per minute)
STAGE_MOV = {"deep": (1.95, 0.1, 0.0), "light": (2.15, 0.15, 0.015), "rem": (2.05, 0.15, 0.03)}
# no random turnovers or brief awakenings this soon after falling asleep, so the planned onset
# is the one the detector's still-minute rule can find
SETTLE_SECONDS = 15 * 60


def clock(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0")


# ---------- the night's structure ----------

def _overlay(stages: list, start: int, end: int, stage: str) -> list:
    out = []
    for s, e, st in stages:
        if e <= start or s >= end:
            out.append([s, e, st])
            continue
        if s < start:
            out.append([s, start, st])
        if e > end:
            out.append([end, e, st])
    out.append([start, end, stage])
    return sorted(out)


def _random_time_in(rng: random.Random, stages: list, stage: str, margin: int, earliest: int) -> int | None:
    spans = [(max(s, earliest), e) for s, e, st in stages if st == stage]
    spans = [(s, e) for s, e in spans if e - s > 2 * margin]
    if not spans:
        return None
    s, e = rng.choice(spans)
    return rng.randint(s + margin, e - margin)


def plan_night(date: str, rng: random.Random, profile: str) -> dict:
    p = PROFILES[profile]
    evening = datetime.strptime(date, "%Y-%m-%d")
    in_bed = int((evening + timedelta(minutes=p["bed"] + rng.randint(-10, 10))).timestamp()) + rng.randint(0, 9)
    onset = in_bed + rng.randint(15, 25) * 60
    final_wake = onset + int(p["sleep_hours"] * 60 + rng.randint(-15, 15)) * 60
    end = final_wake + rng.randint(15, 25) * 60

    cycles = []
    start = onset
    while start < final_wake:
        stop = min(start + rng.randint(80, 100) * 60, final_wake)
        if final_wake - stop < 20 * 60:
            stop = final_wake  # no stub cycle at the end
        cycles.append([start, stop])
        start = stop
    stages = [[in_bed, onset, "awake"]]
    for i, (a, b) in enumerate(cycles):
        stages += cycle_stages(a, b, i)
    stages.append([final_wake, end, "awake"])

    boundaries = [c[0] for c in cycles[1:]]
    wanted = rng.randint(*p["awakenings"])
    starts = rng.sample(boundaries, min(wanted, len(boundaries)))
    settled = onset + SETTLE_SECONDS
    while len(starts) < wanted:
        t = _random_time_in(rng, stages, "light", 6 * 60, settled)
        if t is None:
            break
        starts.append(t)
    awakenings = []
    for s in sorted(starts):
        span = [s, s + rng.randint(2, 5) * 60]
        if span[1] < final_wake and all(span[0] > a[1] + 600 or span[1] < a[0] - 600 for a in awakenings):
            awakenings.append(span)
    for a, b in awakenings:
        stages = _overlay(stages, a, b, "awake")

    taken = {a for a, _ in awakenings}
    turnovers = [b + rng.randint(-60, 60) for b in boundaries if b not in taken and rng.random() < 0.8]
    for _ in range(rng.randint(*p["extra_turns"])):
        t = _random_time_in(rng, stages, "light", 2 * 60, settled)
        if t is not None:
            turnovers.append(t)
    return {
        "in_bed": in_bed,
        "onset": onset,
        "final_wake": final_wake,
        "end": end,
        "cycles": cycles,
        "awakenings": awakenings,
        "turnovers": sorted(turnovers),
        "stages": stages,
    }


# ---------- rows ----------

def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    return tuple(c / length for c in v)


def _rotate(v, degrees: float, rng: random.Random):
    """v turned by exactly `degrees` in a random direction."""
    while True:
        r = (rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        dot = sum(a * b for a, b in zip(r, v))
        u = tuple(a - dot * b for a, b in zip(r, v))
        if sum(c * c for c in u) > 1e-6:
            break
    u = _normalize(u)
    a = math.radians(degrees)
    return tuple(math.cos(a) * x + math.sin(a) * y for x, y in zip(v, u))


def _make_rows(events: dict, rng: random.Random, planted: int | None) -> list[Row]:
    stages = events["stages"]
    turn_times = sorted(events["turnovers"] + ([planted] if planted is not None else []))
    posture = _normalize((rng.uniform(-0.3, 0.3), rng.uniform(-0.9, -0.4), rng.uniform(0.3, 0.9)))
    battery_start = rng.uniform(85, 100)
    drain = rng.uniform(1.2, 1.8)  # % per hour
    hr_level = 68.0
    hr_bump = 0.0
    targets = {}
    turn = None  # (new posture, rows left, planted)
    rows = []
    stage_index = 0
    for t in range(events["in_bed"] + 10, events["end"] + 1, 10):
        while stage_index < len(stages) - 1 and stages[stage_index][1] <= t - 5:
            stage_index += 1
        stage = stages[stage_index][2]

        while turn_times and turn_times[0] <= t:
            when = turn_times.pop(0)
            if when > t - 10:
                strong = when == planted
                turn = (_rotate(posture, rng.uniform(40, 60) if strong else rng.uniform(20, 60), rng), 2, strong)

        noise = 0.005
        if turn is not None:
            new, left, strong = turn
            vector = _normalize(tuple(a + b for a, b in zip(posture, new))) if left == 2 else new
            mov = rng.uniform(15, 30) if strong else rng.uniform(5, 30)
            mov_max = rng.uniform(0.5, 1.5)
            noise = 0.03
            hr_bump = max(hr_bump, rng.uniform(3, 7))
            if left == 2:
                turn = (new, 1, strong)
            else:
                posture, turn = new, None
        elif stage == "awake":
            if rng.random() < 0.8:
                mov, mov_max, noise = rng.uniform(3, 15), rng.uniform(0.2, 1.2), 0.03
                posture = _rotate(posture, rng.uniform(20, 40) if rng.random() < 0.08 else rng.uniform(0, 8), rng)
            else:
                mov, mov_max = rng.uniform(1.0, 3.0), rng.uniform(0.05, 0.2)
            vector = posture
        else:
            mean, spread, twitch = STAGE_MOV[stage]
            mov = max(0.4, rng.gauss(mean, spread))
            if rng.random() < twitch:
                mov += rng.uniform(0.5, 1.5)
            mov_max = mov / 125 * rng.uniform(2.5, 4.0)
            vector = posture

        if stage_index not in targets:
            low, high, _ = STAGE_HR[stage]
            targets[stage_index] = rng.uniform(low, high)
        hr_level += (targets[stage_index] - hr_level) * 0.12  # heart rate lags the stage by about a minute
        hr_value = hr_level + hr_bump + rng.gauss(0, STAGE_HR[stage][2])
        hr_bump *= 0.8
        roll = rng.random()
        if roll < 0.01:
            hr, conf = None, 0.0
        elif roll < 0.08:  # poor contact: a reading is still there but not trustworthy
            hr, conf = float(round(hr_value + rng.uniform(-15, 15))), float(rng.randint(10, 45))
        else:
            hr, conf = float(round(hr_value)), float(rng.randint(60, 100))

        ax, ay, az = (round(c + rng.gauss(0, noise), 2) for c in vector)
        battery = float(round(battery_start - drain * (t - events["in_bed"]) / 3600))
        rows.append(Row(t, rng.choice((124, 125, 125, 125, 126)), round(mov, 3), round(mov_max, 3),
                        ax, ay, az, hr, conf, battery))
    return rows


def generate(date: str, seed: int = 1, profile: str = "normal",
             turnover_at: int | None = None) -> tuple[list[Row], dict]:
    """(rows, events). events: in_bed, onset, final_wake, end, cycles, awakenings, turnovers,
    stages ([start, end, "awake"|"light"|"deep"|"rem"]) and planted_turnover, all unix seconds.
    The structure depends only on date, seed and profile, so planting a turnover keeps it."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    rng = random.Random(seed)
    events = plan_night(date, rng, profile)
    rows = _make_rows(events, rng, turnover_at)
    events["planted_turnover"] = turnover_at
    return rows, events


def _number(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def write_csv(rows: list[Row], path: Path) -> None:
    lines = [HEADER]
    for r in rows:
        lines.append(",".join([
            datetime.fromtimestamp(r.t).isoformat(timespec="seconds"), str(r.t), str(r.samples),
            f"{r.mov_sum:.3f}", f"{r.mov_max:.3f}",
            _number(r.ax, 2), _number(r.ay, 2), _number(r.az, 2),
            _number(r.hr, 0), _number(r.hr_conf, 0), _number(r.battery, 0), str(r.t + 1),
        ]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def clock_time(text: str) -> str:
    """argparse type for a 24-hour HH:MM time."""
    try:
        parsed = datetime.strptime(text.strip(), "%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a 24-hour HH:MM time, e.g. 05:50") from None
    return parsed.strftime("%H:%M")


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a fake night of watch data.")
    parser.add_argument("--date", required=True, help="evening the night starts, YYYY-MM-DD")
    # expanduser: Windows PowerShell passes ~ through as a literal folder name
    parser.add_argument("--out", required=True, type=lambda text: Path(text).expanduser(),
                        help="folder to write watch-DATE.csv into")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="normal")
    parser.add_argument("--turnover-at", metavar="HH:MM", type=clock_time,
                        help="plant a position change at this 24-hour time, e.g. 05:50")
    args = parser.parse_args()

    planted = alarm_unix_for_night(args.date, args.turnover_at, []) if args.turnover_at else None
    rows, events = generate(args.date, args.seed, args.profile, planted)
    # the first row is 10 s after in_bed, so a turnover exactly at in_bed has no row to land in
    if planted is not None and not events["in_bed"] < planted <= events["end"]:
        sys.exit(f"--turnover-at {clock(planted)} is outside this night: it runs from "
                 f"{clock(events['in_bed'])} to {clock(events['end'])}.")
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"watch-{args.date}.csv"
    if path.exists():
        sys.exit(f"{path} already exists. Delete it first if you want to replace it.")
    write_csv(rows, path)
    print(f"Wrote {len(rows)} rows to {path}")
    print(f"In bed {clock(events['in_bed'])}, asleep {clock(events['onset'])}, "
          f"final wake {clock(events['final_wake'])}, recording ends {clock(events['end'])}")
    print("Brief awakenings: " + (", ".join(clock(a) for a, _ in events["awakenings"]) or "none"))
    print("Turnovers: " + (", ".join(clock(t) for t in events["turnovers"]) or "none"))
    if planted is not None:
        print(f"Planted turnover: {clock(planted)}")


if __name__ == "__main__":
    main()
