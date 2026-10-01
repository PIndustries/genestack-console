#!/usr/bin/env bash
# install.sh — curl-pipeable installer for the genestack-console agent.
#
#   curl -fsSL https://console.example.com/agent | bash -s -- \
#       --hub wss://console.example.com:8080 --token gsca_...
#
# The console self-hosts this script at GET /agent, so the one-liner a token
# response hands you also works against the console itself:
#
#   curl -fsSL http://127.0.0.1:8080/agent | bash -s -- \
#       --hub ws://127.0.0.1:8080 --token gsca_...
#
# Same style as scripts/genestack-console.sh, much smaller. Phases:
# preflight → docker → agent source → image build → container run.
# Safe to re-run: an existing container is stopped and replaced.
# See --help for flags and the GSC_* environment knobs.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (all overridable via flags or GSC_* env vars)
# ---------------------------------------------------------------------------
HUB_URL="${GSC_HUB_URL:-}"
TOKEN="${GSC_AGENT_TOKEN:-}"
NAME="${GSC_AGENT_NAME:-gsc-agent}"
IMAGE="${GSC_AGENT_IMAGE:-gsc-agent:local}"
NO_START=0
DO_UNINSTALL=0
# CI escape hatch: never touch docker — just print the docker run command.
GSC_SKIP_DOCKER="${GSC_SKIP_DOCKER:-0}"

# Where to fetch the agent source (main.py + Containerfile) when this script
# is NOT run from a repo checkout (the curl-pipe case). Derived from --hub by
# default (the console self-hosts the source at /agent-src, the same hub that
# served this script); override with GSC_AGENT_SRC_URL (e.g. a raw github URL
# prefix or an internal mirror).
DEFAULT_SRC_BASE=""
SRC_BASE="${GSC_AGENT_SRC_URL:-$DEFAULT_SRC_BASE}"

SUDO=""
SRC=""
DOCKER=() # docker command, possibly prefixed with sudo

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_OFF=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_OFF=""
fi

phase() { printf '\n%s==> %s%s\n' "$C_BLUE" "$*" "$C_OFF"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '%s    OK: %s%s\n' "$C_GREEN" "$*" "$C_OFF"; }
warn()  { printf '%s    WARN: %s%s\n' "$C_YELLOW" "$*" "$C_OFF" >&2; }
die()   { printf '%s    ERROR: %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 1; }

usage() {
  cat <<'EOF'
Genestack Console agent installer

Usage:
  install.sh --hub <ws(s)://host[:port]> --token <gsca_...> [--name NAME] [--no-start]
  install.sh --uninstall [--name NAME]

Flags:
  --hub URL       ws:// or wss:// URL of the console hub (required)
  --token TOKEN   agent enrollment token from the portal (required)
  --name NAME     container/display name (default: gsc-agent)
  --no-start      build the image but do not (re)start the container (CI)
  --uninstall     stop and remove the agent container (image kept); idempotent
  -h, --help      this help

Environment knobs:
  GSC_HUB_URL / GSC_AGENT_TOKEN / GSC_AGENT_NAME
                  fallbacks for --hub / --token / --name
  GSC_AGENT_IMAGE image tag to build/run (default: gsc-agent:local)
   GSC_AGENT_SRC_URL
                   base URL to fetch agent source from (main.py +
                   Containerfile) when not run from a repo checkout
                   (default: derived from --hub, e.g.
                   http://console:8080/agent-src)
  GSC_SKIP_DOCKER=1
                  never touch docker; just print the docker run command (CI)
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --hub)
        [ $# -ge 2 ] || die "--hub needs a value"
        HUB_URL="$2"; shift 2 ;;
      --hub=*) HUB_URL="${1#*=}"; shift ;;
      --token)
        [ $# -ge 2 ] || die "--token needs a value"
        TOKEN="$2"; shift 2 ;;
      --token=*) TOKEN="${1#*=}"; shift ;;
      --name)
        [ $# -ge 2 ] || die "--name needs a value"
        NAME="$2"; shift 2 ;;
      --name=*) NAME="${1#*=}"; shift ;;
      --no-start) NO_START=1; shift ;;
      --uninstall) DO_UNINSTALL=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
  done
  if [ "$DO_UNINSTALL" -eq 1 ]; then
    return 0
  fi
  [ -n "$HUB_URL" ] || die "--hub is required (or set GSC_HUB_URL)"
  [ -n "$TOKEN" ] || die "--token is required (or set GSC_AGENT_TOKEN)"
  case "$HUB_URL" in
    ws://* | wss://*) ;;
    *) die "--hub must start with ws:// or wss:// (got: $HUB_URL)" ;;
  esac
  if [ -z "$SRC_BASE" ]; then
    SRC_BASE="${HUB_URL/ws:/http/}"/agent-src
    SRC_BASE="${SRC_BASE/wss:/https/}"
  fi
}

# ---------------------------------------------------------------------------
# Privilege helpers
# ---------------------------------------------------------------------------
setup_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

as_root() {
  if [ -n "$SUDO" ]; then
    sudo "$@"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# Phase: preflight
# ---------------------------------------------------------------------------
preflight() {
  phase "Phase 1/5: preflight checks"

  local kernel
  kernel="$(uname -s)"
  [ "$kernel" = "Linux" ] || die "unsupported OS: $kernel (linux only)"
  ok "OS: Linux"

  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|aarch64) ;;
    *) die "arch '$arch' unsupported — the agent image targets x86_64 and aarch64" ;;
  esac
  ok "arch: $arch"

  if [ "$(id -u)" -eq 0 ]; then
    ok "privileges: root"
  elif [ -n "$SUDO" ]; then
    ok "privileges: sudo available"
  else
    die "need root or sudo to install packages and run docker"
  fi
}

# ---------------------------------------------------------------------------
# Phase: docker
# ---------------------------------------------------------------------------
detect_docker() {
  DOCKER=()
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif [ -n "$SUDO" ] && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    warn "docker needs sudo (consider: sudo usermod -aG docker $(id -un))"
  else
    die "docker is installed but not usable by this user (fix group membership or run as root)"
  fi
  return 0
}

ensure_docker() {
  phase "Phase 2/5: docker"

  if detect_docker; then
    ok "docker present: $(docker --version 2>/dev/null || echo unknown)"
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    warn "docker not found and this host has no apt-get"
    return 1
  fi
  info "installing docker.io via apt"
  as_root apt-get update
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io
  detect_docker || die "docker still not usable after installing docker.io"
  ok "docker installed"
  return 0
}

# ---------------------------------------------------------------------------
# Phase: systemd fallback
# ---------------------------------------------------------------------------
systemd_fallback() {
  phase "Phase 2/5: docker unavailable — systemd fallback"
  warn "docker is not available on this host"
  info "creating systemd service unit for bare-metal agent"

  as_root mkdir -p /opt/genestack/agent
  as_root cp "$SRC/main.py" /opt/genestack/agent/main.py

  as_root tee /etc/systemd/system/gsc-agent.service >/dev/null <<EOFUNIT
[Unit]
Description=Genestack Console Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/genestack/agent/main.py
Environment=GSC_HUB_URL=$HUB_URL
Environment=GSC_AGENT_TOKEN=$TOKEN
Environment=GSC_AGENT_NAME=$NAME
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
WorkingDirectory=/opt/genestack/agent

[Install]
WantedBy=multi-user.target
EOFUNIT

  as_root systemctl daemon-reload
  as_root systemctl enable gsc-agent
  as_root systemctl start gsc-agent
  ok "systemd service 'gsc-agent' started"
  info "check status with: systemctl status gsc-agent"
  info "view logs with: journalctl -u gsc-agent -f"
}

# ---------------------------------------------------------------------------
# Phase: agent source
# ---------------------------------------------------------------------------
local_checkout() {
  # If this script runs from a repo checkout (agent/install.sh next to
  # main.py + Containerfile), echo the agent dir; empty otherwise (curl pipe).
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  if [ -n "$script_dir" ] && [ -f "$script_dir/main.py" ] && [ -f "$script_dir/Containerfile" ]; then
    printf '%s\n' "$script_dir"
  fi
}

fetch_source() {
  phase "Phase 3/5: agent source"

  SRC="$(local_checkout)"
  if [ -n "$SRC" ]; then
    ok "using local checkout: $SRC"
    return 0
  fi

  command -v curl >/dev/null 2>&1 || die "curl is required to fetch the agent source"
  SRC="$(mktemp -d)"
  info "downloading agent source from $SRC_BASE"

  _fetch_with_retry() {
    local url="$1" dest="$2" label="$3"
    curl -fsSL --max-time 30 -o "$dest" "$url" && return 0
    warn "first attempt failed for $label, retrying…"
    curl -fsSL --max-time 30 -o "$dest" "$url" || return 1
  }

  _fetch_with_retry "$SRC_BASE/main.py" "$SRC/main.py" "main.py" ||
    die "failed to fetch $SRC_BASE/main.py after retries (set GSC_AGENT_SRC_URL to a reachable mirror)"
  _fetch_with_retry "$SRC_BASE/Containerfile" "$SRC/Containerfile" "Containerfile" ||
    die "failed to fetch $SRC_BASE/Containerfile after retries (set GSC_AGENT_SRC_URL to a reachable mirror)"
  ok "source downloaded to $SRC"
}

# ---------------------------------------------------------------------------
# Phase: image build
# ---------------------------------------------------------------------------
run_command() {
  # Echo the docker run command the start phase (or GSC_SKIP_DOCKER) uses.
  printf 'docker run -d --name %s --restart unless-stopped -e GSC_HUB_URL=%s -e GSC_AGENT_TOKEN=%s -e GSC_AGENT_NAME=%s %s\n' \
    "$NAME" "$HUB_URL" "$TOKEN" "$NAME" "$IMAGE"
}

build_image() {
  phase "Phase 4/5: image build"
  [ -f "$SRC/Containerfile" ] || die "no Containerfile in $SRC"
  [ -f "$SRC/main.py" ] || die "no main.py in $SRC"
  "${DOCKER[@]}" build -t "$IMAGE" -f "$SRC/Containerfile" "$SRC"
  ok "image built: $IMAGE"
}

# ---------------------------------------------------------------------------
# Phase: run
# ---------------------------------------------------------------------------
start_agent() {
  phase "Phase 5/5: start agent"
  if [ "$NO_START" -eq 1 ]; then
    warn "--no-start: image built, container not started"
    info "start later with:"
    info "  $(run_command)"
    return 0
  fi

  # Idempotent replace: an existing container is stopped and removed first.
  if "${DOCKER[@]}" ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    info "replacing existing container: $NAME"
    "${DOCKER[@]}" rm -f "$NAME" >/dev/null
  fi
  "${DOCKER[@]}" run -d --name "$NAME" --restart unless-stopped \
    -e "GSC_HUB_URL=$HUB_URL" \
    -e "GSC_AGENT_TOKEN=$TOKEN" \
    -e "GSC_AGENT_NAME=$NAME" \
    "$IMAGE" >/dev/null
  ok "agent container running: $NAME"

  # Post-install connection test: verify the container hasn't crash-looped.
  info "verifying container health (up to 10 s)…"
  local _elapsed=0
  while [ "$_elapsed" -lt 10 ]; do
    local _state
    _state="$("${DOCKER[@]}" inspect --format '{{.State.Status}}' "$NAME" 2>/dev/null || true)"
    if [ "$_state" = "running" ]; then
      ok "container is healthy and running"
      break
    fi
    sleep 1
    _elapsed=$(( _elapsed + 1 ))
  done

  if [ "$_elapsed" -ge 10 ]; then
    warn "container did not reach 'running' state within 10 s"
    warn "last 20 lines of logs:"
    "${DOCKER[@]}" logs --tail 20 "$NAME" >&2 || true
  fi

  info "check the connection with:"
  info "  ${DOCKER[*]} logs -f $NAME     # look for 'connected to $HUB_URL'"
  info "or the env's agent status in the console (GET /api/v1/environments/<id>/agent/status)"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall() {
  phase "Uninstall agent '$NAME'"
  if [ "$GSC_SKIP_DOCKER" -eq 1 ]; then
    warn "GSC_SKIP_DOCKER=1 — would run: docker rm -f $NAME"
    return 0
  fi
  if ! detect_docker; then
    ok "no docker here — nothing to do"
    return 0
  fi
  if "${DOCKER[@]}" ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    "${DOCKER[@]}" rm -f "$NAME" >/dev/null
    ok "container '$NAME' stopped and removed (image '$IMAGE' kept)"
  else
    ok "no container named '$NAME' — nothing to do"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"
  setup_sudo
  if [ "$DO_UNINSTALL" -eq 1 ]; then
    uninstall
    return 0
  fi
  if [ "$GSC_SKIP_DOCKER" -eq 1 ]; then
    warn "GSC_SKIP_DOCKER=1 — not touching docker; run the agent with:"
    run_command
    return 0
  fi
  preflight
  fetch_source

  if ensure_docker; then
    build_image
    start_agent
  else
    systemd_fallback
  fi
}

main "$@"
