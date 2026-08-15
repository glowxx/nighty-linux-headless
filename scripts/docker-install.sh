#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless — docker installer
#
#  One-liner deployment script for Docker environments.
#  Usage: bash <(curl -sL https://raw.githubusercontent.com/glowxx/nighty-linux-headless/main/scripts/docker-install.sh)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[31m'; M=$'\033[95m'; N=$'\033[0m'; else B=; G=; Y=; C=; R=; M=; N=; fi

print_header() { printf "\n%s%s============================================================%s\n %s %s\n%s%s============================================================%s\n\n" "$C" "$B" "$N" "🚀" "$1" "$C" "$B" "$N"; }
print_step() { printf "%s▶ %s%s\n" "$Y" "$1" "$N"; }
print_success() { printf "%s✔ %s%s\n" "$G" "$1" "$N"; }
print_error() { printf "%s✖ %s%s\n" "$R" "$1" "$N"; }
need() { command -v "$1" >/dev/null 2>&1; }

print_header "NIGHTY-LINUX-HEADLESS - DOCKER DEPLOYMENT"

# ── Ensure dependencies ──────────────────────────────────────────────────────
install_git() {
  need git && return 0
  print_step "Installing git..."
  if need apt-get; then sudo apt-get update && sudo apt-get install -y git
  elif need dnf; then sudo dnf install -y git
  elif need pacman; then sudo pacman -S --noconfirm --needed git
  elif need zypper; then sudo zypper --non-interactive install git
  else print_error "Install git manually, then run this script again."; exit 1
  fi
}
install_git

if ! need docker; then
  print_step "Installing Docker..."
  curl -fsSL https://get.docker.com -o get-docker.sh
  sudo sh get-docker.sh >/dev/null 2>&1
  rm get-docker.sh
fi

DIR="${NIGHTY_DOCKER_DIR:-$HOME/nighty-linux-headless}"
if [ ! -d "$DIR" ]; then
  print_step "Cloning repository to $DIR..."
  git clone https://github.com/glowxx/nighty-linux-headless.git "$DIR" >/dev/null 2>&1
fi

cd "$DIR"

while true; do
  printf "%s%sEnter desired Web UI Username:%s " "$M" "$B" "$N"
  read -r WEBUI_USER
  if [ -z "$WEBUI_USER" ]; then print_error "Username cannot be empty."; echo; continue; fi

  printf "%s%sEnter desired Web UI Password:%s " "$M" "$B" "$N"
  read -rs WEBUI_PASS
  printf '\n'
  if [ "${#WEBUI_PASS}" -lt 8 ]; then print_error "Password must contain at least 8 characters."; echo; continue; fi
  case "$WEBUI_PASS" in secret|change-this-please) print_error "Choose a non-default password."; echo; continue ;; esac

  printf "%s%sConfirm Web UI Password:%s " "$M" "$B" "$N"
  read -rs WEBUI_PASS_CONFIRM
  printf '\n'

  if [ "$WEBUI_PASS" = "$WEBUI_PASS_CONFIRM" ]; then
    printf '\n'
    break
  else
    clear
    print_header "NIGHTY-LINUX-HEADLESS - DOCKER DEPLOYMENT"
    print_error "Passwords do not match! Please try again."
    echo
  fi
done

printf "%s%sEnter max log size in MB (default 10):%s " "$M" "$B" "$N"
read -r LOG_SIZE_MB
LOG_SIZE_MB="$(printf '%s' "$LOG_SIZE_MB" | tr -dc '0-9')"
LOG_SIZE_MB="${LOG_SIZE_MB:-10}"
printf '\n'

# Store credentials as Compose secrets, never in docker-compose.yml or process
# arguments. Newlines are intentionally unsupported by Docker secret files.
umask 077
mkdir -p docker-secrets data diagnostics
# Clean up any bad directories Docker previously auto-created on the host
if [ -d "Nighty_stub.exe" ]; then
  print_step "Fixing directory created by Docker for Nighty_stub.exe..."
  rm -rf Nighty_stub.exe 2>/dev/null || sudo rm -rf Nighty_stub.exe 2>/dev/null || true
fi
# Clean up any empty files created by older versions of this script
if [ ! -s "Nighty_stub.exe" ] && [ -f "Nighty_stub.exe" ]; then
  rm -f "Nighty_stub.exe"
fi
printf '%s\n' "$WEBUI_USER" > docker-secrets/webui_username
printf '%s\n' "$WEBUI_PASS" > docker-secrets/webui_password

# Write log limit to .env for docker-compose to pick up (preserve existing .env if present)
if [ -f .env ]; then
  grep -v '^NIGHTY_LOG_MAX_SIZE=' .env > .env.tmp 2>/dev/null || true
  mv .env.tmp .env
fi
printf 'NIGHTY_LOG_MAX_SIZE=%sm\n' "$LOG_SIZE_MB" >> .env

if [ "$(id -u)" -eq 0 ]; then
  PUID="${PUID:-${SUDO_UID:-1000}}" PGID="${PGID:-${SUDO_GID:-1000}}"
else
  PUID="${PUID:-$(id -u)}" PGID="${PGID:-$(id -g)}"
fi
chmod 700 docker-secrets
chmod 644 docker-secrets/* 2>/dev/null || true

print_header "ALMOST DONE - ACTION REQUIRED"
printf "%s1. Upload your licensed 'Nighty.exe' to %s%s%s on your server.%s\n" "$Y" "$B" "$DIR" "$Y" "$N"
printf "%s   (You can use FileZilla, WinSCP, or your preferred SFTP client)%s\n\n" "$C" "$N"
printf "%s2. Once uploaded, run the start script:%s\n" "$Y" "$N"
printf "   %scd %s && bash scripts/docker-start.sh%s\n\n" "$C" "$DIR" "$N"
