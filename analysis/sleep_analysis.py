"""Sleep analysis shared by the web app (system python3) and wake.py (~/snooz-env).

Standard library only. Consumers import it with:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analysis.sleep_analysis import ...

Units: the watch sends one row every 10 s. mov_sum is the sum of |change in acceleration|
(in g) over that row's accelerometer samples (12.5 Hz, about 125 per row). Per-minute
movement ("mov") is scaled to a full minute of samples: sum(mov_sum) / sum(samples) * 750.

The Detector is causal: it only uses rows it has already been given, so the live alarm
(wake.py) and the web app's "would have fired" overlay (simulate_wake) agree when they are
fed the same rows in the same order.
"""
import math
import re
from bisect import bisect_left, insort
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from functools import lru_cache
from typing import NamedTuple

# Every threshold the owner may want to tune. Override any of them with a "tuning" object
# in settings.json; tuning_from_settings() merges and checks the overrides.
DEFAULT_TUNING = {
    "hr_min_confidence": 50,  # heart rate readings below this confidence (0-100) are ignored
    # Lying still, the 12.5 Hz accelerometer's own noise adds up to about 12 per minute (first real
    # night, Oct 2026: quiet half-hours never passed 14), so active starts well above that
    "active_floor": 20.0,     # movement per minute that always counts as active
    "active_ratio": 2.0,      # ...or more than this times the night's resting movement (its noise floor)
    "posture_degrees": 15.0,  # wrist angle change between minutes that counts as turning over
    "onset_minutes": 10,      # consecutive still minutes that mark falling asleep
    "awake_minutes": 3,       # active minutes within the last 5 that mean awake, not light sleep
    "light_minutes": 3,       # minutes after an active minute still counted as light sleep
    "hr_rise_bpm": 8.0,       # heart rate this far above resting counts as light sleep
    "hr_rise_minutes": 2,     # consecutive minutes the heart rate rise must last
    "baseline_minutes": 60,   # window for the resting heart rate
}
_TUNING_MINIMUM = {"onset_minutes": 1, "awake_minutes": 1, "hr_rise_minutes": 1, "baseline_minutes": 1}

SAMPLES_PER_MINUTE = 750           # 12.5 Hz accelerometer
AWAKE_LOOKBACK_MINUTES = 5         # the "last 5 minutes" of the awake rule
RESTING_MOV_PERCENTILE = 20        # resting movement = this percentile of minutes since onset
RESTING_MOV_MIN_MINUTES = 10       # minutes needed before resting movement is trusted
RESTING_HR_MIN_MINUTES = 5         # confident heart rate minutes needed for a resting heart rate
MIN_GRAVITY_G = 0.5                # mean acceleration shorter than this has no usable direction
AROUSAL_WINDOW_SECONDS = 60        # live movement rate and the posture reference look back this far
POSTURE_RECENT_SECONDS = 30        # live posture compares these last seconds with the minute before
AROUSAL_MIN_ROWS = 3               # rows needed in the live movement window before it can fire
HR_RISE_MAX_AGE_SECONDS = 60       # a heart rate rise counts live only if its last minute ended this recently
NO_DATA_SLACK_SECONDS = 60         # data ending this close to latest_start still counts as covering it
MIN_PLAUSIBLE_UNIX = 1_600_000_000  # watch times before Sept 2020 mean its clock was never set
LONG_GAP_SECONDS = 3600            # a gap in the data longer than this is not counted as asleep

# The web app's defaults, so before anything is saved the alarm does what the page shows
DEFAULT_SETTINGS = {"enabled": True, "alarm_time": "06:30", "window_minutes": 30, "fade_minutes": 15}
# [0-9], not \d: \d also matches other scripts' digits, such as full-width ２
ALARM_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


class Row(NamedTuple):
    t: int
    samples: int
    mov_sum: float
    mov_max: float
    ax: float | None
    ay: float | None
    az: float | None
    hr: float | None
    hr_conf: float
    battery: float | None


@dataclass(slots=True)
class Minute:
    t: int
    mov: float | None
    mov_max: float
    hr: float | None
    posture_deg: float | None
    active: bool
    light: bool = False
    awake: bool = False

    def as_dict(self) -> dict:
        return {
            "t": self.t,
            "mov": None if self.mov is None else round(self.mov, 2),
            "mov_max": round(self.mov_max, 3),
            "hr": None if self.hr is None else round(self.hr, 1),
            "posture_deg": None if self.posture_deg is None else round(self.posture_deg, 1),
            "active": self.active,
            "light": self.light,
            "awake": self.awake,
        }


def tuning_from_settings(settings: dict) -> dict:
    tuning = dict(DEFAULT_TUNING)
    overrides = settings.get("tuning") if isinstance(settings, dict) else None
    if not isinstance(overrides, dict):
        return tuning
    for key, value in overrides.items():
        if key not in DEFAULT_TUNING or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number) or number < _TUNING_MINIMUM.get(key, 0):
            continue
        tuning[key] = round(number) if isinstance(DEFAULT_TUNING[key], int) else number
    return tuning


def _whole_number(value, default: int, low: int, high: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(high, max(low, number))


def _timestamp(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def clean_settings(raw) -> dict:
    """settings.json as the alarm uses it; the web app shows the same. Missing values take the defaults."""
    raw = raw if isinstance(raw, dict) else {}
    alarm_time = raw.get("alarm_time", DEFAULT_SETTINGS["alarm_time"])
    valid = isinstance(alarm_time, str) and ALARM_TIME_RE.fullmatch(alarm_time) is not None
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_SETTINGS["enabled"])) and valid,
        "alarm_time": alarm_time if valid else None,
        "window_minutes": _whole_number(raw.get("window_minutes"), DEFAULT_SETTINGS["window_minutes"], 0, 90),
        "fade_minutes": _whole_number(raw.get("fade_minutes"), DEFAULT_SETTINGS["fade_minutes"], 1, 30),
        "tuning": tuning_from_settings(raw),
        "updated": _timestamp(raw.get("updated")),  # when the web app saved it
        "problem": None if valid else f"alarm_time {alarm_time!r} is not a 24-hour HH:MM time",
    }


# ---------- reading rows ----------

def _number(text: str) -> float | None:
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


@lru_cache(maxsize=8)
def _column_indexes(header: tuple[str, ...]) -> tuple | None:
    """Where each Row field sits in this header (None for an optional missing column)."""
    names = ("watch_unix" if "watch_unix" in header else "t", "samples", "mov_sum", "mov_max",
             "ax", "ay", "az", "hr", "hr_conf", "battery")
    if any(name not in header for name in names[:4]):
        return None
    return tuple(header.index(name) if name in header else None for name in names)


def parse_csv_line(line: str, header: list[str]) -> Row | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split(",")
    columns = _column_indexes(tuple(header))
    if columns is None or len(parts) != len(header):
        return None
    t_at, samples_at, mov_sum_at, mov_max_at, *optional = columns
    ax, ay, az, hr, hr_conf, battery = ("" if i is None else parts[i] for i in optional)
    try:
        t = int(parts[t_at])
        samples = int(parts[samples_at])
        mov_sum = _number(parts[mov_sum_at])
        mov_max = _number(parts[mov_max_at])
        hr_value = _number(hr)
        row = Row(t, samples, mov_sum, mov_max, _number(ax), _number(ay), _number(az),
                  hr_value if hr_value and hr_value > 0 else None,
                  _number(hr_conf) or 0.0, _number(battery))
    except ValueError:
        return None
    if t < MIN_PLAUSIBLE_UNIX or samples < 0 or mov_sum is None or mov_sum < 0 or mov_max is None:
        return None
    return row


def _header_columns(line: str) -> list[str] | None:
    if "samples" not in line:
        return None
    text = line.strip()
    if text.startswith("#"):
        # the watch's own pilog.csv: "# start <unix> t,samples,mov_sum,..."
        text = text.split()[-1]
    columns = [c.strip() for c in text.split(",")]
    return columns if "samples" in columns and "mov_sum" in columns else None


def read_fields(path) -> list[tuple[Row, dict[str, str]]]:
    """Each usable row with its column text as written, keyed by the header names in force."""
    rows = []
    header = None
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for line in f:
            columns = _header_columns(line)
            if columns:
                header = columns
                continue
            if header is None:
                continue
            row = parse_csv_line(line, header)
            if row is not None:
                rows.append((row, dict(zip(header, line.strip().split(",")))))
    return rows


def read_rows(path) -> list[Row]:
    return [row for row, _ in read_fields(path)]


# ---------- vectors ----------

def _angle_deg(a, b) -> float:
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    cos = dot / (math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2) * math.sqrt(b[0] ** 2 + b[1] ** 2 + b[2] ** 2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def _usable(vec):
    if vec is None or math.sqrt(vec[0] ** 2 + vec[1] ** 2 + vec[2] ** 2) < MIN_GRAVITY_G:
        return None
    return vec


def _mean_vector(rows, min_rows: int):
    x = y = z = weight = 0.0
    count = 0
    for r in rows:
        if r.ax is None or r.ay is None or r.az is None or not r.samples:
            continue
        x += r.ax * r.samples
        y += r.ay * r.samples
        z += r.az * r.samples
        weight += r.samples
        count += 1
    if count < min_rows:
        return None
    return _usable((x / weight, y / weight, z / weight))


def _median_sorted(s: list[float]) -> float:
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


# ---------- detector ----------

class Detector:
    """Classifies minutes and spots arousals as rows arrive. Never looks ahead.

    A minute closes when a row from a later minute arrives (or on finish()). The awake and
    heart-rate rules may relabel the last few closed minutes, and deciding onset labels the
    minutes since onset (at most onset_minutes back); older minutes never change. Look-backs
    count clock minutes, so they never reach across a Bluetooth gap.
    """

    def __init__(self, tuning: dict | None = None) -> None:
        self.tuning = tuning_from_settings({"tuning": tuning or {}})
        tn = self.tuning
        self._hr_min_conf = tn["hr_min_confidence"]
        self._active_floor = tn["active_floor"]
        self._active_ratio = tn["active_ratio"]
        self._posture_degrees = tn["posture_degrees"]
        self._onset_minutes = tn["onset_minutes"]
        self._awake_minutes = tn["awake_minutes"]
        self._light_minutes = tn["light_minutes"]
        self._hr_rise_bpm = tn["hr_rise_bpm"]
        self._hr_rise_minutes = tn["hr_rise_minutes"]
        self._baseline_seconds = tn["baseline_minutes"] * 60

        self.minutes: list[Minute] = []
        self.onset: int | None = None
        self.resting_mov: float | None = None
        self.resting_hr: float | None = None
        # ~2 minutes of rows for the live arousal checks
        self._recent: deque = deque(maxlen=12)
        # open minute: [t, samples, mov_sum, mov_max, hr_sum, hr_n, gx, gy, gz, g_samples]
        self._open: list | None = None
        self._prev_vector = None
        self._still_run = 0
        self._movs_since_onset: list[float] = []
        self._hrs_since_onset: deque = deque()  # (t, hr) inside the baseline window
        self._hrs_sorted: list[float] = []      # the same heart rates, sorted
        self._hr_rise: tuple[int, float, float] | None = None  # (last minute t, hr, rise)

    def active_threshold(self) -> float:
        if self.resting_mov is None:
            return self._active_floor
        return max(self._active_floor, self._active_ratio * self.resting_mov)

    def add(self, row: Row) -> None:
        t, samples, mov_sum, mov_max, ax, ay, az, hr, hr_conf, _ = row
        key = t - t % 60
        acc = self._open
        if acc is None or key > acc[0]:
            if acc is not None:
                self._close(acc)
            acc = self._open = [key, 0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0]
        elif key < acc[0]:
            return  # older than the open minute; closed minutes are never reopened
        acc[1] += samples
        acc[2] += mov_sum
        if mov_max > acc[3]:
            acc[3] = mov_max
        if hr is not None and hr_conf >= self._hr_min_conf:
            acc[4] += hr
            acc[5] += 1
        if samples and ax is not None and ay is not None and az is not None:
            acc[6] += ax * samples
            acc[7] += ay * samples
            acc[8] += az * samples
            acc[9] += samples
        if not self._recent or t >= self._recent[-1].t:
            self._recent.append(row)

    def finish(self) -> None:
        """Close the open minute, e.g. at the end of a file."""
        if self._open is not None:
            self._close(self._open)
            self._open = None

    def arousal_now(self) -> str | None:
        if not self._recent:
            return None
        newest = self._recent[-1].t
        window = [r for r in self._recent if r.t > newest - AROUSAL_WINDOW_SECONDS]
        samples = sum(r.samples for r in window)
        if len(window) >= AROUSAL_MIN_ROWS and samples:
            rate = sum(r.mov_sum for r in window) / samples * SAMPLES_PER_MINUTE
            if rate > self.active_threshold():
                return f"movement {rate:.0f}/min"
        split = newest - POSTURE_RECENT_SECONDS
        now_vec = _mean_vector((r for r in self._recent if r.t > split), 2)
        before_vec = _mean_vector(
            (r for r in self._recent if split - AROUSAL_WINDOW_SECONDS < r.t <= split), 3)
        if now_vec and before_vec:
            angle = _angle_deg(now_vec, before_vec)
            if angle > self._posture_degrees:
                return f"turned over {angle:.0f} deg"
        if self._hr_rise is not None:
            last_t, hr, rise = self._hr_rise
            # after a Bluetooth gap the first new row closes a minute that may be long past
            if newest - (last_t + 60) <= HR_RISE_MAX_AGE_SECONDS:
                return f"heart rate {hr:.0f} bpm, {rise:.0f} above resting"
        return None

    # ----- minute logic -----

    def _close(self, acc: list) -> None:
        t, samples, mov_sum, mov_max, hr_sum, hr_n, gx, gy, gz, g_samples = acc
        mov = mov_sum / samples * SAMPLES_PER_MINUTE if samples else None
        vector = _usable((gx / g_samples, gy / g_samples, gz / g_samples)) if g_samples else None
        posture = None
        if vector is not None and self._prev_vector is not None:
            posture = _angle_deg(vector, self._prev_vector)
        self._prev_vector = vector
        active = ((mov is not None and mov > self.active_threshold())
                  or (posture is not None and posture > self._posture_degrees))
        minute = Minute(t, mov, mov_max, hr_sum / hr_n if hr_n else None, posture, active)
        self.minutes.append(minute)

        self._update_hr_rise()
        self._mark_awake(minute)
        onset_found = self._update_onset(minute)
        self._mark_light(onset_found)
        self._update_baselines(minute, onset_found)

    def _update_onset(self, minute: Minute) -> bool:
        if self.onset is not None:
            return False
        if minute.active:
            self._still_run = 0
        elif len(self.minutes) > 1 and minute.t - self.minutes[-2].t > 60:
            self._still_run = 1  # a gap is not evidence of lying still, so the run restarts
        else:
            self._still_run += 1
        # The awake lookback can cover the first still minutes, so sleep starts after them. They
        # are a prefix of the run and cannot change once the newest minute is not awake.
        if self._still_run < self._onset_minutes or minute.awake:
            return False
        self.onset = next(m.t for m in self.minutes[-self._still_run:] if not m.awake)
        return True

    def _update_hr_rise(self) -> None:
        self._hr_rise = None
        n = self._hr_rise_minutes
        run = self.minutes[-n:]
        # minute times only increase, so this span means the run has no gaps
        if self.resting_hr is None or len(run) < n or run[-1].t - run[0].t != (n - 1) * 60:
            return
        limit = self.resting_hr + self._hr_rise_bpm
        if all(m.hr is not None and m.hr >= limit for m in run):
            self._hr_rise = (run[-1].t, run[-1].hr, run[-1].hr - self.resting_hr)

    def _mark_awake(self, minute: Minute) -> None:
        if self.onset is None and minute.active:
            minute.awake = True
        since = minute.t - AWAKE_LOOKBACK_MINUTES * 60
        recent = [m for m in self.minutes[-AWAKE_LOOKBACK_MINUTES:] if m.t > since]
        first_active = next((i for i, m in enumerate(recent) if m.active), None)
        if first_active is None:
            return
        if sum(m.active for m in recent) >= self._awake_minutes:
            for m in recent[first_active:]:
                if not m.awake:
                    self._drop_resting_hr(m)  # labelled awake after it joined the resting heart rate
                m.awake = True
                m.light = False

    def _near_activity(self, index: int) -> bool:
        """Minute `index` or one of the light_minutes clock minutes before it was active."""
        since = self.minutes[index].t - self._light_minutes * 60
        lookback = self.minutes[max(0, index - self._light_minutes):index + 1]
        return any(m.active and m.t >= since for m in lookback)

    def _mark_light(self, onset_found: bool) -> None:
        if self.onset is None:
            return
        count = len(self.minutes)
        # onset is decided onset_minutes late, so the minutes since it still need labelling
        first = count - self._still_run if onset_found else count - 1
        for i in range(first, count):
            m = self.minutes[i]
            if m.t >= self.onset and not m.awake and self._near_activity(i):
                m.light = True
        if self._hr_rise is not None:
            for m in self.minutes[-self._hr_rise_minutes:]:
                if not m.awake and m.t >= self.onset:
                    m.light = True

    def _update_baselines(self, minute: Minute, onset_found: bool) -> None:
        if self.onset is None:
            return
        new = [m for m in self.minutes[-self._still_run:] if m.t >= self.onset] if onset_found else [minute]
        for m in new:
            if m.mov is not None:
                insort(self._movs_since_onset, m.mov)
            if m.hr is not None and not m.awake:  # resting heart rate is while asleep, as in analyze()
                self._hrs_since_onset.append((m.t, m.hr))
                insort(self._hrs_sorted, m.hr)
        movs = self._movs_since_onset
        if len(movs) >= RESTING_MOV_MIN_MINUTES:
            self.resting_mov = movs[(len(movs) - 1) * RESTING_MOV_PERCENTILE // 100]
        hrs, ordered = self._hrs_since_onset, self._hrs_sorted
        while hrs and hrs[0][0] <= minute.t - self._baseline_seconds:
            del ordered[bisect_left(ordered, hrs.popleft()[1])]
        self.resting_hr = _median_sorted(ordered) if len(ordered) >= RESTING_HR_MIN_MINUTES else None

    def _drop_resting_hr(self, minute: Minute) -> None:
        entry = (minute.t, minute.hr)
        if minute.hr is not None and entry in self._hrs_since_onset:
            self._hrs_since_onset.remove(entry)
            del self._hrs_sorted[bisect_left(self._hrs_sorted, minute.hr)]


# ---------- whole nights ----------

def _spans(minutes: list[Minute], flag: str) -> list[list[int]]:
    spans: list[list[int]] = []
    for m in minutes:
        if not getattr(m, flag):
            continue
        if spans and spans[-1][1] == m.t:
            spans[-1][1] = m.t + 60
        else:
            spans.append([m.t, m.t + 60])
    return spans


def _gap_spans(minutes: list[Minute]) -> list[list[int]]:
    """Stretches with no data longer than LONG_GAP_SECONDS, e.g. between an evening test and bedtime."""
    return [[a.t + 60, b.t] for a, b in zip(minutes, minutes[1:]) if b.t - (a.t + 60) > LONG_GAP_SECONDS]


def _asleep_minutes(onset: int | None, end: int | None, not_asleep: list[list[int]]) -> int:
    """Clock minutes from onset to end outside the not_asleep spans (awake, and long gaps).
    Shorter Bluetooth gaps count as asleep."""
    if onset is None or end is None or end <= onset:
        return 0
    off = sum(max(0, min(e, end) - max(s, onset)) for s, e in not_asleep)
    return (end - onset - off) // 60


def analyze(rows: list[Row], tuning: dict | None = None) -> dict:
    """Label a whole night. resting_hr here is the median confident heart rate while asleep."""
    detector = Detector(tuning)
    for row in rows:
        detector.add(row)
    detector.finish()
    minutes = detector.minutes
    onset = detector.onset
    awake_spans = _spans(minutes, "awake")
    light_spans = _spans(minutes, "light")
    final_wake = None
    if awake_spans and awake_spans[-1][1] == minutes[-1].t + 60:
        final_wake = awake_spans[-1][0]
    sleep_end = final_wake or (minutes[-1].t + 60 if minutes else None)
    asleep_hrs = [m.hr for m in minutes
                  if onset is not None and onset <= m.t < sleep_end and not m.awake and m.hr is not None]
    return {
        "minutes": [m.as_dict() for m in minutes],
        "onset": onset,
        "final_wake": final_wake,
        "asleep_minutes": _asleep_minutes(onset, sleep_end, awake_spans + _gap_spans(minutes)),
        "light_spans": light_spans,
        "awake_spans": awake_spans,
        "light_minutes": sum(m.light for m in minutes),
        "resting_hr": round(_median_sorted(sorted(asleep_hrs)), 1) if asleep_hrs else None,
        "rows": len(rows),
    }


def wake_window(alarm_unix: int, window_minutes: int, fade_minutes: int) -> tuple[int, int]:
    """(window_start, latest_start) of one alarm. Equal when there is no smart window."""
    if window_minutes <= 0:
        start = alarm_unix - fade_minutes * 60
        return start, start
    window_start = alarm_unix - window_minutes * 60
    return window_start, max(alarm_unix - fade_minutes * 60, window_start)


def simulate_wake(rows: list[Row], alarm_unix: int, window_minutes: int, fade_minutes: int,
                  tuning: dict | None = None) -> dict:
    """What wake.py would have done with these rows: same Detector, same order, same checks."""
    window_start, latest_start = wake_window(alarm_unix, window_minutes, fade_minutes)
    smart = window_minutes > 0
    detector = Detector(tuning)
    trigger = trigger_t = last_t = None
    for row in rows:
        last_t = row.t
        if row.t > latest_start:
            break
        detector.add(row)
        if smart and row.t >= window_start:
            trigger = detector.arousal_now()
            if trigger:
                trigger_t = row.t
                break
    if trigger:
        fade_start, reason = trigger_t, "light_sleep"
    elif smart and (last_t is None or last_t < latest_start - NO_DATA_SLACK_SECONDS):
        fade_start, reason = latest_start, "no_data"
    else:
        fade_start, reason = latest_start, "fallback"
    return {
        "window_start": window_start,
        "latest_start": latest_start,
        "fade_start": fade_start,
        "fade_end": min(fade_start + fade_minutes * 60, alarm_unix),
        "reason": reason,
        "trigger": trigger,
        "trigger_t": trigger_t,
    }


def alarm_unix_for_night(date: str, alarm_time: str, rows: list[Row]) -> int:
    """First alarm_time (HH:MM, local) after the night's first row, or after noon of date."""
    noon = datetime.strptime(date, "%Y-%m-%d").replace(hour=12)
    reference = noon.timestamp()
    if rows and reference <= rows[0].t < reference + 86400:
        reference = rows[0].t
    hour, minute = (int(part) for part in alarm_time.split(":"))
    start = datetime.fromtimestamp(reference)
    alarm = datetime.combine(start.date(), dtime(hour, minute))
    if alarm.timestamp() <= reference:
        alarm = datetime.combine(start.date() + timedelta(days=1), dtime(hour, minute))
    return int(alarm.timestamp())
