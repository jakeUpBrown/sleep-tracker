import io
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.sleep_analysis import read_rows  # noqa: E402
from watch import merge_pilog  # noqa: E402
from watch.watch_logger import HEADER  # noqa: E402

PILOG_HEADER = "# start {start} t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery\n"


def unix(year, month, day, hour, minute=0, second=0) -> int:
    return int(datetime(year, month, day, hour, minute, second).timestamp())


def watch_row(t: int, mov: str = "1.000") -> str:
    return f"{t},125,{mov},0.050,0.01,-0.70,0.71,55,80,90"


def pi_line(t: int, mov: str = "1.000", pi_unix: int | None = None) -> str:
    when = datetime.fromtimestamp(t).isoformat(timespec="seconds")
    return f"{when},{watch_row(t, mov)},{pi_unix if pi_unix is not None else t + 1}"


class MergeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "data"
        self.data.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write_pilog(self, *sessions: list[int]) -> Path:
        path = self.dir / "pilog.csv"
        text = ""
        for times in sessions:
            text += PILOG_HEADER.format(start=times[0] - 5) + "".join(watch_row(t) + "\n" for t in times)
        path.write_text(text)
        return path

    def write_night(self, date: str, lines: list[str]) -> Path:
        path = self.data / f"watch-{date}.csv"
        path.write_text(",".join(HEADER) + "\n" + "".join(line + "\n" for line in lines))
        return path

    def night_times(self, date: str) -> list[int]:
        return [r.t for r in read_rows(self.data / f"watch-{date}.csv")]

    def test_buckets_nights_at_noon(self):
        before_noon = unix(2026, 10, 15, 11, 59, 50)
        after_noon = unix(2026, 10, 15, 12, 0, 10)
        reports = merge_pilog.merge(self.write_pilog([before_noon, after_noon]), self.data)
        self.assertEqual([r["date"] for r in reports], ["2026-10-14", "2026-10-15"])
        self.assertEqual(self.night_times("2026-10-14"), [before_noon])
        self.assertEqual(self.night_times("2026-10-15"), [after_noon])

    def test_fills_gap_and_keeps_pi_rows(self):
        t0 = unix(2026, 10, 14, 23, 0)
        times = [t0 + 10 * i for i in range(6)]
        pi_rows = [pi_line(times[0], mov="9.999", pi_unix=123), pi_line(times[5])]
        path = self.write_night("2026-10-14", pi_rows)
        reports = merge_pilog.merge(self.write_pilog(times), self.data)
        self.assertEqual(reports[0]["existing"], 2)
        self.assertEqual(reports[0]["added"], 4)
        self.assertTrue(reports[0]["written"])
        lines = path.read_text().splitlines()
        self.assertEqual(lines[0], ",".join(HEADER))
        self.assertEqual(lines[1], pi_rows[0])  # the Pi's own text wins, pi_unix and all
        self.assertEqual(self.night_times("2026-10-14"), times)
        self.assertTrue(lines[2].endswith(","))  # watch rows have no pi_unix
        self.assertEqual(lines[2].split(",")[1], str(times[1]))
        self.assertEqual(len(lines[2].split(",")), len(HEADER))
        self.assertEqual(path.with_name(path.name + ".bak").read_text().splitlines()[1:], pi_rows)

    def test_untouched_when_nothing_to_add(self):
        t = unix(2026, 10, 14, 23, 0)
        path = self.write_night("2026-10-14", [pi_line(t)])
        before = path.read_text()
        reports = merge_pilog.merge(self.write_pilog([t]), self.data)
        self.assertEqual(reports[0]["added"], 0)
        self.assertFalse(reports[0]["written"])
        self.assertEqual(path.read_text(), before)
        self.assertFalse(path.with_name(path.name + ".bak").exists())

    def test_dry_run_writes_nothing(self):
        t = unix(2026, 10, 14, 23, 0)
        reports = merge_pilog.merge(self.write_pilog([t, t + 10]), self.data, dry_run=True)
        self.assertEqual(reports[0]["added"], 2)
        self.assertFalse(reports[0]["written"])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_reports_remaining_gaps(self):
        t = unix(2026, 10, 14, 23, 0)
        reports = merge_pilog.merge(self.write_pilog([t, t + 10], [t + 600, t + 610]), self.data)
        self.assertEqual(reports[0]["gaps"], [(t + 10, t + 600)])
        text = merge_pilog.describe(reports[0])
        self.assertIn("11:00 PM to 11:10 PM (9 min)", text)

    def test_skips_garbled_and_header_lines(self):
        t = unix(2026, 10, 14, 23, 0)
        path = self.write_pilog([t])
        path.write_text(path.read_text() + "garbage,line\n" + watch_row(t + 10) + "\n")
        reports = merge_pilog.merge(path, self.data)
        self.assertEqual(reports[0]["added"], 2)
        self.assertEqual(self.night_times("2026-10-14"), [t, t + 10])

    def test_main_reports_missing_file(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(sys, "argv", ["merge_pilog.py", str(self.dir / "nope.csv")]):
            self.assertEqual(merge_pilog.main(), 1)
        self.assertIn("not a file", out.getvalue())


if __name__ == "__main__":
    unittest.main()
