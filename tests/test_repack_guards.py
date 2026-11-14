from __future__ import annotations

from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPACK = ROOT / "scripts" / "repack.py"

COOKIE_MAGIC = b"MEI\014\013\012\013\016"
ENTRY = struct.Struct("!IIIIBc")
REPACK_TIMEOUT = 60


def run_repack(src: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPACK), str(src), str(out)],
        capture_output=True,
        text=True,
        timeout=REPACK_TIMEOUT,
    )


def build_archive_with_toc(toc: bytes) -> bytes:
    prefix = b"MZ" + b"\0" * 254
    tocpos = 0
    toclen = len(toc)
    lenpkg = toclen + 88
    cookie = struct.pack("!8sIIII", COOKIE_MAGIC, lenpkg, tocpos, toclen, 38) + b"\0" * 64
    return prefix + toc + cookie


class RepackOutputGuardTests(unittest.TestCase):
    def test_mismatched_python_is_rejected_before_loading_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'Nighty.exe'
            out = Path(tmp) / 'Nighty_stub.exe'
            name = b'PYZ.pyz\0'
            toc = ENTRY.pack(ENTRY.size + len(name), 0, 0, 0, 0, b'z') + name
            archive = bytearray(build_archive_with_toc(toc))
            version = 314 if sys.version_info[:2] != (3, 14) else 38
            struct.pack_into('!I', archive, len(archive) - 68, version)
            src.write_bytes(archive)
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('repack requires Python', result.stderr)
            self.assertFalse(out.exists())

    def test_version_detection_rejects_missing_or_truncated_cookie(self) -> None:
        for payload in (b'MZ', b'MZ' + COOKIE_MAGIC):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / 'Nighty.exe'
                src.write_bytes(payload)
                result = subprocess.run(
                    [sys.executable, str(REPACK), '--python-version', str(src)],
                    capture_output=True, text=True, timeout=REPACK_TIMEOUT)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('cookie not found or truncated', result.stderr)

    def test_reports_embedded_python_without_loading_bytecode(self) -> None:
        for version, expected in ((38, '3.8'), (314, '3.14')):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / 'Nighty.exe'
                archive = bytearray(build_archive_with_toc(b''))
                struct.pack_into('!I', archive, len(archive) - 68, version)
                src.write_bytes(archive)
                result = subprocess.run([sys.executable, str(REPACK), '--python-version', str(src)],
                                        capture_output=True, text=True, timeout=REPACK_TIMEOUT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_refuses_to_write_over_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Nighty.exe"
            src.write_bytes(b"MZ" + b"\0" * 4094)
            before = src.read_bytes()
            result = run_repack(src, src)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("output path must differ", result.stdout + result.stderr)
            self.assertEqual(src.read_bytes(), before)

    def test_missing_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "absent.exe"
            out = Path(tmp) / "Nighty_stub.exe"
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source binary not found", result.stdout + result.stderr)
            self.assertFalse(out.exists())

    def test_zero_length_toc_entry_terminates_instead_of_looping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Nighty.exe"
            out = Path(tmp) / "Nighty_stub.exe"
            toc = ENTRY.pack(0, 0, 0, 0, 0, b"z") + b"\0" * 32
            src.write_bytes(build_archive_with_toc(toc))
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("corrupt CArchive TOC entry", result.stdout + result.stderr)
            self.assertFalse(out.exists())

    def test_oversized_toc_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Nighty.exe"
            out = Path(tmp) / "Nighty_stub.exe"
            toc = ENTRY.pack(1 << 30, 0, 0, 0, 0, b"z") + b"\0" * 32
            src.write_bytes(build_archive_with_toc(toc))
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("corrupt CArchive TOC entry", result.stdout + result.stderr)
            self.assertFalse(out.exists())

    def test_archive_without_a_pyz_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Nighty.exe"
            out = Path(tmp) / "Nighty_stub.exe"
            name = b"data.bin\0\0\0\0\0\0\0\0"
            toc = ENTRY.pack(ENTRY.size + len(name), 0, 0, 0, 0, b"b") + name
            src.write_bytes(build_archive_with_toc(toc))
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no PYZ entry found", result.stdout + result.stderr)
            self.assertFalse(out.exists())

    def test_missing_cookie_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Nighty.exe"
            out = Path(tmp) / "Nighty_stub.exe"
            src.write_bytes(b"MZ" + b"\0" * 4094)
            result = run_repack(src, out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cookie not found", result.stdout + result.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
