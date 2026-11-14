import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import enforce_config  # noqa: E402


class EnforceConfigTests(unittest.TestCase):
    def test_migrates_active_account_without_overriding_new_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nighty.config'
            path.write_text(json.dumps({'web': True, 'logins': {'old': {'active': True}}}))
            enforce_config.enforce_web(tmp)
            data = json.loads(path.read_text())
            self.assertEqual(data['active_account'], 'old')
            data['active_account'] = 'new'
            path.write_text(json.dumps(data))
            enforce_config.enforce_web(tmp)
            self.assertEqual(json.loads(path.read_text())['active_account'], 'new')

    def test_enforce_web_backup_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = tmp
            cfg_path = os.path.join(appdata, "nighty.config")
            bak_path = os.path.join(appdata, "nighty.config.bak")

            # 1. Normal run with valid config -> updates and creates .bak
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"web": False, "logins": {"user1": {"active": True}}}, f)

            msg = enforce_config.enforce_web(appdata)
            self.assertIn("nighty.config web -> true", msg)
            self.assertTrue(os.path.exists(bak_path))

            with open(bak_path, "r", encoding="utf-8") as f:
                bak_data = json.load(f)
            self.assertTrue(bak_data.get("web"))
            self.assertIn("user1", bak_data.get("logins", {}))

            # 2. Corrupted/empty nighty.config -> auto-restores from .bak
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("")  # 0 bytes

            msg2 = enforce_config.enforce_web(appdata)
            self.assertIn("auto-restored from backup", msg2)
            with open(cfg_path, "r", encoding="utf-8") as f:
                restored_data = json.load(f)
            self.assertTrue(restored_data.get("web"))
            self.assertIn("user1", restored_data.get("logins", {}))

    def test_prefetch_sounds_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = tmp
            sounds_dir = os.path.join(appdata, "data", "sounds")
            os.makedirs(sounds_dir, exist_ok=True)

            # Pre-seed target sound files
            for target in set(enforce_config.SOUND_ALIASES.values()):
                with open(os.path.join(sounds_dir, target), "wb") as f:
                    f.write(b"RIFF" + b"\x00" * 200)

            msg = enforce_config.prefetch_sounds(appdata)
            # Check all aliases were created on disk
            for alias in enforce_config.SOUND_ALIASES.keys():
                alias_path = os.path.join(sounds_dir, alias)
                self.assertTrue(os.path.exists(alias_path), f"Missing alias on disk: {alias}")
                self.assertGreater(os.path.getsize(alias_path), 100)

    def test_sanitize_all_json_encodings(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = tmp
            json_path = os.path.join(appdata, "profile.json")
            # Write raw UTF-8 emoji bytes (>127)
            raw_data = {"name": "test \U0001f525"}
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, ensure_ascii=False)

            self.assertTrue(enforce_config._has_non_ascii(json_path))

            msg = enforce_config.sanitize_all_json_encodings(appdata)
            self.assertIn("healed 1 non-ASCII", msg)
            self.assertFalse(enforce_config._has_non_ascii(json_path))

            with open(json_path, "r", encoding="utf-8") as f:
                sanitized = json.load(f)
            self.assertEqual(sanitized["name"], "test \U0001f525")

    def test_rotate_and_clean_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = tmp
            diag_dir = os.path.join(appdata, "diagnostics")
            os.makedirs(diag_dir, exist_ok=True)

            # Create a 12MB log file
            big_log = os.path.join(diag_dir, "test.log")
            with open(big_log, "wb") as f:
                f.write(b"A" * (12 * 1024 * 1024))

            # Set NIGHTY_HOME to temp dir
            os.environ["NIGHTY_HOME"] = appdata

            msg = enforce_config.rotate_and_clean_logs(appdata)
            self.assertIn("rotated 1 log(s)", msg)
            # File should now be truncated to ~2MB
            self.assertLessEqual(os.path.getsize(big_log), 3 * 1024 * 1024)
            with open(big_log, "rb") as f:
                header = f.read(50)
            self.assertIn(b"[truncated log rotation]", header)


    def test_rotation_never_touches_the_backend_owned_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = os.path.join(tmp, "Nighty Selfbot")
            os.makedirs(appdata, exist_ok=True)
            os.environ["NIGHTY_HOME"] = tmp
            os.environ["NIGHTY_DIAG_DIR"] = os.path.join(tmp, "diagnostics")

            owned = os.path.join(appdata, "nighty.log")
            with open(owned, "wb") as f:
                f.write(b"L" * (12 * 1024 * 1024))

            writer = open(owned, "r+b")
            writer.seek(0, os.SEEK_END)

            enforce_config.rotate_and_clean_logs(appdata)

            writer.write(b"REAL LOG LINE\n")
            writer.flush()
            writer.close()

            with open(owned, "rb") as f:
                data = f.read()
            self.assertNotIn(b"\x00\x00\x00\x00", data)
            self.assertIn(b"REAL LOG LINE", data)


if __name__ == "__main__":
    unittest.main()
