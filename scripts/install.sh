#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless — installer
#
#  Beginner-friendly: it CHECKS what is already present and installs ONLY what
#  is missing, then repacks your Nighty.exe into a headless stub.
#
#  What it sets up (only if missing):
#    • base tools (curl, tar, xz, gnupg)         • Python 3
#    • Xvfb (virtual display)                     • uv (fetches matching Python)
#    • x86-64:  Wine (native, from your distro — or a static build if it's <11.5)
#    • non-x86: Box64  +  a static x86-64 Wine build (runs under Box64)
#
#  You only need to bring your own licensed Nighty.exe (it is never shipped).
#
#  Re-running is safe: anything already installed is detected and skipped.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT="$HERE/scripts/preflight.py"

RP_BLACKHOLE_IP="192.0.2.1"
RP_BLACKHOLE_IP_RE="192\.0\.2\.1"
RP_BLACKHOLE_HOSTS=(lrclib.net api.lrclib.net api.spotify.com)
RP_BLACKHOLE_UNIT="/etc/systemd/system/nighty-rp-blackhole.service"

blackhole_route_present() {
  ip route show "$RP_BLACKHOLE_IP" 2>/dev/null | grep -q unreachable
}

install_blackhole_route() {
  if ! command -v ip >/dev/null 2>&1; then
    warn "iproute2 not found — cannot install the unreachable route for $RP_BLACKHOLE_IP."
    warn "Without it a blackholed request leaves the host and hangs until timeout."
    return 1
  fi
  $SUDO ip route replace unreachable "$RP_BLACKHOLE_IP" >/dev/null 2>&1 || {
    warn "could not add the unreachable route for $RP_BLACKHOLE_IP (needs root)."
    return 1
  }
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemd not present — the unreachable route is active now but will not survive a reboot."
    warn "Add this to your init system or network hooks:  ip route replace unreachable $RP_BLACKHOLE_IP"
    return 0
  fi
  $SUDO tee "$RP_BLACKHOLE_UNIT" >/dev/null <<UNIT
[Unit]
Description=Unreachable route for the nighty RP-fetch blackhole
After=network-pre.target
Wants=network-pre.target
Before=nighty.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'exec ip route replace unreachable $RP_BLACKHOLE_IP'

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
  $SUDO systemctl enable --now nighty-rp-blackhole.service >/dev/null 2>&1 || {
    warn "could not enable nighty-rp-blackhole.service; the route is active but will not persist."
    return 1
  }
  return 0
}
cd "$HERE"

# ── pretty output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; C=; R=; N=; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
add()  { printf '  %s+%s %s\n' "$Y" "$N" "$*"; }
info() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

printf '\n%snighty-linux-headless installer%s\n\n' "$B" "$N"

# ── architecture ─────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) IS_X86=1; info "Architecture: $ARCH — Nighty runs natively under Wine." ;;
  *)            IS_X86=0; info "Architecture: $ARCH — Nighty runs under Wine on an x86-64 emulator (Box64)." ;;
esac

# ── sudo ─────────────────────────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if need sudo; then SUDO="sudo"
  else die "Not running as root and 'sudo' is not installed. Re-run as root, or install sudo first."; fi
fi

# ── package manager ──────────────────────────────────────────────────────────
PM=""; PM_INSTALL=""; PM_UPDATE=""
if   need apt-get; then PM=apt;    PM_UPDATE="$SUDO apt-get update";                    PM_INSTALL="$SUDO apt-get install -y"
elif need dnf;     then PM=dnf;    PM_UPDATE="$SUDO dnf -y makecache";                  PM_INSTALL="$SUDO dnf install -y"
elif need pacman;  then PM=pacman; PM_UPDATE="$SUDO pacman -Sy --noconfirm";            PM_INSTALL="$SUDO pacman -S --noconfirm --needed"
elif need zypper;  then PM=zypper; PM_UPDATE="$SUDO zypper --non-interactive refresh";  PM_INSTALL="$SUDO zypper --non-interactive install"
else warn "No supported package manager found (apt/dnf/pacman/zypper)."; warn "Missing packages can't be auto-installed — see README and install them manually."; fi

_PM_UPDATED=0
pm_refresh() { [ "$_PM_UPDATED" = 1 ] && return 0; [ -n "$PM_UPDATE" ] && { info "Refreshing package lists…"; $PM_UPDATE >/dev/null 2>&1 || true; }; _PM_UPDATED=1; }

# map a generic dependency to the distro package name(s)
pkg_name() {
  case "$PM:$1" in
    apt:xvfb) echo xvfb ;;     dnf:xvfb) echo xorg-x11-server-Xvfb ;;
    pacman:xvfb) echo xorg-server-xvfb ;;  zypper:xvfb) echo xorg-x11-server-Xvfb ;;
    apt:xz) echo xz-utils ;;   *:xz) echo xz ;;
    apt:gnupg) echo gnupg ;;   *:gnupg) echo gnupg2 ;;
    apt:wine) echo "wine64 wine" ;;        *:wine) echo wine ;;
    *) echo "$1" ;;
  esac
}

pm_install() {
  [ -n "$PM" ] || die "Need to install '$1' but no package manager is available. Install it manually and re-run."
  pm_refresh
  local pkg; pkg="$(pkg_name "$1")"
  add "installing: $pkg"
  # shellcheck disable=SC2086
  $PM_INSTALL $pkg >/dev/null 2>&1 || $PM_INSTALL $pkg || die "Failed to install: $pkg"
}

# ensure a command exists; install its package if not
ensure() { # <command> <generic-pkg> <label>
  if need "$1"; then ok "$3 present"; else add "$3 missing"; pm_install "$2"; need "$1" && ok "$3 installed" || die "$3 still missing after install"; fi
}

ensure_wine_runtime_libs() {
  python3 "$PREFLIGHT" libs --quiet && { ok "native Wine/X11 libraries present"; return 0; }
  warn "Some native libraries required by Wine/Box64 are missing."
  python3 "$PREFLIGHT" libs || true
  if [ "$PM" = apt ]; then
    local packages="libx11-6 libxext6 libxrender1 libxfixes3 libxrandr2 libxcomposite1 libxi6 libxcursor1 libxinerama1 libxkbregistry0 libegl1 libcups2"
    pm_refresh
    add "installing Wine/X11 runtime libraries"
    # shellcheck disable=SC2086
    $PM_INSTALL $packages >/dev/null 2>&1 || $PM_INSTALL $packages || die "Failed to install Wine/X11 runtime libraries."
    python3 "$PREFLIGHT" libs --quiet || die "Wine/X11 libraries are still missing after installation."
    ok "native Wine/X11 libraries installed"
  else
    warn "Automatic Wine library installation is currently limited to apt-based distributions."
    warn "Install the libraries listed above with your package manager before starting Nighty."
  fi
}

# download helper: dl <url> <dest>
dl() {
  if   need curl; then curl -fSL --retry 3 --connect-timeout 20 -o "$2" "$1"
  elif need wget; then wget -q -O "$2" "$1"
  else return 1; fi
}

# set KEY=VALUE in a file (replace if present, append otherwise)
set_kv() { # <file> <key> <value>
  if grep -qE "^$2=" "$1"; then
    local esc; esc="$(printf '%s' "$3" | sed -e 's/[\/&|]/\\&/g')"
    sed -i "s|^$2=.*|$2=$esc|" "$1"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
}

# ── 0) .env (create from example, then load so we honour existing settings) ──
if [ ! -f .env ]; then
  cp .env.example .env
  add "created .env — set your Web UI username/password in it before launching!"
else
  ok ".env already exists (keeping your settings)"
fi
. "$HERE/scripts/env_file.sh"
nighty_load_env_file ./.env

# ── 1) base tooling ──────────────────────────────────────────────────────────
info "Checking base tools…"
if ! need curl && ! need wget; then pm_install curl; fi
need curl || need wget || die "Need curl or wget."
ensure tar  tar  "tar"
# xz is only needed to unpack the static Wine tarball; ensure_wine_static_x86
# installs it on demand (it can now be needed on x86-64 too, as a fallback).

# ── 2) Python 3 + Xvfb ───────────────────────────────────────────────────────
ensure python3 python3 "Python 3"
ensure Xvfb    xvfb    "Xvfb (virtual display)"

# ── 3) uv (used to fetch the matching Python for the repack) ─────────────────
if need uv; then
  ok "uv present"
else
  add "uv missing — installing the official build"
  if need curl; then curl -LsSf https://astral.sh/uv/install.sh | sh
  else wget -qO- https://astral.sh/uv/install.sh | sh; fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  need uv && ok "uv installed" || die "uv installed but not on PATH — open a new shell (or 'source ~/.bashrc') and re-run."
fi

# ── 4) Wine (+ Box64 on non-x86) ─────────────────────────────────────────────
WINE_BIN_RESOLVED=""
NIGHTY_HOME="${NIGHTY_HOME:-$HOME/.local/share/nighty}"

# Make sure NIGHTY_HOME exists AND is writable by us. A stale .env can point it
# at a root-only path (e.g. /opt/nighty); plain mkdir would fail silently and
# later downloads/writes would break confusingly. Try as the user, then via
# sudo (+chown), and finally fall back to a guaranteed-writable home location.
ensure_runtime_home() {
  if mkdir -p "$NIGHTY_HOME" 2>/dev/null && [ -w "$NIGHTY_HOME" ]; then ok "Runtime dir: $NIGHTY_HOME"; return; fi
  if [ -n "$SUDO" ] && $SUDO mkdir -p "$NIGHTY_HOME" 2>/dev/null; then
    $SUDO chown -R "$(id -u):$(id -g)" "$NIGHTY_HOME" 2>/dev/null || true
    [ -w "$NIGHTY_HOME" ] && { ok "Runtime dir: $NIGHTY_HOME (created with sudo)"; return; }
  fi
  local fb="$HOME/.local/share/nighty"
  warn "NIGHTY_HOME '$NIGHTY_HOME' is not writable — using $fb instead."
  NIGHTY_HOME="$fb"
  mkdir -p "$NIGHTY_HOME" || die "Could not create a runtime directory at $NIGHTY_HOME."
  ok "Runtime dir: $NIGHTY_HOME"
}
ensure_runtime_home

# Wine's MAJOR version number (e.g. "9" from "wine-9.0"), or 0 if it can't be read.
wine_major() { # <wine-binary>
  local out; out="$("$1" --version 2>/dev/null | head -n1)"
  out="$(printf '%s' "$out" | grep -oE '[0-9]+' | head -n1)"
  printf '%s' "${out:-0}"
}

ensure_wine_x86() {
  # x86-64 normally runs Nighty under the distro's own Wine. BUT older Wine
  # HANGS Nighty during early startup — before the webview stub is even imported,
  # with no error, just a low-CPU stall (Ubuntu 22.04/24.04 ship Wine 6–9). Wine
  # Current Qt also requires ICU support from Wine >=11.5. If Wine is too
  # old (or absent) fall back to the same self-contained static build used on ARM
  # — which here runs natively, no Box64. The system Wine is left untouched.
  if ! need wine64 && ! need wine; then add "Wine missing"; pm_install wine; fi
  local bin ver
  bin="$(command -v wine64 || command -v wine || true)"
  if [ -n "$bin" ]; then
    ver="$(wine_major "$bin")"
    # Qt in current Nighty needs icuuc.dll, introduced after Wine 11.0.
    local version minor
    version="$("$bin" --version 2>/dev/null | head -n1)"
    minor="$(printf '%s' "$version" | sed -nE 's/.*wine-([0-9]+)\.([0-9]+).*/\2/p')"
    if [ "${ver:-0}" -gt 11 ] || { [ "${ver:-0}" -eq 11 ] && [ "${minor:-0}" -ge 5 ]; }; then
      ok "Wine present and new enough (v$ver): $bin"
      WINE_BIN_RESOLVED="$bin"; return
    fi
    warn "Distro Wine is $version — current Nighty requires Wine >=11.5."
  else
    warn "Distro Wine is not available from your package manager."
  fi
  info "Using a self-contained static Wine build instead (your system Wine is left as-is)…"
  ensure_wine_static_x86
}

box64_pkg_for_host() {
  local m=""
  [ -r /proc/device-tree/model ] && m="$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
  case "$m" in
    *"Raspberry Pi 5"*) echo box64-rpi5 ;;
    *"Raspberry Pi 4"*) echo box64-rpi4 ;;
    *"Raspberry Pi 3"*) echo box64-rpi3 ;;
    *)                  echo box64-generic-arm ;;
  esac
}

install_box64_apt() {
  info "Adding the Box64 APT repository…"
  ensure gpg gnupg "gnupg"
  pm_refresh
  local key=/etc/apt/trusted.gpg.d/box64-debian.gpg list=/etc/apt/sources.list.d/box64-debian.list
  if need curl; then
    curl -fsSL https://ryanfortner.github.io/box64-debian/KEY.gpg | $SUDO gpg --dearmor -o "$key" || return 1
    curl -fsSL https://ryanfortner.github.io/box64-debian/box64-debian.list | $SUDO tee "$list" >/dev/null || return 1
  else
    wget -qO- https://ryanfortner.github.io/box64-debian/KEY.gpg | $SUDO gpg --dearmor -o "$key" || return 1
    wget -qO- https://ryanfortner.github.io/box64-debian/box64-debian.list | $SUDO tee "$list" >/dev/null || return 1
  fi
  $SUDO apt-get update >/dev/null 2>&1 || true
  local pkg; pkg="$(box64_pkg_for_host)"
  add "installing: $pkg"
  $SUDO apt-get install -y "$pkg" || $SUDO apt-get install -y box64-generic-arm || return 1
}

install_box64_source() {
  warn "Falling back to building Box64 from source (this takes a few minutes)…"
  ensure git git "git"
  [ -n "$PM" ] && { pm_refresh; $PM_INSTALL build-essential cmake >/dev/null 2>&1 || $PM_INSTALL gcc make cmake >/dev/null 2>&1 || true; }
  need cmake || die "Box64 source build needs cmake — install it and re-run."
  local tmp; tmp="$(mktemp -d)"
  git clone --depth 1 https://github.com/ptitSeb/box64 "$tmp/box64" || { rm -rf "$tmp"; return 1; }
  ( cd "$tmp/box64" && mkdir -p build && cd build \
      && cmake .. -DARM_DYNAREC=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo >/dev/null \
      && make -j"$(nproc)" >/dev/null \
      && $SUDO make install >/dev/null ) || { rm -rf "$tmp"; return 1; }
  $SUDO systemctl restart systemd-binfmt 2>/dev/null || true
  rm -rf "$tmp"
}

ensure_box64() {
  if need box64; then ok "Box64 present"; return; fi
  add "Box64 missing"
  if [ "$PM" = apt ]; then install_box64_apt || install_box64_source; else install_box64_source; fi
  need box64 && ok "Box64 installed" || die "Could not install Box64 automatically. See https://github.com/ptitSeb/box64 and install it, then re-run."
}

# Point WINE_BIN_RESOLVED at the static build's launcher: prefer wine64 (the
# classic amd64 builds), but accept a lone
# wine too (newer WoW64-only builds ship a single "wine"). Empty if neither.
_resolve_static_wine_bin() { # <wine-dir>
  if   [ -x "$1/bin/wine64" ]; then WINE_BIN_RESOLVED="$1/bin/wine64"
  elif [ -x "$1/bin/wine" ];   then WINE_BIN_RESOLVED="$1/bin/wine"
  else WINE_BIN_RESOLVED=""; fi
}

ensure_wine_static_x86() {
  ensure xz xz "xz (extractor)"   # the static Wine tarball is .tar.xz
  ensure_wine_runtime_libs
  local wdir="$NIGHTY_HOME/wine" ver="${WINE_VERSION:-11.5}"
  _resolve_static_wine_bin "$wdir"
  if [ -n "$WINE_BIN_RESOLVED" ] && [ -f "$wdir/lib/wine/x86_64-windows/icuuc.dll" ]; then
    ok "static x86-64 Wine with ICU support present ($wdir)"; return
  fi
  add "static x86-64 Wine missing — downloading Wine $ver"
  local url="https://github.com/Kron4ek/Wine-Builds/releases/download/${ver}/wine-${ver}-amd64-wow64.tar.xz"
  local tgz="$NIGHTY_HOME/.wine-dl.tar.xz"
  info "Fetching $url"
  dl "$url" "$tgz" || die "Could not download Wine. Check your connection, or set WINE_VERSION to another release."
  local staging; staging="$(mktemp -d "$NIGHTY_HOME/.wine-install.XXXXXX")"
  info "Extracting Wine…"
  tar -xf "$tgz" -C "$staging" --strip-components=1 || die "Failed to extract the Wine tarball."
  [ -f "$staging/lib/wine/x86_64-windows/icuuc.dll" ] || die "Wine $ver lacks icuuc.dll; use Wine >=11.5. Existing runtime was not changed."
  if [ -e "$wdir" ]; then
    mv "$wdir" "$wdir.backup.$(date +%s)" || die "Could not back up existing Wine."
  fi
  mv "$staging" "$wdir" || die "Could not activate Wine."
  rm -f "$tgz"
  _resolve_static_wine_bin "$wdir"
  [ -n "$WINE_BIN_RESOLVED" ] || die "Wine extracted but no wine/wine64 launcher was found in $wdir/bin."
  ok "static x86-64 Wine ready ($wdir)"
}

if [ "$IS_X86" = 1 ]; then
  ensure_wine_x86
else
  ensure_box64
  ensure_wine_static_x86
fi

# ── 5) write resolved runtime paths back into .env ──────────────────────────
set_kv .env NIGHTY_HOME "$NIGHTY_HOME"
set_kv .env WINEPREFIX  "$NIGHTY_HOME/prefix"
[ -n "$WINE_BIN_RESOLVED" ] || die "Wine setup finished without a resolved WINE_BIN."
python3 "$PREFLIGHT" wine "$WINE_BIN_RESOLVED" || die "Resolved Wine launcher is not usable."
set_kv .env WINE_BIN "$WINE_BIN_RESOLVED"

# ── 6) locate your Nighty.exe ────────────────────────────────────────────────
SRC="${NIGHTY_EXE:-$HERE/Nighty.exe}"
OUT="${NIGHTY_STUB:-$HERE/Nighty_stub.exe}"
case "$SRC" in /*) : ;; ./*) SRC="$HERE/${SRC#./}" ;; esac
case "$OUT" in /*) : ;; ./*) OUT="$HERE/${OUT#./}" ;; esac
if [ ! -f "$SRC" ]; then
  printf '\n%sAlmost there — one manual step:%s\n' "$B" "$N"
  printf '  Copy YOUR licensed Nighty.exe into this folder, then re-run this installer:\n\n'
  printf '    cp /path/to/Nighty.exe %q\n'  "$SRC"
  printf '    bash scripts/install.sh\n\n'
  printf '  (Nighty.exe is never bundled or redistributed — bring your own copy.)\n'
  die "Nighty.exe not found at: $SRC"
fi
if [ "$IS_X86" = 1 ]; then
  python3 "$PREFLIGHT" pe "$SRC" || die "Nighty.exe failed PE validation."
else
  python3 "$PREFLIGHT" pe "$SRC" --require-x64 || die "ARM/Box64 requires a 64-bit x86-64 Nighty.exe."
fi

# The stub bytecode must match the executable, independently of host architecture.
REPACK_VERSION="$(python3 scripts/repack.py --python-version "$SRC")" || die "Could not detect Nighty's Python version."
info "Ensuring Python $REPACK_VERSION (via uv) for the repack…"
uv python install "$REPACK_VERSION" || die "Could not install Python $REPACK_VERSION via uv."
REPACK_PYTHON="$(uv python find "$REPACK_VERSION" 2>/dev/null || true)"
[ -n "$REPACK_PYTHON" ] || die "Could not obtain Python $REPACK_VERSION via uv."
ok "Python $REPACK_VERSION: $REPACK_PYTHON"

info "Repacking $(basename "$SRC") → $(basename "$OUT") (headless webview stub)…"
NIGHTY_EXE="$SRC" NIGHTY_STUB="$OUT" "$REPACK_PYTHON" scripts/repack.py "$SRC" "$OUT" || die "Repack failed."
if [ "$IS_X86" = 1 ]; then
  python3 "$PREFLIGHT" pe "$OUT" || die "Generated stub failed PE validation."
else
  python3 "$PREFLIGHT" pe "$OUT" --require-x64 || die "Generated stub is not x86-64."
fi
ok "Repack done: $OUT"

# ── 8) RP-fetch blackhole (performance) ──────────────────────────────────────
# Nighty's Rich-Presence task (rpcUpdater) makes *blocking* HTTP calls on the
# bot's asyncio event loop: song lyrics from lrclib.net and now-playing lookups
# from api.spotify.com. Under emulation each call can take many seconds and
# freezes the WHOLE bot — the gateway heartbeat times out and slash commands fail
# with "the application did not respond". Both are pointless on a headless box, so
# we point them at a dead address; the calls then fail instantly instead of
# hanging. (Kept under the historical BLOCK_LRCLIB switch for compatibility.)
info "Performance: blackholing RP fetch hosts (lrclib.net, api.spotify.com)…"
if [ "${BLOCK_LRCLIB:-1}" != 1 ]; then
  ok "skipped (BLOCK_LRCLIB=0)"
else
  hosts_targets=(/etc/hosts)
  os_id="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")"
  cloud_hosts="/etc/cloud/templates/hosts.${os_id}.tmpl"
  if grep -q "manage_etc_hosts.*True" /etc/hosts 2>/dev/null && [ -f "$cloud_hosts" ]; then
    hosts_targets+=("$cloud_hosts")
    info "cloud-init manages /etc/hosts; updating its $os_id template too"
  fi
  hosts_failed=0
  for hosts_file in "${hosts_targets[@]}"; do
    if ! grep -q "nighty-linux-headless: RP-fetch blackhole" "$hosts_file" 2>/dev/null; then
      printf '\n# nighty-linux-headless: RP-fetch blackhole (lyrics/now-playing fetches freeze the bot under emulation)\n' \
        | $SUDO tee -a "$hosts_file" >/dev/null 2>&1 || hosts_failed=1
    fi
    for host in "${RP_BLACKHOLE_HOSTS[@]}"; do
      host_re="$(printf '%s' "$host" | sed 's/\./\\./g')"
      if grep -Eq "^[[:space:]]*(0\.0\.0\.0|127\.0\.0\.1)[[:space:]]+$host_re([[:space:]]|$)" "$hosts_file" 2>/dev/null; then
        info "replacing a loopback-mapped blackhole entry for $host in $hosts_file"
        $SUDO sed -i -E "/^[[:space:]]*(0\.0\.0\.0|127\.0\.0\.1)[[:space:]]+$host_re([[:space:]]|\$)/d" \
          "$hosts_file" >/dev/null 2>&1 || hosts_failed=1
      fi
      if ! grep -Eq "^[[:space:]]*$RP_BLACKHOLE_IP_RE[[:space:]]+$host_re([[:space:]]|$)" "$hosts_file" 2>/dev/null; then
        printf '%s %s\n' "$RP_BLACKHOLE_IP" "$host" | $SUDO tee -a "$hosts_file" >/dev/null 2>&1 || hosts_failed=1
      fi
    done
  done

  install_blackhole_route || hosts_failed=1

  hosts_verified=1
  for host in "${RP_BLACKHOLE_HOSTS[@]}"; do
    host_re="$(printf '%s' "$host" | sed 's/\./\\./g')"
    grep -Eq "^[[:space:]]*$RP_BLACKHOLE_IP_RE[[:space:]]+$host_re([[:space:]]|$)" /etc/hosts || hosts_verified=0
  done
  if [ "$hosts_failed" -eq 0 ] && [ "$hosts_verified" -eq 1 ]; then
    ok "lrclib.net + api.spotify.com blackholed to $RP_BLACKHOLE_IP in /etc/hosts"
  else
    warn "could not fully update /etc/hosts — add the missing lines manually (needs root):"
    for host in "${RP_BLACKHOLE_HOSTS[@]}"; do
      warn "    $RP_BLACKHOLE_IP $host"
    done
    warn "  and the matching unreachable route:"
    warn "    ip route replace unreachable $RP_BLACKHOLE_IP"
  fi
fi

# ── done ─────────────────────────────────────────────────────────────────────
printf '\n%sSetup complete.%s Next:\n\n' "$G" "$N"
printf '  1) Set your Web UI login in .env:  %sWEBUI_USERNAME%s / %sWEBUI_PASSWORD%s\n' "$B" "$N" "$B" "$N"
printf '  2) Start everything with one command:\n'
printf '       %sbash scripts/run.sh%s\n' "$B" "$N"
printf '     It asks whether to run once or set up autostart (systemd). Or skip\n'
printf '     the menu:  bash scripts/run.sh once   |   bash scripts/run.sh autostart\n'
printf '  3) Open  http://<this-host-ip>:%s/  and follow the on-screen onboarding.\n\n' "${BRIDGE_PORT:-8088}"
