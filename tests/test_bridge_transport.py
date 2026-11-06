import http.server
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402

OPENSSL = shutil.which("openssl")


@unittest.skipUnless(OPENSSL, "openssl is required for the local TLS fixture")
class TokenTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key = Path(tmp.name) / "key.pem"
        self.cert = Path(tmp.name) / "cert.pem"
        result = subprocess.run([
            OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(self.cert), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.received = []
        received = self.received
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                body = json.dumps({"id": "test-user"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(key))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.hostname = "localhost"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def local_urlopen(self, request, **kwargs):
        url = "https://%s:%d/" % (self.hostname, self.server.server_address[1])
        local = urllib.request.Request(url, headers=dict(request.header_items()))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPSHandler(context=kwargs["context"]))
        return opener.open(local, timeout=3)

    def test_untrusted_certificate_cannot_receive_account_or_bot_tokens(self):
        with patch.object(bridge.urllib.request, "urlopen", side_effect=self.local_urlopen):
            for fetch in (bridge._discord_bot_get, bridge._discord_account_get):
                self.assertEqual(fetch("synthetic-test-token", "/users/@me"), (-1, None))
        self.assertEqual(self.received, [])

    def test_valid_trusted_certificate_allows_authenticated_request(self):
        context = ssl.create_default_context(cafile=str(self.cert))
        with patch.object(bridge, "_ctx", context), \
             patch.object(bridge.urllib.request, "urlopen", side_effect=self.local_urlopen):
            status, payload = bridge._discord_bot_get("synthetic-test-token", "/users/@me")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"id": "test-user"})
        self.assertEqual(self.received, ["Bot synthetic-test-token"])

    def test_trusted_certificate_for_wrong_hostname_cannot_receive_token(self):
        self.hostname = "127.0.0.1"
        context = ssl.create_default_context(cafile=str(self.cert))
        with patch.object(bridge, "_ctx", context), \
             patch.object(bridge.urllib.request, "urlopen", side_effect=self.local_urlopen):
            self.assertEqual(bridge._discord_bot_get("synthetic-test-token", "/users/@me"), (-1, None))
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
