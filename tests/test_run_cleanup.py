from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
BASH = shutil.which("bash")


def function_source(name):
    return re.search(r"(?ms)^" + name + r"\(\) \{\n.*?^\}", SOURCE).group()


class CleanupWiringTests(unittest.TestCase):
    def test_cleanup_has_one_definition(self):
        self.assertEqual(len(re.findall(r"(?m)^\s*cleanup\(\) \{", SOURCE)), 1)


@unittest.skipUnless(BASH, "bash is required to exercise shutdown")
class CleanupBehaviorTests(unittest.TestCase):
    def run_cleanup(self, script):
        setup = f"""
set -u
HERE={shlex.quote(ROOT.as_posix())}
_CLEANED=0
_STACK_STARTED=1
BACKEND_LOOP_PID=''
BRIDGE_LOOP_PID=''
GUARD_PID=''
XVFB_PID=''
log() {{ :; }}
sleep() {{ :; }}
release_instance_guard() {{ printf 'release\\n'; }}
kill_process_with_arg() {{ printf 'stop-arg:%s\\n' "$1"; }}
nighty_stop_wineserver() {{ printf 'wine-stop\\n'; }}
cleanup_pyinstaller_temp() {{ printf 'temp-cleanup\\n'; }}
{function_source("terminate_pid")}
{function_source("cleanup")}
"""
        return subprocess.run([BASH, "-c", setup + script], capture_output=True,
                              text=True, timeout=15)

    @unittest.skipIf(os.name == "nt", "requires POSIX process signals")
    def test_supervisors_are_terminated_and_cleanup_is_idempotent(self):
        result = self.run_cleanup("""
command sleep 30 & BACKEND_LOOP_PID=$!
command sleep 30 & BRIDGE_LOOP_PID=$!
command sleep 30 & GUARD_PID=$!
command sleep 30 & XVFB_PID=$!
cleanup
cleanup
for pid in "$BACKEND_LOOP_PID" "$BRIDGE_LOOP_PID" "$GUARD_PID" "$XVFB_PID"; do
  if wait "$pid"; then exit 1; fi
done
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("wine-stop\n"), 1)
        self.assertEqual(result.stdout.count("release\n"), 1)
        self.assertIn(f"stop-arg:{ROOT.as_posix()}/scripts/bridge.py\n", result.stdout)
        self.assertIn(f"stop-arg:{ROOT.as_posix()}/scripts/webui_guard.py\n", result.stdout)

    def test_failed_start_releases_guard_without_stopping_a_stack(self):
        result = self.run_cleanup("_STACK_STARTED=0\ncleanup\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "release\n")

    def test_exit_cleanup_preserves_the_original_failure_status(self):
        result = self.run_cleanup("trap cleanup EXIT\nexit 7\n")
        self.assertEqual(result.returncode, 7, result.stderr)


if __name__ == "__main__":
    unittest.main()
