#!/usr/bin/env python3
"""Small web app on the Pi: set the alarm, keep a sleep diary and look at each night's watch data.

Usage:
  python3 sleepweb.py              serve on port 8000
  python3 sleepweb.py --port 8080  serve on another port

Open http://<pi-address>:8000 on your phone and add it to the home screen.
Settings are saved to ~/sleeptracker/settings.json, which wake.py reads.
Sleep data comes from the watch logger's ~/sleeptracker/data/watch-YYYY-MM-DD.csv files,
diary entries go to ~/sleeptracker/data/diary.json.
Uses only the Python standard library, so it runs with the system python3.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import (  # noqa: E402
    ALARM_TIME_RE, DEFAULT_SETTINGS, alarm_unix_for_night, analyze, clean_settings, read_rows, simulate_wake,
    tuning_from_settings,
)

BASE = Path.home() / "sleeptracker"
DATA_DIR = BASE / "data"
SETTINGS = BASE / "settings.json"
DIARY = DATA_DIR / "diary.json"
PAGE = Path(__file__).with_name("sleepweb.html")
# [0-9], not \d: \d also matches other scripts' digits, such as full-width ２０２６
NIGHT_RE = re.compile(r"watch-([0-9]{4}-[0-9]{2}-[0-9]{2})\.csv$")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOTES_MAX_CHARS = 2000

_diary_lock = threading.Lock()
# path -> (key, summary). The page polls every minute, so only files that changed are re-read.
_summaries: dict = {}


def log(message: str) -> None:
    print(f"{time.strftime('%I:%M:%S %p').lstrip('0')} {message}", flush=True)


def load_settings() -> dict:
    """settings.json as stored, for saving: keys the page doesn't know, like "tuning", are kept."""
    try:
        stored = json.loads(SETTINGS.read_text())
    except (FileNotFoundError, ValueError):
        return dict(DEFAULT_SETTINGS)
    return {**DEFAULT_SETTINGS, **stored} if isinstance(stored, dict) else dict(DEFAULT_SETTINGS)


def effective_settings() -> dict:
    """settings.json as wake.py reads it, so the page and the chart show what the alarm will really do."""
    return clean_settings(load_settings())


def save_settings(new: dict) -> dict:
    settings = load_settings()  # keys the page doesn't know, like "tuning", are kept
    if "enabled" in new:
        if not isinstance(new["enabled"], bool):
            raise ValueError("enabled must be true or false")
        settings["enabled"] = new["enabled"]
    if "alarm_time" in new:
        if not ALARM_TIME_RE.fullmatch(str(new["alarm_time"])):
            raise ValueError("alarm time must look like 06:30")
        settings["alarm_time"] = new["alarm_time"]
    if "window_minutes" in new:
        settings["window_minutes"] = whole_minutes(new["window_minutes"], 0, 90, "wake window")
    if "fade_minutes" in new:
        settings["fade_minutes"] = whole_minutes(new["fade_minutes"], 1, 30, "fade length")
    settings["updated"] = int(time.time())
    BASE.mkdir(parents=True, exist_ok=True)
    write_atomic(SETTINGS, settings)  # wake.py never sees a half-written file
    return settings


def whole_minutes(value, low: int, high: int, what: str) -> int:
    message = f"{what} must be a whole number of minutes from {low} to {high}"
    # int() would quietly turn true into 1 and 7.9 into 7
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(message)
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError(message) from None
    if not low <= minutes <= high:
        raise ValueError(message)
    return minutes


def write_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


# ---------- diary ----------

def load_diary() -> dict:
    entries = read_json(DIARY)
    return entries if isinstance(entries, dict) else {}


def _yes_no(body: dict, key: str):
    value = body.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{key} must be true, false or null")
    return value


def _diary_entry(body: dict) -> tuple[str, dict]:
    date = body.get("date")
    if not isinstance(date, str) or not DATE_RE.fullmatch(date):
        raise ValueError("date must look like 2026-10-13")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{date} is not a real date") from None
    rating = body.get("rating")
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 10):
        raise ValueError("rating must be a whole number from 1 to 10, or null")
    notes = body.get("notes")
    if notes is None:
        notes = ""
    elif not isinstance(notes, str):
        raise ValueError("notes must be text")
    notes = notes.strip()
    if len(notes) > NOTES_MAX_CHARS:
        raise ValueError(f"notes can be at most {NOTES_MAX_CHARS} characters")
    entry = {
        "melatonin": _yes_no(body, "melatonin"),
        "sober": _yes_no(body, "sober"),
        "rating": rating,
        "notes": notes,
        "updated": int(time.time()),
    }
    return date, entry


def save_diary(body: dict) -> tuple[str, dict]:
    date, entry = _diary_entry(body)
    with _diary_lock:
        entries = read_json(DIARY) if DIARY.exists() else {}
        if not isinstance(entries, dict):
            raise ValueError(f"{DIARY} can't be read, and saving would lose the other entries. Fix or move it first.")
        entries[date] = entry
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        write_atomic(DIARY, dict(sorted(entries.items())))
    return date, entry


# ---------- nights ----------

def night_path(date: str) -> Path:
    if not DATE_RE.fullmatch(date or ""):
        raise FileNotFoundError(date)
    path = DATA_DIR / f"watch-{date}.csv"
    if not path.exists():
        raise FileNotFoundError(date)
    return path


def last_received(path: Path, fallback: int) -> int:
    """pi_unix of the file's last complete line: when the Pi last got data from the watch."""
    with path.open("rb") as f:
        header = f.readline().decode(errors="replace").strip().split(",")
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode(errors="replace").splitlines()
    if "pi_unix" not in header:
        return fallback
    column = header.index("pi_unix")
    for line in reversed(tail):
        parts = line.strip().split(",")
        if len(parts) == len(header) and parts[column].isdigit():
            return int(parts[column])
    return fallback


def confident_hrs(rows, tuning: dict) -> list[float]:
    return [r.hr for r in rows if r.hr is not None and r.hr_conf >= tuning["hr_min_confidence"]]


def night_summary(path: Path, date: str, tuning: dict) -> dict | None:
    stat = path.stat()
    # keyed on the effective tuning, not settings "updated": changing the alarm time doesn't
    # re-analyse every night, while a hand-edited "tuning" (which doesn't bump "updated") does
    key = (stat.st_mtime_ns, stat.st_size, tuple(sorted(tuning.items())))
    cached = _summaries.get(path)
    if cached and cached[0] == key:
        return cached[1]
    rows = read_rows(path)
    summary = None
    if rows:
        hrs = confident_hrs(rows, tuning)
        summary = {
            "date": date,
            "start": rows[0].t,
            "end": rows[-1].t,
            "last_received": last_received(path, rows[-1].t),
            "rows": len(rows),
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            "hr_min": round(min(hrs)) if hrs else None,
            "asleep_minutes": analyze(rows, tuning)["asleep_minutes"],
        }
    _summaries[path] = (key, summary)
    return summary


def list_nights() -> list:
    tuning = tuning_from_settings(load_settings())
    diary = load_diary()
    nights = []
    for path in sorted(DATA_DIR.glob("watch-*.csv"), reverse=True):
        match = NIGHT_RE.search(path.name)
        if not match:
            continue
        summary = night_summary(path, match.group(1), tuning)
        if summary is None:
            continue
        entry = diary.get(summary["date"])
        nights.append({**summary, "rating": entry.get("rating") if isinstance(entry, dict) else None})
    return nights


def chart_minutes(rows, analysis: dict) -> list:
    battery = {}
    for r in rows:
        if r.battery is not None:
            battery[r.t - r.t % 60] = r.battery
    return [{"t": m["t"], "mov": m["mov"], "hr": m["hr"], "bat": battery.get(m["t"])}
            for m in analysis["minutes"]]


def simulation(date: str, rows, settings: dict) -> dict | None:
    if settings["alarm_time"] is None:
        return None  # not a time wake.py can use, so the alarm is off
    alarm = alarm_unix_for_night(date, settings["alarm_time"], rows)
    return simulate_wake(rows, alarm, settings["window_minutes"], settings["fade_minutes"], settings["tuning"])


def night_detail(date: str) -> dict:
    path = night_path(date)
    settings = effective_settings()
    tuning = settings["tuning"]
    rows = read_rows(path)
    analysis = analyze(rows, tuning)
    actual = read_json(DATA_DIR / f"wake-{date}.json")
    entry = load_diary().get(date)
    return {
        "date": date,
        "minutes": chart_minutes(rows, analysis),
        "analysis": analysis,
        "simulation": simulation(date, rows, settings),
        "settings_used": {key: settings[key] for key in ("alarm_time", "window_minutes", "fade_minutes", "enabled")},
        "actual": actual if isinstance(actual, dict) else None,
        "diary": entry if isinstance(entry, dict) else None,
    }


MANIFEST = {
    "name": "Sleep Tracker",
    "short_name": "Sleep",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#11131a",
    "theme_color": "#11131a",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}
ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">
<rect width="96" height="96" rx="20" fill="#11131a"/>
<path d="M60 20a30 30 0 1 0 16 46A26 26 0 0 1 60 20z" fill="#f2c46d"/></svg>"""


class Handler(BaseHTTPRequestHandler):
    def send(self, status: int, body, ctype: str = "application/json") -> None:
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/settings":
                self.send(200, effective_settings())
            elif url.path == "/api/nights":
                self.send(200, {"now": int(time.time()), "nights": list_nights()})
            elif url.path == "/api/night":
                self.send(200, night_detail(parse_qs(url.query).get("date", [""])[0]))
            elif url.path == "/api/diary":
                self.send(200, {"entries": load_diary()})
            elif url.path == "/manifest.webmanifest":
                self.send(200, MANIFEST, "application/manifest+json")
            elif url.path == "/icon.svg":
                self.send(200, ICON, "image/svg+xml")
            else:
                self.send(404, {"error": "not found"})
        except FileNotFoundError:
            self.send(404, {"error": "no data for that night"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/settings", "/api/diary"):
            self.send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if path == "/api/settings":
                result = save_settings(body)
                note = f"settings changed: {json.dumps(result)}"
            else:
                date, entry = save_diary(body)
                result = {"date": date, "entry": entry}
                note = f"diary saved for {date}"
        except (ValueError, TypeError) as e:
            self.send(400, {"error": str(e)})
            return
        self.send(200, result)
        log(note)

    def log_message(self, fmt, *args) -> None:
        pass  # only saves are logged, from do_POST


def main() -> None:
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Sleep web app on port {port}. Settings file: {SETTINGS}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
