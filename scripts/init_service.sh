#!/usr/bin/env bash

nighty_detect_init_system() {
  if [ -n "${NIGHTY_INIT_SYSTEM:-}" ]; then
    printf '%s' "$NIGHTY_INIT_SYSTEM"
    return 0
  fi
  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    printf 'systemd'
    return 0
  fi
  if command -v rc-update >/dev/null 2>&1 && command -v openrc-run >/dev/null 2>&1; then
    printf 'openrc'
    return 0
  fi
  if command -v sv >/dev/null 2>&1 && [ -d /etc/sv ]; then
    printf 'runit'
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    printf 'systemd'
    return 0
  fi
  printf 'unknown'
}

nighty_systemd_unit_text() {
  local run_user="$1" workdir="$2"
  cat <<EOF
[Unit]
Description=Nighty headless (backend + Web UI bridge)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$run_user
WorkingDirectory=$workdir
ExecStart=/usr/bin/env bash $workdir/scripts/run.sh --run
Restart=always
RestartSec=5
SuccessExitStatus=23
RestartPreventExitStatus=23
KillMode=control-group
MemoryMax=90%

[Install]
WantedBy=multi-user.target
EOF
}

nighty_openrc_script_text() {
  local run_user="$1" workdir="$2"
  cat <<EOF
#!/sbin/openrc-run

name="nighty"
description="Nighty headless (backend + Web UI bridge)"

command="/usr/bin/env"
command_args="bash $workdir/scripts/run.sh --run"
command_user="$run_user"
directory="$workdir"
pidfile="/run/nighty.pid"

supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0

output_log="$workdir/diagnostics/service.log"
error_log="$workdir/diagnostics/service.log"

depend() {
	need net
	after firewall
}
EOF
}

nighty_runit_run_text() {
  local run_user="$1" workdir="$2"
  cat <<EOF
#!/bin/sh
exec 2>&1
cd "$workdir" || exit 1
chpst -u "$run_user" /usr/bin/env bash "$workdir/scripts/run.sh" --run
rc=\$?
if [ "\$rc" -eq 23 ]; then
  sv down nighty
  exit 0
fi
exit "\$rc"
EOF
}

nighty_runit_log_run_text() {
  local workdir="$1"
  cat <<EOF
#!/bin/sh
exec svlogd -tt "$workdir/diagnostics/svlog"
EOF
}
