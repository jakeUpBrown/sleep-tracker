#!/usr/bin/env python3
"""Small web app on the Pi: set the alarm and look at each night's watch data.

Usage:
  python3 sleepweb.py              serve on port 8000
  python3 sleepweb.py --port 8080  serve on another port

Open http://<pi-address>:8000 on your phone and add it to the home screen.
Settings are saved to ~/sleeptracker/settings.json, which wake.py will read.
Sleep data comes from the watch logger's ~/sleeptracker/data/watch-YYYY-MM-DD.csv files.
Uses only the Python standard library, so it runs with the system python3.
"""
import csv
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path.home() / "sleeptracker"
DATA_DIR = BASE / "data"
SETTINGS = BASE / "settings.json"
PAGE = Path(__file__).with_name("sleepweb.html")
DEFAULTS = {"enabled": True, "alarm_time": "06:30", "window_minutes": 30}
HR_MIN_CONFIDENCE = 50  # heart rate readings below this confidence are skipped
NIGHT_RE = re.compile(r"watch-(\d{4}-\d{2}-\d{2})\.csv$")


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text())}
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def save_settings(new: dict) -> dict:
    settings = load_settings()
    if "enabled" in new:
        settings["enabled"] = bool(new["enabled"])
    if "alarm_time" in new:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(new["alarm_time"])):
            raise ValueError("alarm time must look like 06:30")
        settings["alarm_time"] = new["alarm_time"]
    if "window_minutes" in new:
        minutes = int(new["window_minutes"])
        if not 0 <= minutes <= 90:
            raise ValueError("wake window must be 0 to 90 minutes")
        settings["window_minutes"] = minutes
    settings["updated"] = int(time.time())
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, SETTINGS)  # wake.py never sees a half-written file
    return settings


def read_rows(path: Path):
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                yield {
                    "t": int(row["watch_unix"]),
                    "mov": float(row["mov_sum"] or 0),
                    "hr": float(row["hr"] or 0),
                    "conf": float(row["hr_conf"] or 0),
                    "bat": float(row["battery"] or 0),
                    "pi": int(row.get("pi_unix") or 0),
                }
            except (KeyError, ValueError):
                continue  # half-written or garbled line


def list_nights() -> list:
    nights = []
    for path in sorted(DATA_DIR.glob("watch-*.csv"), reverse=True):
        match = NIGHT_RE.search(path.name)
        if not match:
            continue
        rows = list(read_rows(path))
        if not rows:
            continue
        hrs = [r["hr"] for r in rows if r["hr"] > 0 and r["conf"] >= HR_MIN_CONFIDENCE]
        nights.append({
            "date": match.group(1),
            "start": rows[0]["t"],
            "end": rows[-1]["t"],
            "last_received": max(r["pi"] for r in rows),
            "rows": len(rows),
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            "hr_min": round(min(hrs)) if hrs else None,
        })
    return nights


def night_detail(date: str) -> dict:
    path = DATA_DIR / f"watch-{date}.csv"
    if not NIGHT_RE.search(path.name) or not path.exists():
        raise FileNotFoundError(date)
    minutes = {}
    for r in read_rows(path):
        m = minutes.setdefault(r["t"] // 60 * 60, {"mov": 0.0, "hr": [], "bat": r["bat"]})
        m["mov"] += r["mov"]
        m["bat"] = r["bat"]
        if r["hr"] > 0 and r["conf"] >= HR_MIN_CONFIDENCE:
            m["hr"].append(r["hr"])
    return {
        "date": date,
        "minutes": [
            {"t": t, "mov": round(m["mov"], 3),
             "hr": round(sum(m["hr"]) / len(m["hr"]), 1) if m["hr"] else None,
             "bat": m["bat"]}
            for t, m in sorted(minutes.items())
        ],
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
                self.send(200, load_settings())
            elif url.path == "/api/nights":
                self.send(200, {"now": int(time.time()), "nights": list_nights()})
            elif url.path == "/api/night":
                self.send(200, night_detail(parse_qs(url.query).get("date", [""])[0]))
            elif url.path == "/manifest.webmanifest":
                self.send(200, MANIFEST, "application/manifest+json")
            elif url.path == "/icon.svg":
                self.send(200, ICON, "image/svg+xml")
            else:
                self.send(404, {"error": "not found"})
        except FileNotFoundError:
            self.send(404, {"error": "no data for that night"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/settings":
            self.send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            self.send(200, save_settings(json.loads(self.rfile.read(length) or b"{}")))
        except (ValueError, TypeError) as e:
            self.send(400, {"error": str(e)})

    def log_message(self, fmt, *args) -> None:
        if self.command == "POST" and self.path == "/api/settings" and str(args[1]) == "200":
            print(f"{time.strftime('%H:%M:%S')} settings changed: {json.dumps(load_settings())}", flush=True)


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
