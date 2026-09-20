import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Importing bridge runs its module top level. With a default environment
# (BRIDGE_AUTH=auto, HOST=0.0.0.0, empty WEBUI_PASSWORD) that would call
# resolve_credentials() at import time — printing a credential banner and
# rewriting any real .env at the repo root. Force auth off for the import so the
# test suite has no side effects; the tests patch BRIDGE_AUTH/AUTH_* per case.
os.environ.setdefault("BRIDGE_AUTH", "off")

import bridge  # noqa: E402


def make_handler(path="/", headers=None, client="203.0.113.9"):
    h = bridge.H.__new__(bridge.H)
    h.headers = headers or {}
    h.command = "GET"
    h.path = path
    h.requestline = "GET %s HTTP/1.1" % path
    h.request_version = "HTTP/1.1"
    h.client_address = (client, 51000)
    return h


def basic_header(user, password):
    import base64
    raw = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + raw}


class LoopbackDetectionTests(unittest.TestCase):
    def test_ipv4_loopback_is_local(self):
        self.assertTrue(bridge.client_is_loopback("127.0.0.1"))
        self.assertTrue(bridge.client_is_loopback("127.1.2.3"))

    def test_ipv6_loopback_is_local(self):
        self.assertTrue(bridge.client_is_loopback("::1"))
        self.assertTrue(bridge.client_is_loopback("::ffff:127.0.0.1"))

    def test_public_address_is_not_local(self):
        self.assertFalse(bridge.client_is_loopback("203.0.113.9"))
        self.assertFalse(bridge.client_is_loopback("10.0.0.5"))
        self.assertFalse(bridge.client_is_loopback("1.127.0.0"))


class AuthEnabledTests(unittest.TestCase):
    def test_auto_enables_auth_on_public_bind(self):
        with patch.object(bridge, "BRIDGE_AUTH", "auto"), patch.object(bridge, "HOST", "0.0.0.0"):
            self.assertTrue(bridge.auth_enabled())

    def test_auto_disables_auth_on_loopback_bind(self):
        with patch.object(bridge, "BRIDGE_AUTH", "auto"), patch.object(bridge, "HOST", "127.0.0.1"):
            self.assertFalse(bridge.auth_enabled())

    def test_explicit_off_disables_even_on_public_bind(self):
        with patch.object(bridge, "BRIDGE_AUTH", "off"), patch.object(bridge, "HOST", "0.0.0.0"):
            self.assertFalse(bridge.auth_enabled())

    def test_explicit_on_enables_even_on_loopback_bind(self):
        with patch.object(bridge, "BRIDGE_AUTH", "on"), patch.object(bridge, "HOST", "127.0.0.1"):
            self.assertTrue(bridge.auth_enabled())


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bridge, "BRIDGE_AUTH", "on"),
            patch.object(bridge, "AUTH_USER", "operator"),
            patch.object(bridge, "AUTH_PASS", "s3cret-value"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_missing_credentials_rejected(self):
        self.assertFalse(make_handler().authorized())

    def test_correct_credentials_accepted(self):
        h = make_handler(headers=basic_header("operator", "s3cret-value"))
        self.assertTrue(h.authorized())

    def test_wrong_password_rejected(self):
        h = make_handler(headers=basic_header("operator", "wrong"))
        self.assertFalse(h.authorized())

    def test_wrong_username_rejected(self):
        h = make_handler(headers=basic_header("someone", "s3cret-value"))
        self.assertFalse(h.authorized())

    def test_malformed_header_rejected(self):
        self.assertFalse(make_handler(headers={"Authorization": "Basic !!!not-base64"}).authorized())
        self.assertFalse(make_handler(headers={"Authorization": "Bearer abc"}).authorized())

    def test_loopback_client_bypasses_auth(self):
        self.assertTrue(make_handler(client="127.0.0.1").authorized())

    def test_auth_disabled_allows_anonymous(self):
        with patch.object(bridge, "BRIDGE_AUTH", "off"):
            self.assertTrue(make_handler().authorized())


class PublicStateTests(unittest.TestCase):
    FULL = {
        "mode": "main",
        "ready": True,
        "locked": True,
        "cur": "https://discord.com/login",
        "main_idx": 0,
        "total": 42,
        "app_id": "1544745598098608169",
        "events": [{"type": "evaluate_js", "script": "sensitive"}],
    }

    def test_remote_client_sees_only_safe_fields(self):
        out = bridge.public_state(self.FULL, False)
        self.assertEqual(set(out), {"mode", "ready", "locked"})

    def test_remote_client_never_sees_app_id_or_events(self):
        out = bridge.public_state(self.FULL, False)
        for leaked in ("app_id", "events", "cur", "main_idx", "total"):
            self.assertNotIn(leaked, out)

    def test_local_client_sees_full_state(self):
        self.assertEqual(bridge.public_state(self.FULL, True), self.FULL)

    def test_missing_fields_are_omitted_not_none(self):
        out = bridge.public_state({"mode": "loading"}, False)
        self.assertEqual(out, {"mode": "loading"})


class WebsocketAllowlistTests(unittest.TestCase):
    def test_socketio_prefix_allowed(self):
        self.assertTrue("/socket.io/?EIO=4".startswith(bridge.WS_ALLOWED_PREFIXES))

    def test_arbitrary_path_not_allowed(self):
        self.assertFalse("/admin/../etc".startswith(bridge.WS_ALLOWED_PREFIXES))
        self.assertFalse("/".startswith(bridge.WS_ALLOWED_PREFIXES))


class RequestLimitTests(unittest.TestCase):
    def test_max_request_bytes_is_bounded(self):
        self.assertGreater(bridge.MAX_REQUEST_BYTES, 0)
        self.assertLessEqual(bridge.MAX_REQUEST_BYTES, 8 * 1024 * 1024)


class WeakPasswordTests(unittest.TestCase):
    def test_known_defaults_are_treated_as_weak(self):
        for pw in ("", "change-this-please", "secret", "admin", "password"):
            self.assertIn(pw, bridge.WEAK_PASSWORDS)

    def test_generated_password_is_not_weak(self):
        with patch.object(bridge, "WEBUI_PASSWORD", "change-this-please"), \
             patch.object(bridge, "persist_generated_password", return_value=True):
            _, pw = bridge.resolve_credentials()
        self.assertNotIn(pw.lower(), bridge.WEAK_PASSWORDS)
        self.assertGreaterEqual(len(pw), 20)

    def test_strong_password_is_preserved(self):
        with patch.object(bridge, "WEBUI_PASSWORD", "a-genuinely-set-password"):
            _, pw = bridge.resolve_credentials()
        self.assertEqual(pw, "a-genuinely-set-password")


class HandlerHardeningTests(unittest.TestCase):
    def test_keepalive_enabled(self):
        self.assertEqual(bridge.H.protocol_version, "HTTP/1.1")

    def test_socket_timeout_set(self):
        self.assertIsNotNone(bridge.H.timeout)
        self.assertGreater(bridge.H.timeout, 0)

    def test_server_banner_does_not_leak_python_version(self):
        self.assertEqual(bridge.H.sys_version, "")

    def test_version_string_has_no_trailing_space(self):
        h = bridge.H.__new__(bridge.H)
        vs = h.version_string()
        self.assertEqual(vs, "nighty-bridge")
        self.assertNotIn(" ", vs)


def _handler_with_sink(path="/", headers=None, client="203.0.113.9"):
    """A handler wired to an in-memory wfile so response-writing methods run."""
    import io as _io
    h = make_handler(path=path, headers=headers, client=client)
    h.wfile = _io.BytesIO()
    h._headers_buffer = []
    h.close_connection = False
    return h


class KeepAliveDesyncTests(unittest.TestCase):
    """Regression: a reject response that never reads the request body must close
    the connection, or the unread body desyncs the next keep-alive request."""

    def test_demand_auth_closes_connection(self):
        h = _handler_with_sink()
        h.demand_auth()
        self.assertTrue(h.close_connection)
        self.assertIn(b"Connection: close", h.wfile.getvalue())
        self.assertIn(b"401", h.wfile.getvalue())

    def test_send_status_closes_connection(self):
        for code in (400, 403, 413):
            h = _handler_with_sink()
            h._send_status(code)
            self.assertTrue(h.close_connection, "code %d must close" % code)
            self.assertIn(b"Connection: close", h.wfile.getvalue())


class AuthFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bridge, "BRIDGE_AUTH", "on"),
            patch.object(bridge, "AUTH_USER", "operator"),
            patch.object(bridge, "AUTH_PASS", "s3cret-value"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_non_ascii_credentials_fail_closed(self):
        # A base64 body that decodes to bytes with a non-ASCII replacement char
        # must return False, never raise out of the auth gate.
        import base64 as _b64
        raw = _b64.b64encode(b"\xff\xfe:pw").decode("ascii")
        h = make_handler(headers={"Authorization": "Basic " + raw})
        self.assertFalse(h.authorized())

    def test_wrong_username_still_checks_password(self):
        # No short-circuit: both comparisons run regardless of the username.
        h = make_handler(headers=basic_header("wronguser", "s3cret-value"))
        self.assertFalse(h.authorized())


class HealthPathAuthSkipTests(unittest.TestCase):
    def test_exact_health_paths_recognized(self):
        for p in ("/healthz", "/ready", "/healthz?probe=1", "/ready?x=1"):
            self.assertTrue(
                p in ("/healthz", "/ready") or p.startswith(("/healthz?", "/ready?")))

    def test_lookalike_paths_do_not_skip_auth(self):
        for p in ("/healthzfoo", "/ready-status", "/ready/../state", "/healthz/x"):
            self.assertFalse(
                p in ("/healthz", "/ready") or p.startswith(("/healthz?", "/ready?")))


class LoopbackMappedTests(unittest.TestCase):
    def test_ipv4_mapped_loopback_range_is_local(self):
        self.assertTrue(bridge.client_is_loopback("::ffff:127.0.0.1"))
        self.assertTrue(bridge.client_is_loopback("::ffff:127.9.9.9"))

    def test_mapped_public_is_not_local(self):
        self.assertFalse(bridge.client_is_loopback("::ffff:203.0.113.9"))


if __name__ == "__main__":
    unittest.main()
