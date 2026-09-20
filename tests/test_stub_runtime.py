import ast
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import repack  # noqa: E402


def load_stub(event_cap="2000", log_path=""):
    os.environ["NIGHTY_STUB_EVENT_CAP"] = event_cap
    os.environ["NIGHTY_STUB_LOG"] = log_path
    mod = types.ModuleType("stubwv_under_test")
    exec(compile(repack.STUB_INIT, "STUB_INIT", "exec"), mod.__dict__)
    return mod


class StubSourceTests(unittest.TestCase):
    def test_stub_init_is_valid_python(self):
        ast.parse(repack.STUB_INIT)

    def test_dispatch_loop_blocks_instead_of_polling(self):
        self.assertNotIn("_taskq.get(timeout=", repack.STUB_INIT)
        self.assertIn("_taskq.get()", repack.STUB_INIT)


class EventBufferTests(unittest.TestCase):
    def test_events_are_capped(self):
        mod = load_stub(event_cap="5")
        for i in range(12):
            mod._emit("evaluate_js", uid="w", script="x%d" % i)
        self.assertEqual(len(mod._events), 5)

    def test_sequence_numbers_stay_monotonic_after_trimming(self):
        mod = load_stub(event_cap="5")
        for i in range(12):
            mod._emit("evaluate_js", uid="w", script="x%d" % i)
        self.assertEqual([e["seq"] for e in mod._events], [7, 8, 9, 10, 11])
        self.assertEqual(mod._evbase, 7)

    def test_cap_of_zero_means_unbounded(self):
        mod = load_stub(event_cap="0")
        for i in range(50):
            mod._emit("load_url", uid="w", url="u%d" % i)
        self.assertEqual(len(mod._events), 50)
        self.assertEqual(mod._evbase, 0)

    def test_uncapped_sequence_matches_index(self):
        mod = load_stub(event_cap="0")
        for i in range(10):
            mod._emit("load_url", uid="w", url="u%d" % i)
        self.assertEqual([e["seq"] for e in mod._events], list(range(10)))

    def test_non_numeric_cap_falls_back_to_default(self):
        # A typo'd env value must not crash the stub import; it falls back to 2000.
        mod = load_stub(event_cap="not-a-number")
        self.assertEqual(mod._EV_CAP, 2000)

    def test_negative_cap_falls_back_to_default(self):
        # A negative cap would read as "unbounded" (the opposite of intent);
        # it must fall back to the default instead.
        mod = load_stub(event_cap="-5")
        self.assertEqual(mod._EV_CAP, 2000)


class LogDedupeTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="stubwv-", suffix=".log")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def read_lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read().strip().splitlines()

    def test_repeated_lines_are_collapsed(self):
        mod = load_stub(log_path=self.path)
        for _ in range(500):
            mod._log("CTL call api[0].getUserProfiles (dispatch to main)")
        mod._log("a different line")
        lines = self.read_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("repeated 499 more times", lines[1])

    def test_distinct_lines_are_all_written(self):
        mod = load_stub(log_path=self.path)
        for i in range(5):
            mod._log("line %d" % i)
        self.assertEqual(len(self.read_lines()), 5)

    def test_log_handle_is_reused_not_reopened(self):
        mod = load_stub(log_path=self.path)
        mod._log("first")
        handle = mod._logstate["fh"]
        mod._log("second")
        self.assertIs(mod._logstate["fh"], handle)

    def test_logging_survives_an_unwritable_path(self):
        mod = load_stub(log_path="/nonexistent-dir/stub.log")
        mod._log("should not raise")
        self.assertFalse(mod._logstate["fh"])


class PayloadTrimmingTests(unittest.TestCase):
    def test_evaluate_js_stores_truncated_script(self):
        self.assertIn("_emit('evaluate_js', uid=self.uid, script=ss)", repack.STUB_INIT)
        self.assertNotIn("_emit('evaluate_js', uid=self.uid, script=s)", repack.STUB_INIT)

    def test_load_html_stores_length_not_body(self):
        self.assertIn("html_len=len(content)", repack.STUB_INIT)
        self.assertNotIn("html=content", repack.STUB_INIT)


if __name__ == "__main__":
    unittest.main()
