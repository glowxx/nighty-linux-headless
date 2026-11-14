import sys
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge  # noqa: E402


class HealthzEndpointTests(unittest.TestCase):
    def test_provision_selects_account_for_new_runtime(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, 'find_appdata', return_value=directory):
            self.assertTrue(bridge._write_login('first', 'account', '123', 'bot'))
            self.assertTrue(bridge._write_login('second', 'account2', '456', 'bot2', make_active=False))
            data = json.loads((Path(directory) / 'nighty.config').read_text())
            self.assertEqual(data['active_account'], 'first')
            self.assertTrue(data['logins']['first']['active'])
            self.assertFalse(data['logins']['second']['active'])
    def test_api_path_includes_healthz(self):
        # Create dummy request handler instance
        handler = bridge.H.__new__(bridge.H)
        handler.headers = {}
        handler.command = "GET"
        handler.requestline = "GET /healthz HTTP/1.1"
        handler.request_version = "HTTP/1.1"

        with patch.object(bridge, "web_up", return_value=True), \
             patch.object(bridge, "panel_blocked", return_value=False), \
             patch.object(bridge.H, "_send") as mock_send:

            handler.path = "/healthz"
            handler.do_GET()

            # Verify _send was called with 200 OK JSON response instead of proxying
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            ctype = args[1] if len(args) > 1 else kwargs.get("ctype")
            code = kwargs.get("code", 200)
            body = args[0]
            body_str = body.decode("utf-8") if isinstance(body, bytes) else body

            self.assertIn('"status": "ok"', body_str)
            self.assertIn('"ready": true', body_str)
            self.assertEqual(code, 200)
            self.assertEqual(ctype, "application/json")


if __name__ == "__main__":
    unittest.main()
