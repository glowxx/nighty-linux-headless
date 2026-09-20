#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless — orchestrator
#
#  This single script brings up the WHOLE stack and keeps it alive:
#    • a virtual display (Xvfb)
#    • config enforcement (notifications off, Web UI on) — pre-launch + continuous
#    • the LAN Web UI bridge (port 8088)
#    • the Nighty backend (Wine, headless) with auto-relaunch
#
#  Run it with no arguments and it asks whether to start once or to install
#  itself as a service (autostart on every boot) — and does the setup
#  for you. With --run it just starts the stack (this is what the service uses).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/scripts/wine_command.sh"
. "$HERE/scripts/init_service.sh"
. "$HERE/scripts/backoff.sh"

# ── load .env ────────────────────────────────────────────────────────────────
. "$HERE/scripts/env_file.sh"
nighty_load_env_file "$HERE/.env"

# ── defaults ─────────────────────────────────────────────────────────────────
: "${NIGHTY_HOME:=$HOME/.local/share/nighty}"
: "${WINEPREFIX:=$NIGHTY_HOME/prefix}"
: "${NIGHTY_STUB:=$HERE/Nighty_stub.exe}"
: "${NIGHTY_EXE:=$HERE/Nighty.exe}"
: "${WINE_BIN:=wine64}"
: "${DISPLAY_NUM:=99}"
: "${STUB_PORT:=8765}"
: "${BRIDGE_PORT:=8088}"
: "${WEBUI_PORT:=8090}"
: "${ENFORCE_WEBUI:=1}"
: "${NIGHTY_SINGLE_INSTANCE:=enforce}"
: "${NIGHTY_INSTANCE_LOCK:=$NIGHTY_HOME/run.lock}"
: "${XVFB_TIMEOUT:=10}"
: "${BLOCK_LRCLIB:=1}"
: "${NIGHTY_BOX64_PROFILE:=safe}"
# Max seconds to wait for Nighty's stub control server to answer after
# launching Wine before assuming the boot hung and retrying. Some Wine builds
# can stall during first-prefix init well before Nighty's own code ever runs.
: "${BOOT_TIMEOUT:=300}"
# Once the stub is alive, the native panel still has to authenticate, sync and
# open WEBUI_PORT. A transient Discord/Cloudflare failure can otherwise leave the
# loading screen alive forever even though the first-stage watchdog has exited.
: "${WEBUI_BOOT_TIMEOUT:=180}"
: "${BACKEND_FAST_FAIL_SECONDS:=60}"
: "${BACKEND_MAX_BACKOFF:=300}"
: "${BACKEND_TRIAGE_AFTER:=3}"
: "${CLEAN_STALE_MEI:=1}"
: "${NIGHTY_DIAG_DIR:=$HERE/diagnostics}"
mkdir -p "$NIGHTY_HOME" "$NIGHTY_DIAG_DIR"
export NIGHTY_DIAG_DIR

export WINEPREFIX
export WINEARCH=win64
export WINEDEBUG=-all
export DISPLAY=":$DISPLAY_NUM"
export NIGHTY_STUB_PORT="$STUB_PORT"
export NIGHTY_STUB_LOG="${NIGHTY_STUB_LOG:-Z:$NIGHTY_DIAG_DIR/stub_webview.log}"
# Headless DLL overrides. Nighty's GUI is stubbed out and the backend is pure
# Python, so we disable the Windows components that only crash or hang on a
# headless box: .NET (mscoree), Internet Explorer (mshtml — its first-run calls
# the unimplemented advpack.RegInstall and aborts the process), and desktop
# integration (winemenubuilder). Override via WINEDLLOVERRIDES in .env if needed.
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree=d;mshtml=d;winemenubuilder.exe=d}"
# Force UTF-8 for the backend's console/log streams only. Nighty's logger prints
# emoji (🔌 / ❌ / 🌐) in some connect/status messages; under Wine the default
# stdout codec is cp1252, so those lines raise UnicodeEncodeError
# ("--- Logging error ---"), are lost, and flood backend.log with tracebacks (I/O
# that competes with the emulated bot). PYTHONIOENCODING changes ONLY the stdio
# text encoding — NOT the default open() encoding — so Nighty's cp1252 config file
# handling is left exactly as-is.
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# Architecture-aware tuning: Box64 knobs only matter when emulating x86-64.
case "$(uname -m)" in
  x86_64|amd64) ARCH_DESC="x86-64 (native Wine)" ;;
  *) ARCH_DESC="$(uname -m) (Wine over Box64)"
     export BOX64_NOBANNER="${BOX64_NOBANNER:-1}" BOX64_LOG="${BOX64_LOG:-0}"
     # Existing installations stay conservative by default. The balanced profile
     # removes most global barriers while config/box64-nighty.rc restores strict
     # settings only for the crash-prone bundled Go TLS client.
     case "$NIGHTY_BOX64_PROFILE" in
       safe)        _BB=0; _SM=3; _SF=2; _CR=0 ;;
       balanced)    _BB=1; _SM=1; _SF=1; _CR=0 ;;
       performance) _BB=1; _SM=1; _SF=0; _CR=1 ;;
       *) echo "[run] FATAL: unknown NIGHTY_BOX64_PROFILE '$NIGHTY_BOX64_PROFILE' (use safe, balanced, or performance)" >&2; exit 2 ;;
     esac
     : "${BOX64_DYNAREC_BIGBLOCK:=$_BB}"; : "${BOX64_DYNAREC_STRONGMEM:=$_SM}"
     : "${BOX64_DYNAREC_SAFEFLAGS:=$_SF}"; : "${BOX64_DYNAREC_CALLRET:=$_CR}"
     : "${BOX64_DYNAREC_FASTROUND:=1}"; : "${BOX64_DYNAREC_FASTNAN:=1}"
     : "${BOX64_DYNAREC_WAIT:=1}"
     if [ -f "$HERE/config/box64-nighty.rc" ]; then
       export BOX64_RCFILE="${BOX64_RCFILE:-$HERE/config/box64-nighty.rc}"
     fi
     export BOX64_DYNAREC_BIGBLOCK BOX64_DYNAREC_STRONGMEM \
            BOX64_DYNAREC_SAFEFLAGS BOX64_DYNAREC_CALLRET \
            BOX64_DYNAREC_FASTROUND BOX64_DYNAREC_FASTNAN \
            BOX64_DYNAREC_WAIT BOX64_RCFILE ;;
esac
ulimit -s 8192 2>/dev/null || true

log() { echo "[run] $(date '+%H:%M:%S') $*"; }

# Single-instance guard.  The bridge/process probes also recognise an older
# release which is already running but does not hold the new advisory lock.
_INSTANCE_OWNER=0
_INSTANCE_LOCK_KIND=""
_INSTANCE_LOCK_DIR="${NIGHTY_INSTANCE_LOCK}.d"
_INSTANCE_META="${NIGHTY_INSTANCE_LOCK}.meta"

panel_url() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="<this-host-ip>"
  printf 'http://%s:%s/' "$ip" "$BRIDGE_PORT"
}

probe_nighty_bridge() {
  local body="" url="http://127.0.0.1:${BRIDGE_PORT}/ready"
  if command -v curl >/dev/null 2>&1; then
    body="$(curl -fsS --max-time 2 "$url" 2>/dev/null || true)"
  elif command -v wget >/dev/null 2>&1; then
    body="$(wget -qO- -T 2 "$url" 2>/dev/null || true)"
  elif command -v python3 >/dev/null 2>&1; then
    body="$(python3 - "$url" <<'PY' 2>/dev/null || true
import sys, urllib.request
print(urllib.request.urlopen(sys.argv[1], timeout=2).read().decode("utf-8", "replace"))
PY
)"
  fi
  case "$body" in *'"ready"'*) return 0 ;; *) return 1 ;; esac
}

bridge_port_in_use() {
  (exec 8<>"/dev/tcp/127.0.0.1/${BRIDGE_PORT}") >/dev/null 2>&1
}

process_is_runner() {
  local pid="$1" arg=""
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    case "$arg" in
      scripts/run.sh|*/scripts/run.sh) return 0 ;;
    esac
  done <"/proc/$pid/cmdline" 2>/dev/null
  return 1
}

find_existing_runner() {
  local proc pid
  for proc in /proc/[0-9]*; do
    [ -d "$proc" ] || continue
    pid="${proc##*/}"
    [ "$pid" = "${BASHPID:-$$}" ] && continue
    if is_ancestor_pid "$pid"; then continue; fi
    if process_is_runner "$pid"; then
      printf '%s' "$pid"
      return 0
    fi
  done
  return 1
}

is_ancestor_pid() {
  local wanted="$1" current="${BASHPID:-$$}" parent=""
  while [ "$current" -gt 1 ] 2>/dev/null; do
    parent="$(awk '/^PPid:/ {print $2}' "/proc/$current/status" 2>/dev/null || true)"
    [ -n "$parent" ] || break
    [ "$parent" = "$wanted" ] && return 0
    current="$parent"
  done
  return 1
}

write_instance_meta() {
  local tmp="${_INSTANCE_META}.$$"
  ( umask 077
    {
      printf 'pid=%s\n' "$$"
      printf 'started=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      printf 'repo=%s\n' "$HERE"
      printf 'panel=%s\n' "$(panel_url)"
    } >"$tmp"
  ) && mv -f "$tmp" "$_INSTANCE_META"
}

release_instance_guard() {
  [ "$_INSTANCE_OWNER" = 1 ] || return 0
  if [ -f "$_INSTANCE_META" ] && grep -qx "pid=$$" "$_INSTANCE_META" 2>/dev/null; then
    rm -f "$_INSTANCE_META"
  fi
  if [ "$_INSTANCE_LOCK_KIND" = mkdir ] && [ -d "$_INSTANCE_LOCK_DIR" ] && [ ! -L "$_INSTANCE_LOCK_DIR" ]; then
    rm -f "$_INSTANCE_LOCK_DIR/pid" 2>/dev/null || true
    rmdir "$_INSTANCE_LOCK_DIR" 2>/dev/null || true
  fi
  _INSTANCE_OWNER=0
}

acquire_instance_lock() {
  [ "$NIGHTY_SINGLE_INSTANCE" = off ] && return 0
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$NIGHTY_INSTANCE_LOCK" || { log "cannot open instance lock: $NIGHTY_INSTANCE_LOCK"; return 24; }
    flock -n 9 || return 23
    _INSTANCE_LOCK_KIND=flock
    _INSTANCE_OWNER=1
    write_instance_meta
    return 0
  fi

  # Portable fallback for minimal distributions without util-linux/flock.
  if ! mkdir "$_INSTANCE_LOCK_DIR" 2>/dev/null; then
    local owner=""
    [ -f "$_INSTANCE_LOCK_DIR/pid" ] && owner="$(cat "$_INSTANCE_LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
      process_is_runner "$owner" && return 23
    fi
    if [ -d "$_INSTANCE_LOCK_DIR" ] && [ ! -L "$_INSTANCE_LOCK_DIR" ]; then
      rm -f "$_INSTANCE_LOCK_DIR/pid" 2>/dev/null || true
      rmdir "$_INSTANCE_LOCK_DIR" 2>/dev/null || true
    fi
    mkdir "$_INSTANCE_LOCK_DIR" 2>/dev/null || return 23
  fi
  ( umask 077; printf '%s\n' "$$" >"$_INSTANCE_LOCK_DIR/pid" ) || return 24
  _INSTANCE_LOCK_KIND=mkdir
  _INSTANCE_OWNER=1
  write_instance_meta
  return 0
}

report_existing_instance() {
  local pid="${1:-}"
  echo
  log "Nighty is already running${pid:+ (runner PID $pid)} -- a duplicate was NOT started."
  echo "    panel:    $(panel_url)"
  echo "    status:   systemctl status nighty --no-pager"
  echo "    live log: journalctl -u nighty -f"
}

guard_existing_instance() {
  local rc pid=""
  [ "$NIGHTY_SINGLE_INSTANCE" = off ] && return 0
  acquire_instance_lock; rc=$?
  if [ "$rc" -eq 23 ]; then
    pid="$(find_existing_runner || true)"
    report_existing_instance "$pid"
    return 23
  fi
  [ "$rc" -eq 0 ] || return "$rc"

  if probe_nighty_bridge; then
    pid="$(find_existing_runner || true)"
    report_existing_instance "$pid"
    return 23
  fi
  pid="$(find_existing_runner || true)"
  if [ -n "$pid" ]; then
    report_existing_instance "$pid"
    log "The existing process is still starting or its panel is unhealthy; inspect its logs instead of starting another copy."
    return 23
  fi
  if bridge_port_in_use; then
    log "FATAL: port $BRIDGE_PORT is occupied by something that is not a Nighty bridge."
    log "Refusing to kill an unrelated process or start a conflicting instance."
    return 24
  fi
  return 0
}

# ── autostart (systemd / OpenRC / runit) ─────────────────────────────────────
setup_autostart_systemd() {
  local SUDO="$1" run_user="$2" unit=/etc/systemd/system/nighty.service
  if systemctl is-active --quiet nighty.service 2>/dev/null; then
    report_existing_instance ""
    log "Autostart is already active; no second service was created."
    return 0
  fi
  log "installing systemd service → $unit"
  nighty_systemd_unit_text "$run_user" "$HERE" | $SUDO tee "$unit" >/dev/null || return 1
  $SUDO systemctl daemon-reload || { log "daemon-reload failed"; return 1; }
  $SUDO systemctl enable --now nighty.service || { log "enabling service failed"; return 1; }
  echo
  log "Autostart is ON — Nighty now starts on every boot and is running already."
  echo "    status:   systemctl status nighty"
  echo "    live log: journalctl -u nighty -f"
  echo "    panel:    http://<this-host-ip>:$BRIDGE_PORT/"
  echo "    turn off: sudo systemctl disable --now nighty"
}

setup_autostart_openrc() {
  local SUDO="$1" run_user="$2" script=/etc/init.d/nighty
  if ! command -v supervise-daemon >/dev/null 2>&1; then
    log "OpenRC is present but supervise-daemon is missing; Nighty would not be restarted after a crash."
    log "Install a newer OpenRC (>= 0.21) or start it yourself with:  bash scripts/run.sh once"
    return 1
  fi
  log "installing OpenRC service → $script"
  nighty_openrc_script_text "$run_user" "$HERE" | $SUDO tee "$script" >/dev/null || return 1
  $SUDO chmod +x "$script" || return 1
  $SUDO rc-update add nighty default || { log "rc-update add failed"; return 1; }
  $SUDO rc-service nighty start || { log "starting the service failed"; return 1; }
  echo
  log "Autostart is ON — Nighty now starts on every boot and is running already."
  echo "    status:   rc-service nighty status"
  echo "    live log: tail -f $NIGHTY_DIAG_DIR/service.log"
  echo "    panel:    http://<this-host-ip>:$BRIDGE_PORT/"
  echo "    turn off: sudo rc-update del nighty default && sudo rc-service nighty stop"
}

setup_autostart_runit() {
  local SUDO="$1" run_user="$2" svdir=/etc/sv/nighty link_dir="" candidate=""
  for candidate in /var/service /etc/service /etc/runit/runsvdir/default; do
    [ -d "$candidate" ] && { link_dir="$candidate"; break; }
  done
  if [ -z "$link_dir" ]; then
    log "runit is present but no service directory was found (/var/service, /etc/service, /etc/runit/runsvdir/default)."
    return 1
  fi
  log "installing runit service → $svdir"
  $SUDO mkdir -p "$svdir/log" || return 1
  nighty_runit_run_text "$run_user" "$HERE" | $SUDO tee "$svdir/run" >/dev/null || return 1
  nighty_runit_log_run_text "$HERE" | $SUDO tee "$svdir/log/run" >/dev/null || return 1
  $SUDO chmod +x "$svdir/run" "$svdir/log/run" || return 1
  mkdir -p "$NIGHTY_DIAG_DIR/svlog" 2>/dev/null || true
  $SUDO ln -sfn "$svdir" "$link_dir/nighty" || { log "linking the service failed"; return 1; }
  echo
  log "Autostart is ON — runsvdir starts Nighty on every boot (it can take a few seconds to appear)."
  echo "    status:   sv status nighty"
  echo "    live log: tail -f $NIGHTY_DIAG_DIR/svlog/current"
  echo "    panel:    http://<this-host-ip>:$BRIDGE_PORT/"
  echo "    turn off: sudo rm $link_dir/nighty"
}

setup_autostart_manual() {
  log "No supported init system was detected (looked for systemd, OpenRC and runit)."
  log "Force one with NIGHTY_INIT_SYSTEM=systemd|openrc|runit, or start Nighty yourself:"
  echo
  echo "    bash $HERE/scripts/run.sh once"
  echo
  log "To survive a reboot without an init system, add that command to your own supervisor,"
  log "a container restart policy, or an @reboot crontab entry."
  return 1
}

setup_autostart() {
  local init_system SUDO="" run_user
  init_system="$(nighty_detect_init_system)"
  run_user="$(id -un)"
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"

  if probe_nighty_bridge || [ -n "$(find_existing_runner || true)" ]; then
    report_existing_instance "$(find_existing_runner || true)"
    log "Stop the manual instance with Ctrl+C, then run autostart again."
    return 23
  fi

  log "init system: $init_system"
  case "$init_system" in
    systemd) setup_autostart_systemd "$SUDO" "$run_user" ;;
    openrc)  setup_autostart_openrc  "$SUDO" "$run_user" ;;
    runit)   setup_autostart_runit   "$SUDO" "$run_user" ;;
    *)       setup_autostart_manual ;;
  esac
}

# ── the stack ────────────────────────────────────────────────────────────────
_CLEANED=0
_STACK_STARTED=0
XVFB_PID=""
GUARD_PID=""
BRIDGE_LOOP_PID=""
BACKEND_LOOP_PID=""

cleanup_pyinstaller_temp() {
  [ "$CLEAN_STALE_MEI" = 1 ] || return 0
  # PyInstaller normally removes _MEI* on a clean Windows exit. Watchdog
  # SIGKILLs and Wine shutdowns cannot run that cleanup, so remove leftovers
  # externally while no Nighty backend is using this dedicated prefix.
  WINEPREFIX="$WINEPREFIX" bash "$HERE/scripts/cleanup_mei.sh" ||
    log "WARNING: stale PyInstaller temp cleanup did not complete."
}

terminate_pid() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
}

kill_process_with_arg() {
  local wanted="$1" proc pid arg matched
  for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    pid="${proc##*/}"
    [ "$pid" = "${BASHPID:-$$}" ] && continue
    matched=0
    while IFS= read -r -d '' arg; do
      [ "$arg" = "$wanted" ] && matched=1
    done <"$proc/cmdline" 2>/dev/null
    [ "$matched" -eq 1 ] && kill -TERM "$pid" 2>/dev/null || true
  done
}

cleanup() {
  [ "$_CLEANED" = 1 ] && return 0; _CLEANED=1
  if [ "$_STACK_STARTED" != 1 ]; then
    release_instance_guard
    return 0
  fi
  log "shutting down…"
  # Stop supervisors before their children, otherwise their persistence loops
  # immediately respawn the processes and survive as orphans.
  terminate_pid "$BACKEND_LOOP_PID"
  terminate_pid "$BRIDGE_LOOP_PID"
  terminate_pid "$GUARD_PID"
  sleep 1
  # Match real argv entries, not arbitrary command-line text from an SSH script.
  kill_process_with_arg "$HERE/scripts/bridge.py"
  kill_process_with_arg "$HERE/scripts/webui_guard.py"
  nighty_stop_wineserver
  cleanup_pyinstaller_temp
  terminate_pid "$XVFB_PID"
  release_instance_guard
}

run_stack() {
  trap cleanup EXIT
  trap 'exit 0' INT TERM

  local guard_rc
  guard_existing_instance; guard_rc=$?
  if [ "$guard_rc" -eq 23 ]; then
    case "${1:-}" in --run|--service) return 23 ;; *) return 0 ;; esac
  fi
  [ "$guard_rc" -eq 0 ] || return "$guard_rc"

  if [ ! -s "$NIGHTY_STUB" ]; then
    if [ -f "$NIGHTY_EXE" ]; then
      log "$NIGHTY_STUB not found or empty — auto-generating from $NIGHTY_EXE..."
      bash "$HERE/scripts/install.sh" || {
        log "FATAL: Failed to auto-generate $NIGHTY_STUB from $NIGHTY_EXE."
        return 1
      }
    else
      echo "[run] FATAL: $NIGHTY_STUB not found and $NIGHTY_EXE is missing. Provide Nighty.exe or run scripts/install.sh." >&2
      return 1
    fi
  fi
  if ! python3 "$HERE/scripts/preflight.py" wine "$WINE_BIN"; then
    for wine_candidate in "$NIGHTY_HOME/wine/bin/wine64" "$NIGHTY_HOME/wine/bin/wine"; do
      if [ -x "$wine_candidate" ]; then
        WINE_BIN="$wine_candidate"
        export WINE_BIN
        log "Recovered the bundled Wine launcher: $WINE_BIN"
        break
      fi
    done
    python3 "$HERE/scripts/preflight.py" wine "$WINE_BIN" || {
      log "FATAL: Wine launcher is unavailable. Re-run: bash scripts/install.sh"
      return 1
    }
  fi
  nighty_configure_wine_command "$WINE_BIN" "$(uname -m)" || return 1
  # The instance guard is held and Wine has not been launched yet, so all
  # matching PyInstaller extraction directories in this prefix are stale.
  cleanup_pyinstaller_temp
  case "$(uname -m)" in
    x86_64|amd64) : ;;
    *) python3 "$HERE/scripts/preflight.py" libs --quiet || {
         python3 "$HERE/scripts/preflight.py" libs || true
         log "FATAL: native Wine/X11 libraries are missing. Re-run: bash scripts/install.sh"
         return 1
       } ;;
  esac

  log "host architecture: $ARCH_DESC"
  case "$(uname -m)" in
    x86_64|amd64) : ;;
    *)
      log "Box64 profile: $NIGHTY_BOX64_PROFILE (BIGBLOCK=$BOX64_DYNAREC_BIGBLOCK STRONGMEM=$BOX64_DYNAREC_STRONGMEM SAFEFLAGS=$BOX64_DYNAREC_SAFEFLAGS CALLRET=$BOX64_DYNAREC_CALLRET)"
      if [ "$BLOCK_LRCLIB" = 1 ] && ! grep -Eq '^[[:space:]]*192\.0\.2\.1[[:space:]]+(api\.)?lrclib\.net([[:space:]]|$)' /etc/hosts 2>/dev/null; then
        log "WARNING: lrclib.net is not blocked; its synchronous lyrics fetch can freeze Discord commands for 10-60s."
        log "Re-run bash scripts/install.sh or add the documented /etc/hosts entries."
      elif [ "$BLOCK_LRCLIB" = 1 ] && command -v ip >/dev/null 2>&1 \
           && ! ip route show 192.0.2.1 2>/dev/null | grep -q unreachable; then
        log "WARNING: the RP-fetch blackhole route is missing; blackholed requests will leave the host and hang until timeout."
        log "Restore it with:  sudo ip route replace unreachable 192.0.2.1"
      fi
      ;;
  esac
  DIAG_DIR="${NIGHTY_DIAG_DIR:-$HERE/diagnostics}"
  mkdir -p "$DIAG_DIR" 2>/dev/null || true

  cleanup() {
    log "Shutdown signal received. Stopping stack cleanly..."
    [ -n "${BACKEND_LOOP_PID:-}" ] && kill -15 "$BACKEND_LOOP_PID" 2>/dev/null || true
    [ -n "${GUARD_PID:-}" ] && kill -15 "$GUARD_PID" 2>/dev/null || true
    [ -n "${BRIDGE_LOOP_PID:-}" ] && kill -15 "$BRIDGE_LOOP_PID" 2>/dev/null || true
    pkill -15 -f "bridge.py" 2>/dev/null || true
    pkill -15 -f "webui_guard.py" 2>/dev/null || true
    if [ -n "${WINE_BIN:-}" ] && [ -x "$WINE_BIN" ]; then
      "$WINE_BIN" wineserver -k 2>/dev/null || true
    elif command -v wineserver >/dev/null 2>&1; then
      wineserver -k 2>/dev/null || true
    fi
    [ -n "${XVFB_PID:-}" ] && kill -15 "$XVFB_PID" 2>/dev/null || true
    rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" "/tmp/.X${DISPLAY_NUM}-lock" 2>/dev/null || true
    release_instance_guard
    log "Shutdown complete."
    exit 0
  }
  trap cleanup SIGTERM SIGINT SIGHUP

  # Pre-launch config enforcement (notifications off, Web UI creds + web:true).
  python3 "$HERE/scripts/enforce_config.py" || true

  # Virtual display. Xvfb refuses to auto-create /tmp/.X11-unix unless running
  # as root, so on a non-root install (or any host where /tmp is freshly
  # mounted/cleared) it silently fails to bind its socket. create the socket dir
  # ourselves rather than relying on Xvfb to do it.
  mkdir -p /tmp/.X11-unix
  chmod 1777 /tmp/.X11-unix 2>/dev/null || true
  if pgrep -f "[X]vfb :$DISPLAY_NUM" >/dev/null 2>&1; then
    pkill -9 -f "[X]vfb :$DISPLAY_NUM" 2>/dev/null || true
    sleep 1
  fi
  rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" "/tmp/.X${DISPLAY_NUM}-lock" 2>/dev/null || true
  _STACK_STARTED=1
  Xvfb ":$DISPLAY_NUM" -screen 0 1366x768x24 -nolisten tcp >"$DIAG_DIR/xvfb.log" 2>&1 &
  XVFB_PID=$!
  xvfb_waited=0
  while [ "$xvfb_waited" -lt "$XVFB_TIMEOUT" ]; do
    if kill -0 "$XVFB_PID" 2>/dev/null && [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
      break
    fi
    kill -0 "$XVFB_PID" 2>/dev/null || break
    sleep 1
    xvfb_waited=$((xvfb_waited + 1))
  done
  if ! kill -0 "$XVFB_PID" 2>/dev/null || [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    log "FATAL: Xvfb failed to become ready on :$DISPLAY_NUM within ${XVFB_TIMEOUT}s."
    tail -n 20 "$DIAG_DIR/xvfb.log" 2>/dev/null || true
    return 1
  fi

  # Continuous Web UI hard-enforcement.
  if [ "$ENFORCE_WEBUI" = "1" ]; then
    python3 "$HERE/scripts/webui_guard.py" >>"$NIGHTY_DIAG_DIR/guard.log" 2>&1 &
    GUARD_PID=$!
  fi

  # Web UI bridge — kept alive in its own loop.
  ( while true; do
      python3 "$HERE/scripts/bridge.py" >>"$NIGHTY_DIAG_DIR/bridge.log" 2>&1
      echo "[bridge] $(date '+%H:%M:%S') exited — restarting in 3s" >>"$NIGHTY_DIAG_DIR/bridge.log"
      sleep 3
    done ) &
  BRIDGE_LOOP_PID=$!
  log "Web UI bridge up — open  http://<this-host-ip>:$BRIDGE_PORT/"

  # Backend — relaunch forever (covers a UI-triggered restart/close). A
  # watchdog kills and retries if the stub control server never answers
  # within BOOT_TIMEOUT — some Wine builds stall during first-prefix init
  # with no error, well before Nighty's own code would ever hang or crash.
  ( fast_failures=0
    while true; do
      if [ ! -s "$NIGHTY_STUB" ]; then
        if [ -f "$NIGHTY_EXE" ]; then
          log "$NIGHTY_STUB was removed or emptied — regenerating from $NIGHTY_EXE..."
          bash "$HERE/scripts/install.sh" || {
            log "ERROR: Failed to regenerate $NIGHTY_STUB — retrying in 5s..."
            sleep 5
            continue
          }
        fi
      fi
      python3 "$HERE/scripts/rotate_logs.py" >/dev/null 2>&1 || true
      python3 "$HERE/scripts/enforce_config.py" >/dev/null 2>&1 || true
      python3 "$HERE/scripts/preflight.py" report --diag-dir "$NIGHTY_DIAG_DIR" --quiet >/dev/null 2>&1 || true
      mkdir -p "$HERE/dist/ws_extensions" "$NIGHTY_HOME/dist/ws_extensions" 2>/dev/null || true
      log "launching backend ($NIGHTY_STUB)…"
      backend_started=$(date +%s)
      "${NIGHTY_WINE_COMMAND[@]}" "$NIGHTY_STUB" >>"$NIGHTY_DIAG_DIR/backend.log" 2>&1 &
      BACKEND_PID=$!

      (
        waited=0
        stub_ready=0
        while [ "$waited" -lt "$BOOT_TIMEOUT" ]; do
          kill -0 "$BACKEND_PID" 2>/dev/null || exit 0
          # Bash builtin TCP probe (no curl/wget dependency): the stub is up
          # once something accepts a connection on STUB_PORT.
          if (exec 3<>"/dev/tcp/127.0.0.1/${STUB_PORT}") 2>/dev/null; then
            exec 3<&- 3>&-
            stub_ready=1
            break
          fi
          sleep 5
          waited=$((waited + 5))
        done
        if [ "$stub_ready" -ne 1 ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
          log "backend boot timed out after ${BOOT_TIMEOUT}s (stub never answered on :${STUB_PORT}). Dumping backend log tail..."
          echo "[run] === BACKEND LOG TAIL (LAST 25 LINES) ===" >&2
          tail -n 25 "$NIGHTY_DIAG_DIR/backend.log" 2>/dev/null >&2 || true
          echo "[run] ==========================================" >&2
          python3 "$HERE/scripts/preflight.py" report --diag-dir "$NIGHTY_DIAG_DIR" --quiet >/dev/null 2>&1 || true
          log "killing unresponsive backend and retrying."
          kill -9 "$BACKEND_PID" 2>/dev/null || true
          exit 0
        fi

        web_waited=0
        while [ "$web_waited" -lt "$WEBUI_BOOT_TIMEOUT" ]; do
          kill -0 "$BACKEND_PID" 2>/dev/null || exit 0
          if (exec 4<>"/dev/tcp/127.0.0.1/${WEBUI_PORT}") 2>/dev/null; then
            exec 4<&- 4>&-
            exit 0
          fi
          sleep 5
          web_waited=$((web_waited + 5))
        done
        if kill -0 "$BACKEND_PID" 2>/dev/null; then
          log "backend panel timed out after ${WEBUI_BOOT_TIMEOUT}s (stub is up but Web UI never answered on :${WEBUI_PORT}). Running network diagnostics..."
          python3 "$HERE/scripts/preflight.py" diag >>"$NIGHTY_DIAG_DIR/backend.log" 2>&1 || true
          python3 "$HERE/scripts/preflight.py" report --diag-dir "$NIGHTY_DIAG_DIR" --quiet >/dev/null 2>&1 || true
          echo "[run] === BACKEND LOG TAIL (LAST 25 LINES) ===" >&2
          tail -n 25 "$NIGHTY_DIAG_DIR/backend.log" 2>/dev/null >&2 || true
          echo "[run] ==========================================" >&2
          log "killing unresponsive backend and retrying."
          kill -9 "$BACKEND_PID" 2>/dev/null || true
        fi
      ) &
      WATCHDOG_PID=$!

      wait "$BACKEND_PID" 2>/dev/null || true
      kill "$WATCHDOG_PID" 2>/dev/null || true
      backend_ended=$(date +%s)
      backend_ran=$((backend_ended - backend_started))
      if [ "$backend_ran" -lt "$BACKEND_FAST_FAIL_SECONDS" ]; then
        fast_failures=$((fast_failures + 1))
      else
        fast_failures=0
      fi
      relaunch_delay="$(nighty_relaunch_delay "$fast_failures" "$BACKEND_MAX_BACKOFF")"
      if [ "$fast_failures" -eq "$BACKEND_TRIAGE_AFTER" ]; then
        log "backend exited after ${backend_ran}s on ${fast_failures} consecutive attempts — it is not starting."
        echo "[run] === BACKEND LOG TAIL (LAST 25 LINES) ===" >&2
        tail -n 25 "$NIGHTY_DIAG_DIR/backend.log" 2>/dev/null >&2 || true
        echo "[run] ==========================================" >&2
        python3 "$HERE/scripts/preflight.py" triage >&2 2>/dev/null || true
        log "backing off up to ${BACKEND_MAX_BACKOFF}s between attempts; fix the cause above, then restart."
      fi
      nighty_stop_wineserver
      cleanup_pyinstaller_temp
      if [ "$fast_failures" -gt 0 ]; then
        log "backend exited after ${backend_ran}s — relaunching in ${relaunch_delay}s (attempt $((fast_failures + 1)))."
      else
        log "backend exited — relaunching in ${relaunch_delay}s (persistence)."
      fi
      sleep "$relaunch_delay"
    done ) &
  BACKEND_LOOP_PID=$!

  wait
}

# ── entry point ──────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: bash scripts/run.sh [COMMAND]

Commands:
  once        Start the whole stack now, in this terminal (Ctrl+C to stop).
  autostart   Install + enable a service so it starts on every boot
              (systemd, OpenRC or runit - detected automatically).
  diag        Show environment and network diagnostics.
  --run       Same as 'once' (this is what the installed service uses).
  help        Show this help.

With no command it shows an interactive menu. Tip: when using the menu, do NOT
background it with '&' — a backgrounded prompt can't read the terminal. Use a
command instead, e.g.  bash scripts/run.sh autostart
EOF
}

case "${1:-}" in
  once|--run|run|--service) run_stack "${1:-}"; exit $? ;;
  autostart|--autostart)    setup_autostart; exit $? ;;
  diag|--diag)              python3 "$HERE/scripts/preflight.py" report --diag-dir "${NIGHTY_DIAG_DIR:-$HERE/diagnostics}"; exit $? ;;
  -h|--help|help)           usage; exit 0 ;;
  "")                       : ;;   # no command → interactive menu below
  *) echo "Unknown command: $1" >&2; usage >&2; exit 1 ;;
esac

# No command: if there's no real terminal (piped, or launched as a service),
# just run — never block on a prompt we can't show.
if [ ! -t 0 ] || [ ! -t 1 ]; then
  run_stack; exit $?
fi

# Interactive menu. Ignore SIGTTIN so that, if this was backgrounded with '&',
# the read fails cleanly (and we default to "run once") instead of suspending.
trap '' TTIN
echo
echo "  Nighty headless — how do you want to run it?"
echo "    1) Run now (one-off, in this terminal — Ctrl+C stops it)"
echo "    2) Set up autostart (systemd/OpenRC/runit) — starts automatically on every boot"
echo
printf "  Choice [1/2] (Enter = 1): "
if ! read -r choice; then
  echo; echo "  (no terminal input — starting once. For autostart run:  bash scripts/run.sh autostart)"
  choice=1
fi
trap - TTIN

case "${choice:-1}" in
  2)      setup_autostart; exit $? ;;
  1|"")   run_stack ;;
  *)      echo "  Unrecognised choice '$choice' — starting once."; run_stack ;;
esac
