from __future__ import annotations

import errno
import io
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nighty_repack", ROOT / "scripts" / "repack.py")
assert SPEC and SPEC.loader
REPACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPACK)

PAYLOAD = b"MZ" + b"\x90" * 4094
OLD_CONTENT = b"stale stub content"


class HeadlessScreenTests(unittest.TestCase):
    def test_async_rpc_is_queued_on_the_same_dispatcher(self):
        scope = {}
        exec(REPACK.STUB_INIT, scope)
        api = types.SimpleNamespace(run=mock.Mock(return_value='done'))
        scope['_JS_API'].append(api)
        with mock.patch('http.server.ThreadingHTTPServer') as server, \
                mock.patch.object(scope['threading'], 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs['target']()
            scope['_start_ctl']()
        handler_type = server.call_args.args[1]
        handler = handler_type.__new__(handler_type)
        payload = json.dumps({'api': 0, 'method': 'run', 'args': [42], 'async': True}).encode()
        handler.headers = {'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler._send = mock.Mock()
        handler.do_POST()
        api.run.assert_not_called()
        fn, args, kwargs, box, done = scope['_taskq'].get_nowait()
        self.assertEqual(fn(*args, **kwargs), 'done')
        api.run.assert_called_once_with(42)
        self.assertTrue(handler._send.call_args.args[0]['started'])

    def test_creates_and_retains_application_when_gui_stub_has_not_created_one(self):
        primary = object()
        class App:
            def __init__(self, argv):
                self.argv = argv
        gui = type('Gui', (), {
            'screenAt': staticmethod(lambda point: None),
            'primaryScreen': staticmethod(lambda: primary),
            'instance': staticmethod(lambda: None),
        })
        scope = {}
        with mock.patch.dict(sys.modules, {
            'PyQt6': types.ModuleType('PyQt6'),
            'PyQt6.QtGui': types.SimpleNamespace(QGuiApplication=gui),
            'PyQt6.QtWidgets': types.SimpleNamespace(QApplication=App),
        }):
            exec(REPACK.QT_SCREEN_HOOK, scope)
        self.assertIs(gui.primaryScreen(), primary)
        self.assertIsInstance(scope['_nighty_qt_app'], App)
        self.assertEqual(scope['_nighty_qt_app'].argv, ['-platform', 'offscreen'])

    def test_screen_at_uses_primary_only_when_point_has_no_screen(self):
        for selected in (None, object()):
            with self.subTest(selected=selected):
                primary = object()
                gui = type('Gui', (), {
                    'screenAt': staticmethod(lambda point: selected),
                    'primaryScreen': staticmethod(lambda: primary),
                    'instance': staticmethod(lambda: object()),
                })
                with mock.patch.dict(sys.modules, {
                    'PyQt6': types.ModuleType('PyQt6'),
                    'PyQt6.QtGui': types.SimpleNamespace(QGuiApplication=gui),
                    'PyQt6.QtWidgets': types.SimpleNamespace(QApplication=gui),
                }):
                    exec(REPACK.QT_SCREEN_HOOK, {})
                self.assertIs(gui.screenAt(object()), selected if selected is not None else primary)


def raising_replace(err):
    def _replace(src, dst):
        raise OSError(err, os.strerror(err))
    return _replace


class WriteOutputTests(unittest.TestCase):
    def test_normal_write_is_atomic_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            how = REPACK.write_output(str(target), PAYLOAD)
            self.assertEqual(how, "atomic")
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertFalse((Path(tmp) / "Nighty_stub.exe.tmp").exists())

    def test_busy_target_falls_back_and_keeps_the_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            target.write_bytes(OLD_CONTENT)
            inode_before = target.stat().st_ino
            with mock.patch.object(REPACK.os, "replace", raising_replace(errno.EBUSY)):
                how = REPACK.write_output(str(target), PAYLOAD)
            self.assertIn("in-place", how)
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertEqual(target.stat().st_ino, inode_before)
            self.assertFalse((Path(tmp) / "Nighty_stub.exe.tmp").exists())

    def test_cross_device_target_also_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            target.write_bytes(OLD_CONTENT)
            with mock.patch.object(REPACK.os, "replace", raising_replace(errno.EXDEV)):
                how = REPACK.write_output(str(target), PAYLOAD)
            self.assertIn("in-place", how)
            self.assertEqual(target.read_bytes(), PAYLOAD)

    def test_fallback_shrinks_a_larger_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            target.write_bytes(b"X" * (len(PAYLOAD) * 3))
            with mock.patch.object(REPACK.os, "replace", raising_replace(errno.EBUSY)):
                REPACK.write_output(str(target), PAYLOAD)
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertEqual(target.stat().st_size, len(PAYLOAD))

    def test_disk_full_is_raised_not_silently_written_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            target.write_bytes(OLD_CONTENT)
            with mock.patch.object(REPACK.os, "replace", raising_replace(errno.ENOSPC)):
                with self.assertRaises(OSError) as caught:
                    REPACK.write_output(str(target), PAYLOAD)
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertEqual(target.read_bytes(), OLD_CONTENT)

    def test_read_only_filesystem_is_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Nighty_stub.exe"
            target.write_bytes(OLD_CONTENT)
            with mock.patch.object(REPACK.os, "replace", raising_replace(errno.EROFS)):
                with self.assertRaises(OSError):
                    REPACK.write_output(str(target), PAYLOAD)
            self.assertEqual(target.read_bytes(), OLD_CONTENT)


if __name__ == "__main__":
    unittest.main()
