#!/usr/bin/env python3
"""Fill gaps in the Pi's watch-YYYY-MM-DD.csv files from the watch's own pilog.csv.

Usage:
  python3 merge_pilog.py ~/pilog.csv                  merge into ~/sleeptracker/data
  python3 merge_pilog.py ~/pilog.csv --dry-run        report only, write nothing
  python3 merge_pilog.py ~/pilog.csv --data-dir DIR   merge into another folder

The watch appends every row to pilog.csv whether or not the Pi was listening, so when
watch_logger.py missed part of a night this puts the missing rows back. Rows are matched
on the watch's timestamp; the Pi's own row wins where both have one (it carries pi_unix).
A night that gains rows is rewritten in the logger's format, with the original kept as
watch-YYYY-MM-DD.csv.bak. Rows that came from the watch have an empty pi_unix.
"""
import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.sleep_analysis import read_fields  # noqa: E402
from watch.watch_logger import DATA_DIR, HEADER, WATCH_FIELDS, clock12  # noqa: E402

GAP_SECONDS = 60  # spans with no row for longer than this are reported


def night_date(watch_unix: int) -> str:
    # Same rule as watch_logger.night_file: before noon belongs to the previous evening
    return f"{datetime.fromtimestamp(watch_unix) - timedelta(hours=12):%Y-%m-%d}"


def load(path: Path) -> dict[int, dict[str, str]]:
    """watch_unix -> column text of each usable row in a Pi night file or the watch's pilog.csv."""
    rows = {}
    for row, fields in read_fields(path):
        rows.setdefault(row.t, fields)
    return rows


def format_row(fields: dict[str, str]) -> str:
    watch_unix = fields.get("watch_unix") or fields["t"]
    when = fields.get("time") or datetime.fromtimestamp(int(watch_unix)).isoformat(timespec="seconds")
    values = [watch_unix] + [fields.get(name, "") for name in WATCH_FIELDS[1:]]
    return ",".join([when, *values, fields.get("pi_unix", "")])


def gaps(times: list[int], longer_than: int = GAP_SECONDS) -> list[tuple[int, int]]:
    return [(a, b) for a, b in zip(times, times[1:]) if b - a > longer_than]


def write_night(path: Path, rows: dict[int, dict[str, str]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        f.write(",".join(HEADER) + "\n")
        for t in sorted(rows):
            f.write(format_row(rows[t]) + "\n")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


def merge(pilog: Path, data_dir: Path, dry_run: bool = False) -> list[dict]:
    """Merge pilog.csv into the night files under data_dir; one report per night touched."""
    nights: dict[str, dict[int, dict[str, str]]] = {}
    for t, fields in load(pilog).items():
        nights.setdefault(night_date(t), {})[t] = fields
    reports = []
    for date in sorted(nights):
        path = data_dir / f"watch-{date}.csv"
        existing = load(path) if path.exists() else {}
        added = {t: f for t, f in nights[date].items() if t not in existing}
        merged = {**existing, **added}
        if added and not dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)
            write_night(path, merged)
        reports.append({"date": date, "path": path, "existing": len(existing), "added": len(added),
                        "written": bool(added) and not dry_run, "gaps": gaps(sorted(merged))})
    return reports


def describe(report: dict) -> str:
    lines = [f"{report['path'].name}: {report['existing']} rows on the Pi, {report['added']} added from the watch"
             + ("" if report["added"] else " (left untouched)")]
    for a, b in report["gaps"]:
        start, end = datetime.fromtimestamp(a), datetime.fromtimestamp(b)
        lines.append(f"  still no data {clock12(start)} to {clock12(end)} ({(b - a) // 60} min)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill the Pi's night files from the watch's pilog.csv.")
    parser.add_argument("pilog", type=Path, help="pilog.csv downloaded from the watch")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help=f"night files folder (default {DATA_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args()
    if not args.pilog.is_file():
        print(f"{args.pilog} is not a file")
        return 1
    reports = merge(args.pilog, args.data_dir, args.dry_run)
    if not reports:
        print(f"no usable rows in {args.pilog}")
        return 1
    for report in reports:
        print(describe(report))
    if args.dry_run:
        print("dry run: nothing written")
    elif any(r["written"] for r in reports):
        print("originals kept as .bak; reload the web app to see the filled nights")
    return 0


if __name__ == "__main__":
    sys.exit(main())
