import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watch import watch_logger  # noqa: E402
from watch.watch_logger import Recorder, clock12, command_js, read_command, write_status  # noqa: E402

GOOD_LINE = "PL,1792025223,125,1.234,0.050,0.01,-0.70,0.71,55,80,90"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def quietly(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            result = fn(*args)
        return result, out.getvalue()


class ReadCommandTest(TempDirTest):
    def write_cmd(self, content):
        path = self.dir / "watch-cmd.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    def test_returns_cmd_and_deletes_file(self):
        now = time.time()
        path = self.write_cmd({"cmd": "alarm", "sent": int(now) - 10})
        self.assertEqual(read_command(path, now), "alarm")
        self.assertFalse(path.exists())

    def test_missing_file_is_none(self):
        self.assertIsNone(read_command(self.dir / "watch-cmd.json", time.time()))

    def test_stale_command_is_dropped_and_deleted(self):
        now = time.time()
        path = self.write_cmd({"cmd": "alarm", "sent": int(now) - 6 * 60})
        result, out = self.quietly(read_command, path, now)
        self.assertIsNone(result)
        self.assertFalse(path.exists())
        self.assertIn("stale", out)

    def test_malformed_json_is_dropped_and_deleted(self):
        for content in ['{"cmd": "alarm", "sent"', '["alarm"]', '{"cmd": "alarm"}', '{"cmd": "buzz", "sent": "soon"}']:
            with self.subTest(content=content):
                path = self.write_cmd(content)
                result, out = self.quietly(read_command, path, time.time())
                self.assertIsNone(result)
                self.assertFalse(path.exists())
                self.assertIn("bad command file", out)

    def test_undecodable_file_is_dropped_and_deleted(self):
        path = self.dir / "watch-cmd.json"
        path.write_bytes(b'{"cmd": "\x81\x8d\x8f\x90\x9d"}')  # invalid in UTF-8 and in cp1252
        result, out = self.quietly(read_command, path, time.time())
        self.assertIsNone(result)
        self.assertFalse(path.exists())
        self.assertIn("unreadable command file", out)

    def test_unreadable_undeletable_path_does_not_raise(self):
        path = self.dir / "watch-cmd.json"
        path.mkdir()  # read_text and unlink both fail with OSError
        result, out = self.quietly(read_command, path, time.time())
        self.assertIsNone(result)
        self.assertIn("unreadable command file", out)
        self.assertIn("could not delete", out)

    def test_command_that_cannot_be_deleted_is_not_sent(self):
        now = time.time()
        path = self.write_cmd({"cmd": "alarm", "sent": int(now)})
        with mock.patch.object(type(path), "unlink", side_effect=PermissionError("denied")):
            result, out = self.quietly(read_command, path, now)
        self.assertIsNone(result)
        self.assertIn("could not delete", out)


class CommandJsTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(command_js("alarm"), '\x10if(!global.PLA)load("pilog.alarm.js")\n')
        self.assertEqual(command_js("stop"), "\x10load()\n")
        self.assertEqual(command_js("buzz"), "\x10Bangle.buzz(1000)\n")
        self.assertIsNone(command_js("reboot"))


class WriteStatusTest(TempDirTest):
    def test_writes_four_keys_atomically(self):
        path = self.dir / "watch-status.json"
        before = int(time.time())
        write_status(path, True, 1792025223, None)
        status = json.loads(path.read_text())
        self.assertEqual(set(status), {"connected", "last_row", "awake", "updated"})
        self.assertIs(status["connected"], True)
        self.assertEqual(status["last_row"], 1792025223)
        self.assertIsNone(status["awake"])
        self.assertIsInstance(status["updated"], int)
        self.assertGreaterEqual(status["updated"], before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["watch-status.json"])

    def test_overwrites_previous_status(self):
        path = self.dir / "watch-status.json"
        write_status(path, True, 1, None)
        write_status(path, False, 1, 2)
        status = json.loads(path.read_text())
        self.assertEqual((status["connected"], status["awake"]), (False, 2))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["watch-status.json"])


class RecorderTest(TempDirTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(watch_logger, "DATA_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.status_path = self.dir / "watch-status.json"
        self.recorder = Recorder(self.status_path)

    def csv_lines(self):
        path = watch_logger.night_file(datetime.now())
        return path.read_text().splitlines() if path.exists() else []

    def status(self):
        return json.loads(self.status_path.read_text())

    def test_good_line_writes_a_row_and_status(self):
        self.quietly(self.recorder.handle_line, GOOD_LINE)
        lines = self.csv_lines()
        self.assertEqual(lines[0], ",".join(watch_logger.HEADER))
        self.assertEqual(len(lines), 2)
        fields = lines[1].split(",")
        self.assertEqual(len(fields), len(watch_logger.HEADER))
        self.assertEqual(fields[1:-1], GOOD_LINE[3:].split(","))
        self.assertEqual(self.recorder.rows, 1)
        self.assertEqual(self.status()["last_row"], int(fields[-1]))

    def test_garbled_line_is_skipped(self):
        _, out = self.quietly(self.recorder.handle_line, "PL,1792025223,125,1.2")
        self.assertEqual(self.csv_lines(), [])
        self.assertEqual(self.recorder.rows, 0)
        self.assertIn("garbled", out)

    def test_console_chatter_is_ignored(self):
        _, out = self.quietly(self.recorder.handle_line, "=undefined")
        self.assertEqual(self.csv_lines(), [])
        self.assertEqual(out, "")

    def test_awake_line_marks_awake(self):
        self.quietly(self.recorder.handle_line, GOOD_LINE)
        _, out = self.quietly(self.recorder.handle_line, "PL,AWAKE,1792025460")
        self.assertEqual(self.recorder.awake, 1792025460)
        self.assertEqual(self.status()["awake"], 1792025460)
        self.assertEqual(len(self.csv_lines()), 2)  # no data row for it
        self.assertNotIn("garbled", out)
        self.assertIn("watch button pressed: awake at " + clock12(datetime.fromtimestamp(1792025460)), out)

    def test_awake_with_garbled_time_uses_pi_time(self):
        before = int(time.time())
        self.quietly(self.recorder.handle_line, "PL,AWAKE,")
        self.assertGreaterEqual(self.recorder.awake, before)

    def test_connected_flag_is_saved(self):
        self.recorder.set_connected(True)
        self.assertIs(self.status()["connected"], True)
        self.recorder.set_connected(False)
        self.assertIs(self.status()["connected"], False)


class Clock12Test(unittest.TestCase):
    def test_twelve_hour_times(self):
        self.assertEqual(clock12(datetime(2026, 10, 14, 6, 31)), "6:31 AM")
        self.assertEqual(clock12(datetime(2026, 10, 14, 0, 5)), "12:05 AM")
        self.assertEqual(clock12(datetime(2026, 10, 14, 12, 0)), "12:00 PM")
        self.assertEqual(clock12(datetime(2026, 10, 14, 18, 7, 9), seconds=True), "6:07:09 PM")


if __name__ == "__main__":
    unittest.main()
