import ast
import os
import sys
import tempfile
import types
import unittest
import atexit
import json
import threading
import http.client
import http.server
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import repack  # noqa: E402


def load_stub(event_cap="2000", log_path=""):
    mod = types.ModuleType("stubwv_under_test")
    with patch.dict(os.environ, {"NIGHTY_STUB_EVENT_CAP": event_cap, "NIGHTY_STUB_LOG": log_path}):
        exec(compile(repack.STUB_INIT, "STUB_INIT", "exec"), mod.__dict__)
    atexit.unregister(mod._close_log)
    return mod


class StubTestCase(unittest.TestCase):
    def load_stub(self, **kwargs):
        mod = load_stub(**kwargs)
        self.addCleanup(mod._close_log)
        return mod


class StubSourceTests(StubTestCase):
    def test_stub_init_is_valid_python(self):
        ast.parse(repack.STUB_INIT)

    def test_dispatch_loop_blocks_instead_of_polling(self):
        mod = self.load_stub()
        done = threading.Event()
        box, calls, thread_ids = {}, [], []
        class StopDispatch(BaseException):
            pass
        class Tasks:
            def get(self, *args, **kwargs):
                calls.append((args, kwargs))
                if len(calls) > 1:
                    raise StopDispatch
                def work():
                    thread_ids.append(threading.get_ident())
                    return "done"
                return work, (), {}, box, done
        mod._taskq = Tasks()
        with patch.object(mod.threading, "Thread"), self.assertRaises(StopDispatch):
            mod.start()
        self.assertEqual(calls, [((), {}), ((), {})])
        self.assertEqual(box, {"result": "done"})
        self.assertTrue(done.is_set())
        self.assertEqual(thread_ids, [threading.get_ident()])


class EventBufferTests(StubTestCase):
    def test_events_are_capped(self):
        mod = self.load_stub(event_cap="5")
        for i in range(12):
            mod._emit("evaluate_js", uid="w", script="x%d" % i)
        self.assertEqual(len(mod._events), 5)

    def test_sequence_numbers_stay_monotonic_after_trimming(self):
        mod = self.load_stub(event_cap="5")
        for i in range(12):
            mod._emit("evaluate_js", uid="w", script="x%d" % i)
        self.assertEqual([e["seq"] for e in mod._events], [7, 8, 9, 10, 11])
        self.assertEqual(mod._evbase, 7)

    def test_cap_of_zero_means_unbounded(self):
        mod = self.load_stub(event_cap="0")
        for i in range(50):
            mod._emit("load_url", uid="w", url="u%d" % i)
        self.assertEqual(len(mod._events), 50)
        self.assertEqual(mod._evbase, 0)

    def test_uncapped_sequence_matches_index(self):
        mod = self.load_stub(event_cap="0")
        for i in range(10):
            mod._emit("load_url", uid="w", url="u%d" % i)
        self.assertEqual([e["seq"] for e in mod._events], list(range(10)))

    def test_non_numeric_cap_falls_back_to_default(self):
        # A typo'd env value must not crash the stub import; it falls back to 2000.
        mod = self.load_stub(event_cap="not-a-number")
        self.assertEqual(mod._EV_CAP, 2000)

    def test_negative_cap_falls_back_to_default(self):
        # A negative cap would read as "unbounded" (the opposite of intent);
        # it must fall back to the default instead.
        mod = self.load_stub(event_cap="-5")
        self.assertEqual(mod._EV_CAP, 2000)

    def test_event_endpoint_respects_since_after_trimming(self):
        mod = self.load_stub(event_cap="5")
        for i in range(12):
            mod._emit("evaluate_js", uid="w", script="x%d" % i)
        ready = threading.Event()
        servers = []
        original_server = http.server.ThreadingHTTPServer
        def create_server(address, handler):
            server = original_server(("127.0.0.1", 0), handler)
            servers.append(server)
            ready.set()
            return server
        with patch.object(http.server, "ThreadingHTTPServer", side_effect=create_server):
            mod._start_ctl()
            self.assertTrue(ready.wait(3), "control server did not bind")
        server = servers[0]
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for since, expected in (("0", [7, 8, 9, 10, 11]), ("9", [9, 10, 11]),
                                ("12", []), ("-1", [7, 8, 9, 10, 11]), ("invalid", [7, 8, 9, 10, 11])):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request("GET", "/bridge/events?since=" + since)
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual([event["seq"] for event in payload["events"]], expected)
                self.assertEqual(payload["total"], 12)
            finally:
                connection.close()


class LogDedupeTests(StubTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "stub.log")

    def read_lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read().strip().splitlines()

    def test_repeated_lines_are_collapsed(self):
        mod = self.load_stub(log_path=self.path)
        for _ in range(500):
            mod._log("CTL call api[0].getUserProfiles (dispatch to main)")
        mod._log("a different line")
        lines = self.read_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("repeated 499 more times", lines[1])

    def test_distinct_lines_are_all_written(self):
        mod = self.load_stub(log_path=self.path)
        for i in range(5):
            mod._log("line %d" % i)
        self.assertEqual(len(self.read_lines()), 5)

    def test_log_handle_is_reused_not_reopened(self):
        mod = self.load_stub(log_path=self.path)
        mod._log("first")
        handle = mod._logstate["fh"]
        mod._log("second")
        self.assertIs(mod._logstate["fh"], handle)

    def test_logging_survives_an_unwritable_path(self):
        mod = self.load_stub(log_path=str(Path(self.path) / "nonexistent" / "stub.log"))
        mod._log("should not raise")
        self.assertFalse(mod._logstate["fh"])

    def test_final_repetition_summary_is_flushed_and_handle_closed(self):
        mod = self.load_stub(log_path=self.path)
        for _ in range(10):
            mod._log("repeated")
        handle = mod._logstate["fh"]
        mod._close_log()
        self.assertTrue(handle.closed)
        self.assertEqual(len(self.read_lines()), 2)
        self.assertIn("repeated 9 more times", self.read_lines()[1])


class PayloadTrimmingTests(StubTestCase):
    def test_evaluate_js_stores_truncated_script(self):
        mod = self.load_stub()
        window = mod.Window("master", "test")
        callbacks = []
        window.evaluate_js("x" * 10000, callback=callbacks.append)
        self.assertLess(len(mod._events[-1]["script"]), 200)
        self.assertEqual(callbacks, [None])

    def test_load_html_stores_length_not_body(self):
        mod = self.load_stub()
        window = mod.Window("master", "test")
        window.load_html("x" * 10000)
        event = mod._events[-1]
        self.assertEqual(event["html_len"], 10000)
        self.assertNotIn("html", event)


if __name__ == "__main__":
    unittest.main()
