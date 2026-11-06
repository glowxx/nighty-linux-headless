import os
import sys
import unittest
import io
import json
import socket
import threading
import tempfile
import subprocess
import http.client
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402
import enforce_config  # noqa: E402


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
        with patch.object(bridge, "BRIDGE_AUTH", "auto"), patch.object(bridge, "HOST", "0.0.0.0"):
            self.assertTrue(make_handler(client="127.0.0.1").authorized())

    def test_forced_auth_rejects_anonymous_loopback(self):
        self.assertFalse(make_handler(client="127.0.0.1").authorized())

    def test_unicode_credentials_are_supported(self):
        with patch.object(bridge, "AUTH_USER", "operatör"), patch.object(bridge, "AUTH_PASS", "pässwörd"):
            self.assertTrue(make_handler(headers=basic_header("operatör", "pässwörd")).authorized())

    def test_basic_scheme_is_case_insensitive(self):
        headers = basic_header("operator", "s3cret-value")
        headers["Authorization"] = headers["Authorization"].replace("Basic", "basic")
        self.assertTrue(make_handler(headers=headers).authorized())

    def test_invalid_base64_is_not_ignored(self):
        headers = basic_header("operator", "s3cret-value")
        headers["Authorization"] += "!"
        self.assertFalse(make_handler(headers=headers).authorized())

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
    def attempt_upgrade(self, path, extra_headers=None):
        headers = {"Upgrade": "websocket"}
        headers.update(extra_headers or {})
        h = _handler_with_sink(path, headers)
        with ExitStack() as stack:
            stack.enter_context(patch.object(h, "authorized", return_value=True))
            for name, value in (("web_up", True), ("panel_blocked", False),
                                ("setup_locked", True), ("add_account_active", False)):
                stack.enter_context(patch.object(bridge, name, return_value=value))
            tunnel = stack.enter_context(patch.object(h, "_proxy_ws"))
            h.do_GET()
            return tunnel.called, h.wfile.getvalue()

    def test_socketio_and_ws_endpoints_allowed(self):
        for path in ("/socket.io/?EIO=4&transport=websocket", "/socket.io", "/ws", "/ws/?x=1"):
            with self.subTest(path=path):
                self.assertTrue(self.attempt_upgrade(path)[0])

    def test_lookalikes_and_traversal_are_not_tunnelled(self):
        for path in ("/admin", "/", "/ws-admin", "/socket.io-admin",
                     "/ws/../admin", "/ws/%2e%2e/admin", "/ws%2f..%2fadmin"):
            with self.subTest(path=path):
                tunnelled, response = self.attempt_upgrade(path)
                self.assertFalse(tunnelled)
                self.assertIn(b"403", response)

    def test_cross_site_upgrade_is_rejected_even_after_authentication(self):
        tunnelled, response = self.attempt_upgrade("/socket.io/", {
            "Host": "nighty.example", "Origin": "https://attacker.example"})
        self.assertFalse(tunnelled)
        self.assertIn(b"403", response)

    def test_same_origin_https_proxy_upgrade_is_allowed(self):
        tunnelled, _ = self.attempt_upgrade("/socket.io/", {
            "Host": "nighty.example", "Origin": "https://nighty.example"})
        self.assertTrue(tunnelled)


class RequestLimitTests(unittest.TestCase):
    def test_max_request_bytes_is_bounded(self):
        self.assertGreater(bridge.MAX_REQUEST_BYTES, 0)
        self.assertLessEqual(bridge.MAX_REQUEST_BYTES, 8 * 1024 * 1024)


class WeakPasswordTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.object(bridge, "env", side_effect=lambda key, default=None: default)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_known_defaults_are_treated_as_weak(self):
        for pw in ("", "change-this-please", "secret", "admin", "password"):
            self.assertIn(pw, bridge.WEAK_PASSWORDS)

    def test_generated_password_is_not_weak(self):
        with patch.object(bridge, "WEBUI_PASSWORD", "change-this-please"), \
             patch.object(bridge, "persist_generated_password", return_value=True), \
             redirect_stdout(io.StringIO()):
            _, pw = bridge.resolve_credentials()
        self.assertNotIn(pw.lower(), bridge.WEAK_PASSWORDS)
        self.assertGreaterEqual(len(pw), 20)

    def test_strong_password_is_preserved(self):
        with patch.object(bridge, "WEBUI_PASSWORD", "a-genuinely-set-password"):
            _, pw = bridge.resolve_credentials()
        self.assertEqual(pw, "a-genuinely-set-password")

    def test_failed_persistence_stops_startup_without_printing_a_password(self):
        output = io.StringIO()
        with patch.object(bridge, "WEBUI_PASSWORD", ""), \
             patch.object(bridge, "persist_generated_password", return_value=False), \
             redirect_stdout(output), self.assertRaises(RuntimeError):
            bridge.resolve_credentials()
        self.assertEqual(output.getvalue(), "")


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
        with patch.object(bridge.hmac, "compare_digest", wraps=bridge.hmac.compare_digest) as compare:
            self.assertFalse(h.authorized())
        self.assertEqual(compare.call_count, 2)


class HealthPathAuthSkipTests(unittest.TestCase):
    def test_exact_health_paths_recognized(self):
        for path in ("/healthz", "/ready", "/healthz?probe=1", "/ready?x=1"):
            h = _handler_with_sink(path)
            with patch.object(h, "authorized", return_value=False), \
                 patch.object(bridge, "web_up", return_value=False):
                h.do_GET()
            payload = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual(payload, {"status": "starting", "ready": False})

    def test_lookalike_paths_do_not_skip_auth(self):
        for path in ("/healthzfoo", "/ready-status", "/ready/../state", "/healthz/x"):
            h = _handler_with_sink(path)
            with patch.object(h, "authorized", return_value=False):
                h.do_GET()
            self.assertIn(b"401", h.wfile.getvalue())


class LoopbackMappedTests(unittest.TestCase):
    def test_ipv4_mapped_loopback_range_is_local(self):
        self.assertTrue(bridge.client_is_loopback("::ffff:127.0.0.1"))
        self.assertTrue(bridge.client_is_loopback("::ffff:127.9.9.9"))

    def test_mapped_public_is_not_local(self):
        self.assertFalse(bridge.client_is_loopback("::ffff:203.0.113.9"))

    def test_hexadecimal_and_expanded_ipv6_loopback(self):
        self.assertTrue(bridge.client_is_loopback("::ffff:7f00:1"))
        self.assertTrue(bridge.client_is_loopback("0:0:0:0:0:0:0:1"))
        self.assertFalse(bridge.client_is_loopback("127.attacker.example"))


class CredentialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"
        env_patch = patch.dict(os.environ, {"NIGHTY_ENV": str(self.path)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        cache = dict(enforce_config._ENV_CACHE)
        self.addCleanup(enforce_config._ENV_CACHE.update, cache)

    def test_restart_uses_saved_password_and_panel_uses_same_credentials(self):
        self.path.write_text("WEBUI_USERNAME=operator\nWEBUI_PASSWORD=change-this-please\n", encoding="utf-8")
        output = io.StringIO()
        with patch.object(bridge, "WEBUI_PASSWORD", "change-this-please"), redirect_stdout(output):
            first = bridge.resolve_credentials()
            second = bridge.resolve_credentials()
        self.assertEqual(first, second)
        self.assertEqual(output.getvalue().count("generated credentials"), 1)
        appdata = Path(self.tmp.name) / "appdata"
        appdata.mkdir()
        enforce_config.enforce_web(str(appdata))
        panel = json.loads((appdata / "web_config.json").read_text(encoding="utf-8"))
        self.assertEqual((panel["username"], panel["password"]), first)

    def test_exported_and_duplicate_assignments_are_replaced(self):
        self.path.write_text("# keep this\n export WEBUI_PASSWORD = 'secret'\nWEBUI_PASSWORD=admin\nOTHER=a$(id)b\n", encoding="utf-8")
        self.assertTrue(bridge.persist_generated_password("strong-new-value", "operator"))
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("WEBUI_PASSWORD=strong-new-value"), 2)
        self.assertIn("# keep this\n", text)
        self.assertIn("OTHER=a$(id)b\n", text)
        self.assertEqual(enforce_config.env("WEBUI_PASSWORD"), "strong-new-value")

    def test_new_file_and_temp_are_private_from_creation(self):
        original_replace = bridge.os.replace
        def inspect_temp(src, dst):
            if os.name != "nt":
                self.assertEqual(Path(src).stat().st_mode & 0o777, 0o600)
            original_replace(src, dst)
        with patch.object(bridge.os, "replace", side_effect=inspect_temp):
            self.assertTrue(bridge.persist_generated_password("strong-new-value", "operator"))
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_failed_replace_keeps_old_file_and_removes_temp(self):
        self.path.write_text("WEBUI_PASSWORD=secret\n", encoding="utf-8")
        with patch.object(bridge.os, "replace", side_effect=OSError("read-only")):
            self.assertFalse(bridge.persist_generated_password("strong-new-value"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "WEBUI_PASSWORD=secret\n")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_atomic_replacement_refreshes_cache_with_unchanged_size_and_timestamp(self):
        old = "change-this-please"
        new = "s" * len(old)
        self.path.write_text("WEBUI_PASSWORD=" + old + "\n", encoding="utf-8", newline="\n")
        self.assertEqual(enforce_config.env("WEBUI_PASSWORD"), old)
        stat = self.path.stat()
        original_replace = bridge.os.replace
        def replace_with_same_timestamp(src, dst):
            os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            original_replace(src, dst)
        with patch.object(bridge.os, "replace", side_effect=replace_with_same_timestamp):
            self.assertTrue(bridge.persist_generated_password(new))
        self.assertEqual(self.path.stat().st_size, stat.st_size)
        self.assertEqual(enforce_config.env("WEBUI_PASSWORD"), new)

    def test_predictable_old_temp_path_is_not_overwritten(self):
        decoy = self.path.with_suffix(".tmp")
        decoy.write_text("leave this alone", encoding="utf-8")
        self.assertTrue(bridge.persist_generated_password("strong-new-value"))
        self.assertEqual(decoy.read_text(encoding="utf-8"), "leave this alone")

    def test_import_does_not_generate_or_write_credentials(self):
        env = os.environ.copy()
        env.update(PYTHONPATH=str(SCRIPTS), BRIDGE_AUTH="on", WEBUI_PASSWORD="")
        result = subprocess.run([sys.executable, "-c", "import bridge"], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.path.exists())


class ServerErrorTests(unittest.TestCase):
    def invoke_error(self, error):
        server = bridge.BridgeServer.__new__(bridge.BridgeServer)
        def handler(*args):
            raise error
        server.RequestHandlerClass = handler
        output = io.StringIO()
        with patch.object(server, "shutdown_request"), redirect_stderr(output):
            server.process_request_thread(object(), ("203.0.113.9", 51000))
        return output.getvalue()

    def test_client_disconnect_errors_are_suppressed_by_server(self):
        for error in (BrokenPipeError(), ConnectionResetError(), socket.timeout(), TimeoutError()):
            self.assertEqual(self.invoke_error(error), "")

    def test_unexpected_error_is_logged_without_payload_or_traceback(self):
        self.assertEqual(self.invoke_error(RuntimeError("sensitive payload")),
                         "[bridge] RuntimeError from 203.0.113.9\n")


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("BRIDGE_AUTH", "on"), ("AUTH_USER", "operator"),
                            ("AUTH_PASS", "s3cret-value"), ("MAX_REQUEST_BYTES", 32)):
            self.stack.enter_context(patch.object(bridge, name, value))
        for name, value in (("web_up", True), ("panel_blocked", False),
                            ("setup_locked", True), ("add_account_active", False)):
            self.stack.enter_context(patch.object(bridge, name, return_value=value))
        self.state = self.stack.enter_context(patch.object(bridge, "get_state", return_value=PublicStateTests.FULL))
        self.server = bridge.BridgeServer(("127.0.0.1", 0), bridge.H)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, path, headers=None, method="GET", body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw_request(self, data):
        with socket.create_connection(self.server.server_address, timeout=3) as client:
            client.sendall(data)
            client.shutdown(socket.SHUT_WR)
            parts = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    return b"".join(parts)
                parts.append(chunk)

    def test_forced_auth_protects_real_loopback_requests(self):
        status, headers, _ = self.request("/state")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        status, _, body = self.request("/state", basic_header("operator", "s3cret-value"))
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"mode", "ready", "locked"})
        self.assertEqual(self.request("/events", basic_header("operator", "s3cret-value"))[0], 404)

    def test_auto_retains_internal_diagnostics(self):
        with patch.object(bridge, "BRIDGE_AUTH", "auto"), patch.object(bridge, "HOST", "0.0.0.0"):
            status, _, body = self.request("/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), PublicStateTests.FULL)

    def test_forwarding_headers_cannot_disable_forced_auth(self):
        self.assertEqual(self.request("/state", {"X-Forwarded-For": "127.0.0.1"})[0], 401)

    def test_cross_origin_post_is_rejected_with_correct_credentials(self):
        wrong_port = "http://127.0.0.1:%d" % (self.server.server_address[1] + 1)
        for origin in ("https://attacker.example", "null", "http://user@127.0.0.1", wrong_port):
            headers = basic_header("operator", "s3cret-value")
            headers["Origin"] = origin
            status, response_headers, _ = self.request("/rpc", headers, method="POST")
            self.assertEqual(status, 403)
            self.assertEqual(response_headers["Connection"], "close")

    def test_same_origin_post_reaches_the_locked_setup_gate(self):
        headers = basic_header("operator", "s3cret-value")
        headers["Origin"] = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.assertEqual(self.request("/rpc", headers, method="POST")[0], 404)

    def test_duplicate_authorization_headers_are_rejected(self):
        auth = basic_header("operator", "s3cret-value")["Authorization"].encode("ascii")
        response = self.raw_request(b"GET /state HTTP/1.1\r\nHost: localhost\r\nAuthorization: "
                                    + auth + b"\r\nAuthorization: Basic invalid\r\n\r\n")
        self.assertIn(b"HTTP/1.1 401", response)

    def test_anonymous_health_returns_only_minimal_status(self):
        for path in ("/healthz", "/ready?probe=1"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"status": "ok", "ready": True})
            self.assertEqual(headers["Server"], "nighty-bridge")

    def test_authenticated_health_has_details(self):
        status, _, body = self.request("/healthz", basic_header("operator", "s3cret-value"))
        self.assertEqual(status, 200)
        self.assertIn("uptime_seconds", json.loads(body))

    def test_ambiguous_framing_closes_before_a_second_request(self):
        auth = basic_header("operator", "s3cret-value")["Authorization"].encode("ascii")
        for method, framing, body in (
            (b"POST", b"Transfer-Encoding: chunked\r\n", b"0\r\n\r\n"),
            (b"POST", b"Content-Length: 0\r\nContent-Length: 5\r\n", b"abcde"),
            (b"GET", b"Content-Length: 5\r\n", b"abcde"),
        ):
            with self.subTest(method=method, framing=framing):
                data = method + b" /state HTTP/1.1\r\nHost: localhost\r\nAuthorization: " + auth + b"\r\n" + framing + b"\r\n" + body
                data += b"GET /state HTTP/1.1\r\nHost: localhost\r\nAuthorization: " + auth + b"\r\n\r\n"
                response = self.raw_request(data)
                self.assertIn(b"HTTP/1.1 400", response)
                self.assertIn(b"Connection: close", response)
                self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.state.assert_not_called()

    def test_expect_continue_does_not_accept_unauthorized_or_oversized_body(self):
        requests = (
            (b"", b"1", b"401"),
            (b"Authorization: " + basic_header("operator", "s3cret-value")["Authorization"].encode("ascii") + b"\r\n", b"33", b"413"),
        )
        for auth, length, expected in requests:
            response = self.raw_request(b"POST /rpc HTTP/1.1\r\nHost: localhost\r\n" + auth + b"Content-Length: " + length + b"\r\nExpect: 100-continue\r\n\r\n")
            self.assertIn(b"HTTP/1.1 " + expected, response)
            self.assertNotIn(b"100 Continue", response)

    def test_incomplete_body_is_rejected(self):
        auth = basic_header("operator", "s3cret-value")["Authorization"].encode("ascii")
        response = self.raw_request(b"POST /rpc HTTP/1.1\r\nHost: localhost\r\nAuthorization: " + auth + b"\r\nContent-Length: 5\r\n\r\nx")
        self.assertIn(b"HTTP/1.1 400", response)

    def test_valid_requests_reuse_connection(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request("GET", "/state", headers=basic_header("operator", "s3cret-value"))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            first_socket = connection.sock
            connection.request("GET", "/ready")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertIs(connection.sock, first_socket)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
