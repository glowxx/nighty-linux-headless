from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
INIT_SERVICE_SH = ROOT / "scripts" / "init_service.sh"
RUN_SH = ROOT / "scripts" / "run.sh"
UNINSTALL_SH = ROOT / "scripts" / "uninstall.sh"
BASH = shutil.which("bash")

RUN_USER = "nightyuser"
WORKDIR = "/opt/nighty headless"


@unittest.skipUnless(BASH, "bash is required to exercise the generators")
class ServiceTextTests(unittest.TestCase):
    def emit(self, function: str, *args: str) -> str:
        quoted = " ".join(f'"{a}"' for a in args)
        script = f'. "{INIT_SERVICE_SH.as_posix()}"\n{function} {quoted}\n'
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_systemd_unit_keeps_the_duplicate_exit_contract(self) -> None:
        text = self.emit("nighty_systemd_unit_text", RUN_USER, WORKDIR)
        self.assertIn(f"User={RUN_USER}", text)
        self.assertIn(f"WorkingDirectory={WORKDIR}", text)
        self.assertIn(f"ExecStart=/usr/bin/env bash {WORKDIR}/scripts/run.sh --run", text)
        self.assertIn("RestartPreventExitStatus=23", text)
        self.assertIn("SuccessExitStatus=23", text)
        template = (ROOT / "systemd" / "nighty.service").read_text(encoding="utf-8")
        for setting in ("KillMode=control-group", "MemoryMax=90%"):
            self.assertIn(setting, text)
            self.assertIn(setting, template)

    def test_openrc_script_is_an_openrc_run_script(self) -> None:
        text = self.emit("nighty_openrc_script_text", RUN_USER, WORKDIR)
        self.assertTrue(text.startswith("#!/sbin/openrc-run"), text[:40])
        self.assertIn(f'command_user="{RUN_USER}"', text)
        self.assertIn(f'directory="{WORKDIR}"', text)
        self.assertIn(f'command_args="bash {WORKDIR}/scripts/run.sh --run"', text)
        self.assertIn('supervisor="supervise-daemon"', text)
        self.assertIn("need net", text)

    def test_runit_run_script_stops_supervising_on_the_duplicate_exit(self) -> None:
        text = self.emit("nighty_runit_run_text", RUN_USER, WORKDIR)
        self.assertTrue(text.startswith("#!/bin/sh"), text[:40])
        self.assertIn("exec 2>&1", text)
        self.assertIn(f'chpst -u "{RUN_USER}"', text)
        self.assertIn('if [ "$rc" -eq 23 ]', text)
        self.assertIn("sv down nighty", text)

    def test_generated_scripts_quote_paths_containing_spaces(self) -> None:
        runit = self.emit("nighty_runit_run_text", RUN_USER, WORKDIR)
        self.assertIn(f'cd "{WORKDIR}"', runit)
        self.assertIn(f'"{WORKDIR}/scripts/run.sh"', runit)

    def test_every_generated_script_is_syntactically_valid(self) -> None:
        for function, args in (
            ("nighty_openrc_script_text", (RUN_USER, WORKDIR)),
            ("nighty_runit_run_text", (RUN_USER, WORKDIR)),
            ("nighty_runit_log_run_text", (WORKDIR,)),
        ):
            text = self.emit(function, *args)
            check = subprocess.run([BASH, "-n"], input=text, capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, f"{function}: {check.stderr}")


@unittest.skipUnless(BASH, "bash is required to exercise detection")
class InitDetectionTests(unittest.TestCase):
    def detect(self, forced):
        assignment = f'NIGHTY_INIT_SYSTEM="{forced}"\n' if forced is not None else ""
        script = f'. "{INIT_SERVICE_SH.as_posix()}"\n{assignment}nighty_detect_init_system\n'
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_explicit_override_wins(self) -> None:
        for forced in ("systemd", "openrc", "runit"):
            self.assertEqual(self.detect(forced), forced)

    def test_detection_returns_a_known_value(self) -> None:
        self.assertIn(self.detect(None), {"systemd", "openrc", "runit", "unknown"})


class AutostartWiringTests(unittest.TestCase):
    def test_run_sh_dispatches_on_the_detected_init_system(self) -> None:
        text = RUN_SH.read_text(encoding="utf-8")
        for handler in ("setup_autostart_systemd", "setup_autostart_openrc",
                        "setup_autostart_runit", "setup_autostart_manual"):
            self.assertIn(handler, text)
        self.assertIn("nighty_detect_init_system", text)

    def test_run_sh_no_longer_refuses_without_systemd(self) -> None:
        text = RUN_SH.read_text(encoding="utf-8")
        self.assertNotIn("systemd not found on this host", text)

    def test_uninstaller_removes_all_three_service_types(self) -> None:
        text = UNINSTALL_SH.read_text(encoding="utf-8")
        self.assertIn("/etc/systemd/system/nighty.service", text)
        self.assertIn("/etc/init.d/nighty", text)
        self.assertIn("/etc/sv/nighty", text)


if __name__ == "__main__":
    unittest.main()
