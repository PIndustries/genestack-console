#!/usr/bin/env bash
# genestack-console.sh — one-shot installer for the Genestack Console fleet hub.
#
# Default: download the compiled Linux binary and start it under systemd.
# You do not get a source tree. Optional: --docker loads a binary-wrapped
# image (still no source) for operators who want a container.
#
#   curl -fsSL https://genestack.dev/console.sh | bash
#
# Safe to re-run. The UI binds loopback only — SSH tunnel or VPN.
set -euo pipefail
# Stamped at publish time when the installer is deployed.
GSC_INSTALLER_REV="${GSC_INSTALLER_REV:-dev}"

# ---------------------------------------------------------------------------
# Defaults (all overridable via flags or GSC_* env vars)
# ---------------------------------------------------------------------------
# Linux fleet hub defaults. Darwin (Mac) overrides prefix + docker below —
# the published ELF is Linux-only; Mac runs the same compiled image in Docker.
PREFIX="${GSC_PREFIX:-/opt/genestack-console}"
PORT="${GSC_PORT:-8080}"
WITH_AIO=0
OS_KERNEL="$(uname -s 2>/dev/null || echo unknown)"
OS_ARCH="$(uname -m 2>/dev/null || echo unknown)"
HOST_KIND=linux
if [ "$OS_KERNEL" = "Darwin" ]; then
  HOST_KIND=darwin
elif [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; then
  HOST_KIND=wsl
else
  case "$OS_KERNEL" in
    MINGW* | MSYS* | CYGWIN* | Windows_NT | Windows*) HOST_KIND=windows ;;
  esac
fi
# Laptop shells cannot run the Linux ELF: Docker Desktop (or Colima) runs the
# published image. WSL2 is Linux — native binary, same as Ubuntu.
if [ "$HOST_KIND" = "darwin" ] || [ "$HOST_KIND" = "windows" ]; then
  PREFIX="${GSC_PREFIX:-$HOME/genestack-console}"
  if [ "${GSC_FROM_SOURCE:-0}" != "1" ] && [ "${GSC_USE_DOCKER:-0}" != "1" ]; then
    GSC_USE_DOCKER="${GSC_USE_DOCKER:-1}"
  fi
  GENESTACK_ROOT="${GENESTACK_ROOT:-$HOME/genestack}"
fi

needs_container_runtime() {
  [ "$HOST_KIND" = "darwin" ] || [ "$HOST_KIND" = "windows" ]
}
NON_INTERACTIVE=0
DO_UNINSTALL=0
DO_UPDATE=0
AUTO_UPDATE="${GSC_AUTO_UPDATE:-0}"
GSC_VERSION_URL="${GSC_VERSION_URL:-https://github.com/PIndustries/genestack-console/releases/latest/download/version.json}"

# Compiled Linux binary (Nuitka). Default install never clones source.
GSC_BINARY_URL="${GSC_BINARY_URL:-https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-linux-amd64}"
GSC_IMAGE="${GSC_IMAGE:-genestack-console:stable}"
GSC_IMAGE_TAR="${GSC_IMAGE_TAR:-https://genestack.dev/releases/genestack-console-linux-amd64-docker.tar.gz}"
GSC_FROM_SOURCE="${GSC_FROM_SOURCE:-0}"
GSC_USE_DOCKER="${GSC_USE_DOCKER:-0}"
GSC_BIND_HOST="${GSC_BIND_HOST:-127.0.0.1}"
RUNTIME=""          # binary | image
CONSOLE_BIN=""
# Public Genestack cloud tree (Apache-2.0). Not the Console. Tarball, not git.
GSC_GENESTACK_URL="${GSC_GENESTACK_URL:-https://github.com/rackerlabs/genestack/archive/refs/heads/main.tar.gz}"
GSC_REPO_URL="${GSC_REPO_URL:-}"         # only used with --from-source
GSC_REPO_REF="${GSC_REPO_REF:-}"
GSC_CONSOLE_SUBDIR="${GSC_CONSOLE_SUBDIR:-genestack-console}"
RUNTIME_IMAGE=""                         # tag compose actually runs
GSC_ADVERTISE_URL="${GSC_ADVERTISE_URL:-}"
GSC_CORS_ORIGIN="${GSC_CORS_ORIGIN:-}"
CORS_ORIGINS=()
GSC_SKIP_APT="${GSC_SKIP_APT:-0}"       # CI escape hatch: never touch apt
GSC_SKIP_ENGINE="${GSC_SKIP_ENGINE:-0}" # CI escape hatch: install files only
# PXE DHCP/HTTP run inside the compiled binary when an env has a pxe: section.
# This flag is kept for compatibility; it does not add a container.
WITH_PXE="${GSC_WITH_PXE:-0}"
# Optional Postgres sidecar (docker compose installs only): adds a
# postgres:16-alpine `db` service to the generated compose file and points
# the console's database_url at it. Default off (SQLite).
WITH_POSTGRES="${GSC_WITH_POSTGRES:-0}"
DB_PASSWORD="" # generated on first run; reused from an existing config.yaml

AIO_NAME="${GSC_AIO_NAME:-genestack-aio}"
AIO_CPUS="${GSC_AIO_CPUS:-4}"
AIO_MEM_MB="${GSC_AIO_MEM_MB:-8192}"
AIO_DISK="${GSC_AIO_DISK:-60G}"
AIO_SSH_PORT="${GSC_AIO_SSH_PORT:-2222}"
AIO_IMAGE_URL="${GSC_AIO_IMAGE_URL:-}"
AIO_IMAGE_PATH="${GSC_AIO_IMAGE_PATH:-}" # local base image (air-gapped); skips download
if [ -z "$AIO_IMAGE_URL" ]; then
  case "$OS_ARCH" in
    arm64 | aarch64)
      AIO_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
      ;;
    *)
      AIO_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
      ;;
  esac
fi

ENGINE=""          # docker | podman
DOCKER=()          # docker command, possibly prefixed with sudo
COMPOSE=()         # docker compose plugin, or docker-compose
SUDO=""
SRC=""
ADMIN_KEY=""

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

box() {
  # box "line" ... — boxed banner, sized to the longest line.
  local line width=0 border
  for line in "$@"; do
    [ "${#line}" -gt "$width" ] && width="${#line}"
  done
  printf -v border '%*s' $((width + 4)) ''
  border=${border// /=}
  printf '+%s+\n' "$border"
  for line in "$@"; do
    printf '|  %-*s  |\n' "$width" "$line"
  done
  printf '+%s+\n' "$border"
}

confirm() {
  # confirm "question" — interactive y/N; auto-yes when non-interactive or
  # when no tty is available (e.g. curl | bash without flags).
  if [ "$NON_INTERACTIVE" -eq 1 ]; then return 0; fi
  if [ ! -r /dev/tty ]; then
    warn "no tty; assuming yes: $1"
    return 0
  fi
  local reply
  printf '    %s [y/N] ' "$1" > /dev/tty
  read -r reply < /dev/tty
  case "$reply" in
    y | Y | yes | YES | Yes) return 0 ;;
    *) return 1 ;;
  esac
}

usage() {
  cat <<'EOF'
Genestack Console installer

Installs the fleet hub. Linux / WSL2: compiled ELF + systemd.
macOS and Windows (Git Bash): Docker Desktop (same compiled image).
You do not get a source tree. Native Windows without Docker or WSL is not a target.
Reach the UI over an SSH tunnel or VPN (binds 127.0.0.1). Then Guided setup
or the seeded demo/walkthrough tenant.

Usage:
  curl -fsSL https://genestack.dev/console.sh | bash
  curl -fsSL https://genestack.dev/console.sh | bash -s -- --dev
  genestack-console.sh --install [--prefix DIR] [--port N] [--advertise-url URL]
  genestack-console.sh --uninstall [--prefix DIR] [--non-interactive]
  genestack-console.sh --update [--prefix DIR]   # pull a newer binary if published

Actions:
  --install           Install the console (default when no action is given)
  --update            Check genestack.dev and replace the binary if newer
  --uninstall         Stop services, remove units, VM, and the prefix

Flags:
  --prefix DIR        Install root (default: /opt/genestack-console;
                      on macOS/Windows: ~/genestack-console)
  --port N            Host port for the console UI/API (default: 8080)
  --advertise-url URL URL agents use to reach this hub (writes hub.advertise_url)
  --cors-origin URL   Extra CORS origin (repeatable, or comma-separated)
  --with-aio-vm       Optional lab: all-in-one genestack dev VM (QEMU/KVM on
                      Linux, QEMU/HVF on macOS) and register it as an environment
  --dev               Laptop lab: same as --with-aio-vm (Console + AIO VM)
  --docker            Container path: load the published binary-wrapped image
                      (no source). Default on Linux is the host ELF + systemd.
                      Default on macOS and Windows Git Bash (the ELF is Linux-only).
  --from-source       Developer path: build from a local checkout (not default)
  --auto-update       Daily systemd timer that runs --update
  --non-interactive   Never prompt; assume yes (for curl | bash and CI)
  -h, --help          This help

Environment knobs:
  GSC_BINARY_URL      compiled Linux binary (default: the GitHub release asset)
  GSC_IMAGE           docker image tag after --docker load (default: genestack-console:stable)
  GSC_IMAGE_TAR       docker-save tarball of that image (default: genestack.dev/releases/…-docker.tar.gz)
  GSC_USE_DOCKER      1 = same as --docker
  GSC_BIND_HOST       address the binary binds (default: 127.0.0.1)
  GSC_FROM_SOURCE     1 = build from a checkout (developers)
  GSC_REPO_URL        git URL used only with --from-source (e.g. https://github.com/PIndustries/genestack-console.git)
  GSC_REPO_REF        git ref used only with --from-source (e.g. main)
  GSC_GENESTACK_URL   tarball of the public Genestack cloud tree
  GSC_ADVERTISE_URL   same as --advertise-url
  GSC_CORS_ORIGIN     extra CORS origins (comma-separated)
  GSC_SKIP_APT=1      do not touch apt (CI); missing deps become hard errors
  GSC_SKIP_ENGINE=1   install files only; never build/start containers (CI)
  GSC_WITH_PXE=1      PXE DHCP/HTTP run inside the console binary (Python).
                      No sidecar container. The host must sit on the
                      provisioning network (DHCP is L2 broadcast). Default
                      is on whenever an environment has a pxe: section.
  GSC_WITH_POSTGRES=1 add a postgres:16-alpine db service to the compose stack
                      (named volume + healthcheck; api/worker wait on it) and
                      point the console's database_url at it instead of SQLite
                      (docker compose installs only; podman stays on SQLite)
  GSC_AIO_NAME        VM + environment name (default: genestack-aio)
  GSC_AIO_CPUS        VM vCPUs (default: 4)
  GSC_AIO_MEM_MB      VM memory in MiB (default: 8192)
  GSC_AIO_DISK        VM disk size (default: 60G)
  GSC_AIO_SSH_PORT    host forward to VM ssh (default: 2222)
  GSC_AIO_IMAGE_URL   cloud image (default: Ubuntu noble amd64, or arm64 on Apple Silicon)
  GSC_AIO_IMAGE_PATH  local base image to use instead of downloading
  GENESTACK_ROOT      host path bind-mounted at /genestack in the container
                      (default: /opt/genestack; on macOS/Windows: ~/genestack).
                      Cloned on first install if empty.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
append_cors_origins() {
  # Split a comma/space-separated list onto CORS_ORIGINS.
  local raw="${1:-}" item
  raw="${raw//,/ }"
  for item in $raw; do
    [ -n "$item" ] && CORS_ORIGINS+=("$item")
  done
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --prefix)
        [ $# -ge 2 ] || die "--prefix needs a value"
        PREFIX="$2"; shift 2 ;;
      --prefix=*) PREFIX="${1#*=}"; shift ;;
      --port)
        [ $# -ge 2 ] || die "--port needs a value"
        PORT="$2"; shift 2 ;;
      --port=*) PORT="${1#*=}"; shift ;;
      --advertise-url)
        [ $# -ge 2 ] || die "--advertise-url needs a value"
        GSC_ADVERTISE_URL="$2"; shift 2 ;;
      --advertise-url=*) GSC_ADVERTISE_URL="${1#*=}"; shift ;;
      --cors-origin)
        [ $# -ge 2 ] || die "--cors-origin needs a value"
        append_cors_origins "$2"; shift 2 ;;
      --cors-origin=*) append_cors_origins "${1#*=}"; shift ;;
      --with-aio-vm | --dev) WITH_AIO=1; shift ;;
      --docker) GSC_USE_DOCKER=1; shift ;;
      --from-source) GSC_FROM_SOURCE=1; shift ;;
      --auto-update) AUTO_UPDATE=1; shift ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --install) DO_INSTALL=1; shift ;;
      --update) DO_UPDATE=1; shift ;;
      --uninstall) DO_UNINSTALL=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
  done
  if [ -n "${GSC_CORS_ORIGIN}" ]; then
    append_cors_origins "$GSC_CORS_ORIGIN"
  fi
  if [ "${DO_INSTALL:-0}" -eq 1 ] && [ "${DO_UNINSTALL:-0}" -eq 1 ]; then
    die "pick one: --install or --uninstall"
  fi
  if [ "${DO_UPDATE:-0}" -eq 1 ] && [ "${DO_UNINSTALL:-0}" -eq 1 ]; then
    die "pick one: --update or --uninstall"
  fi
  PREFIX="${PREFIX%/}"
  case "$PREFIX" in
    /*) ;;
    *) die "--prefix must be an absolute path (got: $PREFIX)" ;;
  esac
  case "$PREFIX" in
    / | /bin | /boot | /dev | /etc | /home | /lib | /lib64 | /proc | /root | /sbin | /sys | /usr | /var | /opt | /tmp | /mnt | /srv | /run)
      die "refusing unsafe --prefix: $PREFIX (pick a dedicated subdirectory)" ;;
  esac
  case "$PORT" in
    '' | *[!0-9]*) die "--port must be a number (got: $PORT)" ;;
    *)
      [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port out of range: $PORT"
      ;;
  esac
  case "$AIO_SSH_PORT" in
    '' | *[!0-9]*) die "GSC_AIO_SSH_PORT must be a number (got: $AIO_SSH_PORT)" ;;
  esac
}

# ---------------------------------------------------------------------------
# Privilege + platform helpers
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

need_root_or_die() {
  if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
    die "need root or sudo for: $1"
  fi
}

ensure_prefix() {
  if [ -d "$PREFIX" ] && [ -w "$PREFIX" ]; then
    return 0
  fi
  need_root_or_die "creating $PREFIX"
  as_root mkdir -p "$PREFIX"
  if [ "$(id -u)" -ne 0 ]; then
    as_root chown "$(id -u):$(id -g)" "$PREFIX"
  fi
}

# Owner-only mode on secret material under the install prefix. Safe to re-run.
# Covers config.yaml (+ .bak), SQLite DB, local-agent-token, and admin creds.
# Re-run is safe. Existing installs pick up the tighter mode on the next update.
harden_secret_perms() {
  local f
  for f in     "$PREFIX/config.yaml"     "$PREFIX/config.yaml.bak"     "$PREFIX/ADMIN_CREDENTIALS.txt"     "$PREFIX/data/console.db"     "$PREFIX/data/console.db-wal"     "$PREFIX/data/console.db-shm"     "$PREFIX/data/local-agent-token"
  do
    if [ -e "$f" ]; then
      chmod 0600 "$f" 2>/dev/null || as_root chmod 0600 "$f" 2>/dev/null || true
    fi
  done
}

# ---------------------------------------------------------------------------
# Phase: preflight
# ---------------------------------------------------------------------------
preflight() {
  phase "Phase 1/8: preflight checks"
  info "installer ${GSC_INSTALLER_REV}"
  info "platform: ${HOST_KIND} ${OS_KERNEL} ${OS_ARCH} prefix=${PREFIX}"

  if [ "$HOST_KIND" = "darwin" ]; then
    ok "OS: macOS (${OS_ARCH}) — Console runs in Docker; AIO uses QEMU/HVF"
    if [ "$WITH_AIO" -eq 1 ]; then
      if command -v sysctl >/dev/null 2>&1 && [ "$(sysctl -n kern.hv_support 2>/dev/null || echo 0)" != "1" ]; then
        die "macOS Hypervisor.framework is not available (kern.hv_support). The AIO VM needs HVF."
      fi
      ok "HVF: Hypervisor.framework available"
    fi
    return 0
  fi

  if [ "$HOST_KIND" = "windows" ]; then
    ok "OS: Windows (${OS_ARCH}) — Console runs in Docker Desktop (no native Win32 hub)"
    if [ "$WITH_AIO" -eq 1 ]; then
      die "the AIO VM is not supported from Git Bash / PowerShell. Use WSL2 Ubuntu (curl | bash --dev) for a local VM, or skip --dev and use the seeded demo/walkthrough tenant."
    fi
    return 0
  fi

  if [ "$HOST_KIND" = "wsl" ]; then
    ok "OS: WSL2 (${OS_ARCH}) — Linux binary path"
    if [ "$WITH_AIO" -eq 1 ] && { [ ! -e /dev/kvm ] || [ ! -r /dev/kvm ]; }; then
      die "WSL2 has no /dev/kvm. Nested virtualization is off or unsupported. Skip --dev and use the seeded walkthrough, or enable nested virt and KVM in this distro."
    fi
  fi

  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" = "ubuntu" ] && { [ "${VERSION_ID:-}" = "22.04" ] || [ "${VERSION_ID:-}" = "24.04" ]; }; then
      ok "OS: Ubuntu ${VERSION_ID}"
    else
      warn "supported targets are Ubuntu 22.04/24.04; detected '${PRETTY_NAME:-unknown}' — continuing anyway"
    fi
  else
    warn "no /etc/os-release; cannot verify distribution — continuing anyway"
  fi

  if [ "$OS_ARCH" = "x86_64" ]; then
    ok "arch: x86_64"
  elif [ "$WITH_AIO" -eq 1 ]; then
    die "arch '$OS_ARCH' unsupported on Linux — the AIO VM needs x86_64 (qemu-system-x86_64 + KVM). On Apple Silicon use macOS + --dev."
  else
    warn "arch '$OS_ARCH' is not x86_64; console container may still work, the AIO VM will not"
  fi

  if [ "$WITH_AIO" -eq 1 ]; then
    if [ ! -e /dev/kvm ]; then
      die "/dev/kvm not found. Enable hardware virtualization (VT-x/AMD-V) in BIOS/firmware; on a VM enable nested virtualization; then 'sudo modprobe kvm_intel' (or kvm_amd). Without KVM the AIO VM would be unusably slow, so refusing to continue."
    fi
    if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
      die "/dev/kvm exists but is not accessible by $(id -un). Fix: sudo usermod -aG kvm $(id -un) && re-login."
    fi
    ok "KVM: /dev/kvm present and accessible"
  fi
}

# ---------------------------------------------------------------------------
# Phase: dependencies
# ---------------------------------------------------------------------------
pkg_installed() { dpkg -s "$1" >/dev/null 2>&1; }

detect_engine() {
  ENGINE=""
  DOCKER=()
  COMPOSE=()
  if command -v docker >/dev/null 2>&1; then
    ENGINE="docker"
    if docker info >/dev/null 2>&1; then
      DOCKER=(docker)
    elif [ -n "$SUDO" ] && sudo docker info >/dev/null 2>&1; then
      DOCKER=(sudo docker)
      warn "docker needs sudo (consider: sudo usermod -aG docker $(id -un))"
    else
      ENGINE=""
      warn "docker is installed but not usable by this user"
    fi
  elif command -v podman >/dev/null 2>&1; then
    ENGINE="podman"
  fi
  if [ "$ENGINE" = "docker" ]; then
    if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
      COMPOSE=("${DOCKER[@]}" compose)
    elif command -v docker-compose >/dev/null 2>&1; then
      COMPOSE=(docker-compose)
    fi
  fi
}

require_cmd() {
  # require_cmd <command> <package-hint>
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' is missing (package: $2). Install it and re-run."
}

need_engine() {
  # Docker/podman is only required for --docker, --from-source, or Postgres.
  # The default (compiled binary + systemd) runs PXE in-process — no engine.
  # macOS always uses the published Linux image (no host ELF).
  [ "$GSC_FROM_SOURCE" -eq 1 ] || [ "$GSC_USE_DOCKER" -eq 1 ] \
    || [ "$WITH_POSTGRES" -eq 1 ]
}

brew_pkg() {
  # brew_pkg <formula> — install via Homebrew when missing.
  local formula="$1"
  command -v brew >/dev/null 2>&1 || return 1
  brew list --formula "$formula" >/dev/null 2>&1 && return 0
  info "brew install $formula"
  brew install "$formula"
}

qemu_system_bin() {
  case "$OS_ARCH" in
    arm64 | aarch64) echo "qemu-system-aarch64" ;;
    *) echo "qemu-system-x86_64" ;;
  esac
}

install_deps_windows() {
  require_cmd curl curl
  require_cmd python3 python3
  command -v jq >/dev/null 2>&1 || die "jq is required. In Git Bash: install jq, or use WSL2 Ubuntu and re-run the same curl | bash."
  detect_engine
  if [ "$GSC_SKIP_ENGINE" -eq 0 ] && need_engine; then
    [ -n "$ENGINE" ] || die "Docker Desktop is required on Windows (or use WSL2 Ubuntu). Start Docker Desktop, then re-run. There is no Win32 Console binary."
    [ "${#COMPOSE[@]}" -gt 0 ] || die "docker compose plugin missing (Docker Desktop includes it)"
    ok "container engine: $ENGINE (Windows)"
  fi
  ok "Windows dependencies present"
}

install_deps_darwin() {
  require_cmd curl curl
  require_cmd python3 python3
  if ! command -v jq >/dev/null 2>&1; then
    brew_pkg jq || die "jq is required. Install Homebrew (https://brew.sh) then: brew install jq"
  fi
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    command -v git >/dev/null 2>&1 || brew_pkg git || die "git is required (--from-source)"
  fi
  if [ "$WITH_AIO" -eq 1 ]; then
    if ! command -v "$(qemu_system_bin)" >/dev/null 2>&1 || ! command -v qemu-img >/dev/null 2>&1; then
      brew_pkg qemu || die "qemu is required for --dev / --with-aio-vm. Install Homebrew then: brew install qemu"
    fi
    require_cmd "$(qemu_system_bin)" qemu
    require_cmd qemu-img qemu
    require_cmd ssh-keygen openssh
  fi
  detect_engine
  if [ "$GSC_SKIP_ENGINE" -eq 0 ] && need_engine; then
    [ -n "$ENGINE" ] || die "Docker Desktop (or Colima) is required on macOS. Install it, start it, then re-run. The Console ELF is Linux-only; Docker runs the published image."
    if ! docker info >/dev/null 2>&1 && [ "${#DOCKER[@]}" -eq 0 ]; then
      die "Docker is installed but not running. Open Docker Desktop (or start Colima) and re-run."
    fi
    [ "${#COMPOSE[@]}" -gt 0 ] || die "docker compose plugin missing (Docker Desktop includes it)"
    ok "container engine: $ENGINE (macOS)"
  fi
  ok "macOS dependencies present"
}

install_deps() {
  phase "Phase 2/8: dependencies"

  if [ "$HOST_KIND" = "windows" ]; then
    install_deps_windows
    return 0
  fi
  if [ "$HOST_KIND" = "darwin" ]; then
    install_deps_darwin
    return 0
  fi

  local have_apt=0
  command -v apt-get >/dev/null 2>&1 && have_apt=1

  detect_engine

  if [ "$have_apt" -eq 0 ]; then
    warn "apt-get not found — assuming dependencies are managed externally"
    require_cmd curl curl
    require_cmd jq jq
    require_cmd python3 python3
    if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
      require_cmd git git
    fi
    if [ "$WITH_AIO" -eq 1 ]; then
      require_cmd "$(qemu_system_bin)" qemu
      require_cmd qemu-img qemu-utils
      command -v cloud-localds >/dev/null 2>&1 || warn "cloud-localds missing — will build the cloud-init ISO without it"
    fi
    if [ "$GSC_SKIP_ENGINE" -eq 0 ] && need_engine; then
      [ -n "$ENGINE" ] || die "no container engine (docker/podman) available"
    fi
    return 0
  fi

  local missing=()
  pkg_installed curl || missing+=(curl)
  pkg_installed jq || missing+=(jq)
  pkg_installed python3 || missing+=(python3)
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    pkg_installed git || missing+=(git)
  fi
  pkg_installed ca-certificates || missing+=(ca-certificates)
  if [ "$WITH_AIO" -eq 1 ]; then
    # qemu-system-x86 provides the binary; qemu-kvm is a virtual package on
    # newer Ubuntu (26.04+) and fails dpkg checks even when qemu is present.
    pkg_installed qemu-system-x86 || missing+=(qemu-system-x86)
    pkg_installed cloud-image-utils || missing+=(cloud-image-utils)
  fi
  if need_engine && [ -z "$ENGINE" ] && [ "$GSC_SKIP_ENGINE" -eq 0 ]; then
    missing+=(docker.io)
  fi

  if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$GSC_SKIP_APT" -eq 1 ]; then
      warn "GSC_SKIP_APT=1 — not installing: ${missing[*]}"
    else
      info "missing packages: ${missing[*]}"
      if confirm "Install missing packages via apt?"; then
        need_root_or_die "apt install"
        as_root apt-get update
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
      else
        die "cannot continue without: ${missing[*]}"
      fi
    fi
  else
    ok "all required packages already installed"
  fi

  # Compose plugin for docker (docker.io alone does not provide it).
  detect_engine
  if need_engine && [ "$ENGINE" = "docker" ] && [ "${#COMPOSE[@]}" -eq 0 ] && [ "$GSC_SKIP_ENGINE" -eq 0 ]; then
    if [ "$GSC_SKIP_APT" -eq 1 ]; then
      warn "GSC_SKIP_APT=1 — docker compose plugin missing"
    else
      info "installing docker compose plugin"
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 ||
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose ||
        die "could not install a docker compose plugin"
      detect_engine
    fi
  fi

  # Hard verification of what we actually exec later.
  require_cmd curl curl
  require_cmd jq jq
  require_cmd python3 python3
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    require_cmd git git
  fi
  if [ "$WITH_AIO" -eq 1 ]; then
    require_cmd qemu-system-x86_64 qemu-system-x86
    require_cmd qemu-img qemu-utils
    command -v cloud-localds >/dev/null 2>&1 || warn "cloud-localds missing — will build the cloud-init ISO without it"
    require_cmd ssh-keygen openssh-client
  fi
  if [ "$GSC_SKIP_ENGINE" -eq 0 ] && need_engine; then
    [ -n "$ENGINE" ] || die "no usable container engine (tried docker, podman). Install docker.io and re-run."
    if [ "$ENGINE" = "docker" ]; then
      [ "${#COMPOSE[@]}" -gt 0 ] || die "docker present but no compose plugin (install docker-compose-v2)"
    fi
    ok "container engine: $ENGINE"
  elif [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    warn "GSC_SKIP_ENGINE=1 — container build/start phases will be skipped"
  else
    ok "no container engine required (compiled binary on the host)"
  fi
}

# ---------------------------------------------------------------------------
# Phase: source / image
# ---------------------------------------------------------------------------
local_checkout() {
  # If this script runs from inside a repo checkout (scripts/genestack-console.sh next
  # to ../Containerfile), echo the console dir; empty otherwise (curl pipe).
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  if [ -n "$script_dir" ] && [ -f "$script_dir/../Containerfile" ]; then
    (cd "$script_dir/.." && pwd)
  fi
}

engine_image_exists() {
  local tag="$1"
  if [ "$ENGINE" = "docker" ]; then
    "${DOCKER[@]}" image inspect "$tag" >/dev/null 2>&1
  else
    podman image inspect "$tag" >/dev/null 2>&1
  fi
}

engine_pull() {
  local tag="$1"
  if [ "$ENGINE" = "docker" ]; then
    "${DOCKER[@]}" pull "$tag"
  else
    podman pull "$tag"
  fi
}

engine_load() {
  local tar="$1"
  if [ "$ENGINE" = "docker" ]; then
    "${DOCKER[@]}" load -i "$tar"
  else
    podman load -i "$tar"
  fi
}

install_source_from_checkout() {
  SRC="$PREFIX/src"
  ensure_prefix
  mkdir -p "$SRC"
  local local_src
  local_src="$(local_checkout)"
  [ -n "$local_src" ] || die "--from-source needs a checkout (scripts/ next to Containerfile) or GSC_REPO_URL"
  info "installing from local checkout: $local_src"
  (cd "$local_src" && tar cf - \
    --exclude=.git --exclude=.venv --exclude=data --exclude=log \
    --exclude='__pycache__' --exclude='*.egg-info' --exclude=.pytest_cache \
    .) | (cd "$SRC" && tar xf -)
  [ -f "$SRC/Containerfile" ] || die "source install failed — no Containerfile in $SRC"
  ok "source installed at $SRC"
}

install_binary() {
  CONSOLE_BIN="$PREFIX/bin/genestack-console"
  ensure_prefix
  mkdir -p "$PREFIX/bin"
  if [ -x "$CONSOLE_BIN" ]; then
    ok "binary already at $CONSOLE_BIN"
    RUNTIME=binary
    return 0
  fi
  local tmp src="$GSC_BINARY_URL"
  tmp="$(mktemp)"
  info "installing compiled console from $src"
  if [ -f "$src" ]; then
    cp -a "$src" "$tmp"
  elif [ "${src#file://}" != "$src" ] && [ -f "${src#file://}" ]; then
    cp -a "${src#file://}" "$tmp"
  elif ! curl -fSL "$src" -o "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  if ! file "$tmp" 2>/dev/null | grep -qi 'executable\|ELF'; then
    # still accept if it has a shebang-less ELF magic
    if [ "$(od -An -tx1 -N4 "$tmp" 2>/dev/null | tr -d ' \n')" != "7f454c46" ]; then
      rm -f "$tmp"
      return 1
    fi
  fi
  mv "$tmp" "$CONSOLE_BIN"
  chmod 0755 "$CONSOLE_BIN"
  RUNTIME=binary
  ok "installed binary $CONSOLE_BIN"
  return 0
}

install_docker_image() {
  # Operator container path: load a docker-save of the compiled ELF. No source.
  CONSOLE_BIN=""
  ensure_prefix
  if [ -z "$ENGINE" ]; then
    detect_engine
  fi
  [ -n "$ENGINE" ] || die "--docker needs docker or podman"
  local tar tmp
  if [ -n "$GSC_IMAGE_TAR" ]; then
    if [ -f "$GSC_IMAGE_TAR" ]; then
      tar="$GSC_IMAGE_TAR"
    else
      tmp="$(mktemp "${TMPDIR:-/tmp}/gsc-image.XXXXXX")"
      mv "$tmp" "$tmp.tar.gz"
      tmp="$tmp.tar.gz"
      info "downloading container image from $GSC_IMAGE_TAR"
      curl -fSL "$GSC_IMAGE_TAR" -o "$tmp" || die "could not download $GSC_IMAGE_TAR"
      tar="$tmp"
    fi
    info "docker load $tar"
    engine_load "$tar"
    [ -n "${tmp:-}" ] && rm -f "$tmp"
  elif [ -n "$GSC_IMAGE" ]; then
    info "pulling $GSC_IMAGE"
    engine_pull "$GSC_IMAGE"
  else
    die "--docker needs GSC_IMAGE_TAR or GSC_IMAGE"
  fi
  RUNTIME=image
  RUNTIME_IMAGE="$GSC_IMAGE"
  ok "container runtime $RUNTIME_IMAGE (compiled binary inside, no source)"
}

write_pxe_build() {
  # Tiny sidecar (dnsmasq + busybox httpd). Not the Console source tree.
  local dest="$PREFIX/pxe-build"
  mkdir -p "$dest" "$PREFIX/pxe"
  local local_src
  local_src="$(local_checkout)"
  if [ -n "$local_src" ] && [ -f "$local_src/pxe/Dockerfile" ]; then
    cp -a "$local_src/pxe/Dockerfile" "$local_src/pxe/entrypoint.sh" "$dest/"
  else
    cat > "$dest/Dockerfile" <<'EOF'
FROM alpine:3.21
RUN apk add --no-cache dnsmasq busybox-extras
EXPOSE 67/udp 8080/tcp
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
EOF
    cat > "$dest/entrypoint.sh" <<'EOF'
#!/bin/sh
set -eu
PXE_ROOT="${PXE_ROOT:-/srv/pxe}"
CONF="$PXE_ROOT/dnsmasq.conf"
HTTP_PORT="${PXE_HTTP_PORT:-8080}"
if [ ! -f "$CONF" ]; then
  echo "pxe sidecar: $CONF not found — sleeping until the console renders it" >&2
  exec sleep infinity
fi
mkdir -p /etc/dnsmasq.d
ln -sf "$CONF" /etc/dnsmasq.d/pxe.conf
httpd -f -p "$HTTP_PORT" -h "$PXE_ROOT" &
exec dnsmasq -k --conf-file=/etc/dnsmasq.d/pxe.conf
EOF
    chmod 0755 "$dest/entrypoint.sh"
  fi
  ok "PXE sidecar build context at $dest"
}

resolve_image() {
  phase "Phase 3/8: console runtime"
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    install_source_from_checkout
    RUNTIME=image
    RUNTIME_IMAGE="genestack-console:local"
    ok "will build $RUNTIME_IMAGE from source"
    return 0
  fi

  if [ "$GSC_USE_DOCKER" -eq 1 ]; then
    if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
      RUNTIME=image
      RUNTIME_IMAGE="$GSC_IMAGE"
      ok "SKIP_ENGINE: would load $RUNTIME_IMAGE"
      return 0
    fi
    install_docker_image
    return 0
  fi

  if needs_container_runtime; then
    info "${HOST_KIND}: using the published Linux container image (host ELF is Linux-only)"
    GSC_USE_DOCKER=1
    if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
      RUNTIME=image
      RUNTIME_IMAGE="$GSC_IMAGE"
      ok "SKIP_ENGINE: would load $RUNTIME_IMAGE"
      return 0
    fi
    install_docker_image
    return 0
  fi

  if install_binary; then
    return 0
  fi
  warn "no compiled binary at $GSC_BINARY_URL — not cloning source"

  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    die "GSC_SKIP_ENGINE=1 and no binary. Compile with scripts/compile-console.sh"
  fi

  die "no compiled binary. On a maintainer box: scripts/compile-console.sh then host the file at GSC_BINARY_URL. Default install does not clone source and does not ship a tarball of source."
}

ensure_genestack_root() {
  # Compose bind-mounts ${GENESTACK_ROOT:-/opt/genestack}:/genestack:ro.
  # Public Apache-2.0 cloud tree only — never the Console source.
  local dest="${GENESTACK_ROOT:-/opt/genestack}"
  local local_src parent="" tmp
  if [ -d "$dest/.git" ] || [ -d "$dest/bin" ]; then
    ok "genestack tree already present at $dest"
    return 0
  fi
  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    warn "GSC_SKIP_ENGINE=1 — skipping populate of $dest"
    return 0
  fi

  local_src="$(local_checkout)"
  if [ -n "$local_src" ]; then
    parent="$(cd "$local_src/.." && pwd)"
  fi

  if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null || true)" ]; then
    warn "$dest exists but has no .git/ or bin/ — not overwriting (catalog bind-mount may be empty)"
    return 0
  fi

  if [ -d "$dest" ] && [ -w "$dest" ]; then
    :
  elif mkdir -p "$dest" 2>/dev/null && [ -w "$dest" ]; then
    :
  else
    need_root_or_die "populating $dest (compose bind-mounts it at /genestack)"
    as_root mkdir -p "$dest"
  fi

  if [ -n "$parent" ] && [ -d "$parent/bin" ] && [ -f "$parent/openstack-components.yaml" ]; then
    info "copying genestack tree from $parent → $dest"
    if [ -w "$dest" ]; then
      (cd "$parent" && tar cf - \
        --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude=.pytest_cache \
        .) | tar xf - -C "$dest"
    else
      (cd "$parent" && tar cf - \
        --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude=.pytest_cache \
        .) | as_root tar xf - -C "$dest"
    fi
    [ -d "$dest/bin" ] || die "copy into $dest did not produce bin/"
    ok "genestack tree installed at $dest"
    return 0
  fi

  info "fetching public Genestack cloud tree (not the Console)"
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  curl -fSL "$GSC_GENESTACK_URL" -o "$tmp/genestack.tgz"
  tar -tzf "$tmp/genestack.tgz" >/dev/null
  tar -xzf "$tmp/genestack.tgz" -C "$tmp"
  local inner
  inner="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  [ -n "$inner" ] || die "genestack tarball had no top-level directory"
  if [ -w "$dest" ]; then
    (cd "$inner" && tar cf - .) | tar xf - -C "$dest"
  else
    as_root mkdir -p "$dest"
    (cd "$inner" && tar cf - .) | as_root tar xf - -C "$dest"
  fi
  rm -rf "$tmp"
  trap - RETURN
  [ -d "$dest/bin" ] || [ -f "$dest/openstack-components.yaml" ] ||
    die "unpack into $dest did not produce bin/ or openstack-components.yaml"
  ok "genestack tree installed at $dest"
}

# ---------------------------------------------------------------------------
# Phase: config
# ---------------------------------------------------------------------------
rand_token() {
  python3 -c 'import secrets, sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"
}

fernet_key() {
  python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
}

render_config() {
  phase "Phase 4/8: configuration"
  local cfg="$PREFIX/config.yaml"
  if [ -f "$cfg" ]; then
    ok "config already exists at $cfg (keeping existing keys/secrets)"
    harden_secret_perms
    if [ "$WITH_POSTGRES" -eq 1 ]; then
      # Reuse the password from the existing database_url so the db service
      # and the console keep agreeing across re-runs.
      DB_PASSWORD="$(sed -n 's/.*postgresql+psycopg:\/\/console:\([^@]*\)@.*/\1/p' "$cfg" | head -n1)"
      if [ -z "$DB_PASSWORD" ]; then
        warn "GSC_WITH_POSTGRES=1 but $cfg has no postgresql+psycopg database_url"
        warn "the db sidecar gets a fresh password — set database_url in $cfg to match:"
        warn "  database_url: postgresql+psycopg://console:<password>@db:5432/console"
      fi
    fi
    return 0
  fi
  ensure_prefix

  local admin_key operator_key viewer_key secret
  admin_key="gsc-admin-$(rand_token 24)"
  operator_key="gsc-operator-$(rand_token 24)"
  viewer_key="gsc-viewer-$(rand_token 24)"
  secret="$(fernet_key)"

  local db_url_line
  db_url_line="# database_url unset: SQLite at ./data/console.db. For Postgres, set e.g."
  if [ "$WITH_POSTGRES" -eq 1 ]; then
    DB_PASSWORD="$(rand_token 24)"
    db_url_line="database_url: postgresql+psycopg://console:${DB_PASSWORD}@db:5432/console"
  fi

  local cors_yaml origin advertise gs_root data_dir_cfg
  advertise="${GSC_ADVERTISE_URL:-}"
  cors_yaml="    - http://127.0.0.1:${PORT}
    - http://localhost:${PORT}"
  if [ "${#CORS_ORIGINS[@]}" -gt 0 ]; then
    for origin in "${CORS_ORIGINS[@]}"; do
      [ -n "$origin" ] || continue
      cors_yaml="${cors_yaml}
    - ${origin}"
    done
  fi
  # Absolute data dir so the frozen onefile does not write into its extract tmp.
  data_dir_cfg="${PREFIX}/data"
  local bind_host bind_port
  if [ "${RUNTIME:-}" = "image" ]; then
    gs_root="/genestack"
    # Container-internal bind; compose/podman maps the host port.
    bind_host="0.0.0.0"
    bind_port="8080"
  else
    gs_root="${GENESTACK_ROOT:-/opt/genestack}"
    bind_host="${GSC_BIND_HOST:-127.0.0.1}"
    bind_port="${PORT}"
  fi

  cat > "$cfg" <<EOF
# Genestack Console config — generated by scripts/genestack-console.sh on first run.
# Re-running the installer never overwrites this file. Edit freely.
# Jobs rehearse only until you set dry_run: false. The UI Guide tab will refuse to pretend this is live.
dry_run: true
# Labeled walkthrough tenant/env so first-boot UI has a sample full deployment.
seed_demo: true
data_dir: ${data_dir_cfg}
update:
  url: https://github.com/PIndustries/genestack-console/releases/latest/download/version.json
  auto: ${AUTO_UPDATE}
${db_url_line}
# (Postgres form: database_url: postgresql+psycopg://<user>:<pass>@<host>:5432/<db>)

# Fernet key for secrets at rest; unique to this install. Back up before rotating.
secret_key: ${secret}

auth:
  # Random per-install API keys (never the publicly-known dev-* keys).
  api_keys:
    ${admin_key}: admin
    ${operator_key}: operator
    ${viewer_key}: viewer
  session_ttl_hours: 12
  # Production posture: real login required. NEVER enable on anything exposed.
  dev_auto_login: false

# Binary installs use the host tree. Container installs bind-mount it at /genestack.
genestack:
  root: ${gs_root}
ansible:
  root: null
maas:
  url: ""
  api_key: ""

# URL agents/targets use to reach this hub. Empty until you pass --advertise-url
# (or GSC_ADVERTISE_URL). Agent install needs a URL the target host can reach.
hub:
  advertise_url: "${advertise}"

# OVH dedicated/Rise: add accounts in the UI (Admin → OVH accounts).
# ovh:
#   endpoint: https://eu.api.ovh.com/1.0
#   app_key: ""
#   app_secret: ""

# Listen address. Edit these and restart the console — no recompile.
# 127.0.0.1 = this host only (SSH tunnel). 0.0.0.0 = every interface.
server:
  host: ${bind_host}
  port: ${bind_port}

# Console-managed overlay. Hub is the WireGuard server; agents get a peer
# on first adopt (works through a physical firewall because the agent dials
# out first). Pick a network that does not overlap the hosts.
# Set enabled: true and wireguard.endpoint to an address agents can reach
# on UDP (typically the hub's public or VPN IP plus listen_port).
wireguard:
  enabled: false
  interface: wg-gsc
  network: 10.67.67.0/24
  listen_port: 51820
  endpoint: ""

jobs:
  timeout_seconds: 600

collector:
  enabled: true
  interval_seconds: 60
  probe_timeout_seconds: 15
  retention_hours: 168

metrics:
  enabled: false
  retention_hours: 72

# Hypervisor tier: the console discovers QEMU VMs (like the AIO dev VM) by
# scanning these roots for pidfiles and /proc for qemu-system processes.
hypervisor:
  enabled: true
  roots:
    - ${PREFIX}/vms

stream:
  max_subscribers: 100
  relay_enabled: true
  relay_interval_seconds: 3.0

# CORS restricted to the origin the UI is actually served from.
# Extra origins: --cors-origin / GSC_CORS_ORIGIN.
cors:
  allow_origins:
${cors_yaml}
EOF
  chmod 0600 "$cfg" # break-glass API keys + Fernet key: owner-only
  # If an operator left a config.yaml.bak beside it, lock that down too.
  [ -f "${cfg}.bak" ] && chmod 0600 "${cfg}.bak" || true
  harden_secret_perms
  ok "config written to $cfg (dev_auto_login: false, rotated keys, pinned CORS)"
}

admin_key_from_config() {
  awk -F: '/: admin$/{gsub(/^[ \t]+/, "", $1); print $1; exit}' "$PREFIX/config.yaml"
}

render_pxe_compose_service() {
  # Appended to compose. PXE build context is PREFIX/pxe-build (sidecar only).
  cat >> "$1" <<EOF
  pxe:
    build:
      context: ./pxe-build
      dockerfile: Dockerfile
    image: genestack-console-pxe:local
    container_name: genestack-console-pxe
    restart: unless-stopped
    network_mode: host
    cap_add:
      - NET_ADMIN
      - NET_RAW
    environment:
      - PXE_HTTP_PORT=8088
    volumes:
      - ./pxe:/srv/pxe
EOF
}

render_compose() {
  if [ "${RUNTIME:-}" = "binary" ]; then
    rm -f "$PREFIX/docker-compose.yml"
    if [ "$WITH_PXE" -eq 1 ]; then
      ok "PXE is in-process in the compiled binary (no sidecar container)"
    else
      ok "binary runtime — no compose file"
    fi
    return 0
  fi
  local file="$PREFIX/docker-compose.yml"
  # api/worker ordering: with GSC_WITH_POSTGRES=1 both wait on the db
  # healthcheck; the worker keeps its existing ordering after the api.
  local console_dep="" worker_dep
  worker_dep=$'    depends_on:\n      - console'
  if [ "$WITH_POSTGRES" -eq 1 ]; then
    [ -n "$DB_PASSWORD" ] || DB_PASSWORD="$(rand_token 24)"
    console_dep=$'    depends_on:\n      db:\n        condition: service_healthy'
    worker_dep=$'    depends_on:\n      console:\n        condition: service_started\n      db:\n        condition: service_healthy'
  fi
  [ -n "$RUNTIME_IMAGE" ] || RUNTIME_IMAGE="genestack-console:local"
  local build_block="" worker_cmd vol_data vol_pxe vol_cfg vol_vms
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    build_block=$'    build:\n      context: ./src\n      dockerfile: Containerfile\n      args:\n        GSC_BUILD: '"${GSC_BUILD:-unknown}"$'\n'
    worker_cmd='["python", "-m", "app.worker.runner", "--daemon", "--interval", "5"]'
    vol_data="./data:/app/data"
    vol_pxe="./pxe:/app/data/pxe"
    vol_cfg="./config.yaml:/app/config.yaml:ro"
    vol_vms="./vms:${PREFIX}/vms"
  else
    # Binary-wrapped image: ENTRYPOINT is genestack-console (no source tree).
    worker_cmd='["worker", "--daemon", "--interval", "5"]'
    vol_data="./data:/opt/genestack-console/data"
    vol_pxe="./pxe:/opt/genestack-console/data/pxe"
    vol_cfg="./config.yaml:/opt/genestack-console/config.yaml:ro"
    vol_vms="./vms:/opt/genestack-console/vms"
  fi
  local net_block="" ports_block
  ports_block=$'    ports:\n      - "127.0.0.1:'"${PORT}"$':8080"\n'
  if [ "$WITH_PXE" -eq 1 ]; then
    # In-process DHCP needs the host's provisioning NIC (L2 broadcast).
    # network_mode: host cannot be combined with ports:.
    net_block=$'    network_mode: host\n    cap_add:\n      - NET_ADMIN\n      - NET_RAW\n'
    ports_block=""
  fi
  cat > "$file" <<EOF
# Generated by scripts/genestack-console.sh — regenerated on each run.
# pid: host lets the console's hypervisor tier discover/manage host QEMU VMs.
# Default path runs a prebuilt image (no source tree on the host).
services:
  console:
${build_block}    image: ${RUNTIME_IMAGE}
    container_name: genestack-console
    restart: unless-stopped
    pid: host
${net_block}${ports_block}    volumes:
      - ${vol_data}
      - ${vol_pxe}
      - ${vol_cfg}
      - ${vol_vms}
      - ${GENESTACK_ROOT:-/opt/genestack}:/genestack:ro
${console_dep}
  worker:
${build_block}    image: ${RUNTIME_IMAGE}
    container_name: genestack-console-worker
    restart: unless-stopped
    pid: host
${net_block}    command: ${worker_cmd}
    volumes:
      - ${vol_data}
      - ${vol_pxe}
      - ${vol_cfg}
      - ${vol_vms}
      - ${GENESTACK_ROOT:-/opt/genestack}:/genestack:ro
${worker_dep}
EOF
  # ./pxe is always mounted into console/worker (the console writes the
  # sidecar's inputs there as data/pxe); the sidecar service itself is only
  # emitted with GSC_WITH_PXE=1.
  local extras=""
  if [ "$WITH_PXE" -eq 1 ]; then
    write_pxe_build
    {
      echo "  # PXE/DHCP sidecar (GSC_WITH_PXE=1). network_mode: host — DHCP is L2."
      render_pxe_compose_service /dev/stdout
    } >> "$file"
    extras="with pxe sidecar"
  fi
  # Appended LAST: the top-level volumes: map must not swallow later
  # services entries.
  if [ "$WITH_POSTGRES" -eq 1 ]; then
    cat >> "$file" <<EOF
  # Postgres sidecar (GSC_WITH_POSTGRES=1): config.yaml's database_url points
  # at this service; console/worker wait on its healthcheck. Data survives
  # rebuilds in the named volume. POSTGRES_PASSWORD only applies when the
  # volume is first initialized.
  db:
    image: postgres:16-alpine
    container_name: genestack-console-db
    restart: unless-stopped
    environment:
      POSTGRES_USER: console
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: console
    volumes:
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U console -d console"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 5s

volumes:
  db-data:
EOF
    extras="${extras:+$extras, }with postgres db"
  fi
  ok "compose file written to $file${extras:+ ($extras)}"
}

# ---------------------------------------------------------------------------
# Phase: systemd
# ---------------------------------------------------------------------------
install_update_timer() {
  [ "${AUTO_UPDATE}" = "1" ] || return 0
  local bin="${CONSOLE_BIN:-$PREFIX/bin/genestack-console}"
  mkdir -p "$PREFIX/systemd"
  cat > "$PREFIX/systemd/genestack-console-update.service" <<EOF
[Unit]
Description=Genestack Console auto-update
After=network-online.target

[Service]
Type=oneshot
Environment=GSC_PREFIX=${PREFIX}
Environment=CONSOLE_CONFIG=${PREFIX}/config.yaml
ExecStart=${bin} update
EOF
  cat > "$PREFIX/systemd/genestack-console-update.timer" <<EOF
[Unit]
Description=Daily Genestack Console update check

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF
  if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then
    as_root cp "$PREFIX/systemd/genestack-console-update.service" /etc/systemd/system/
    as_root cp "$PREFIX/systemd/genestack-console-update.timer" /etc/systemd/system/
    as_root systemctl daemon-reload
    as_root systemctl enable --now genestack-console-update.timer >/dev/null 2>&1 || true
    ok "daily auto-update timer enabled"
  else
    warn "not root — wrote $PREFIX/systemd/genestack-console-update.timer (enable it yourself)"
  fi
}

do_update() {
  phase "Update"
  local bin="${PREFIX}/bin/genestack-console"
  if [ -x "$bin" ]; then
    GSC_PREFIX="$PREFIX" CONSOLE_CONFIG="${PREFIX}/config.yaml" "$bin" update
    local rc=$?
    harden_secret_perms
    return $rc
  fi
  die "no compiled binary at $bin — install first"
}

render_systemd_binary() {
  mkdir -p "$PREFIX/systemd"
  local bin="${CONSOLE_BIN:-$PREFIX/bin/genestack-console}"
  cat > "$PREFIX/systemd/genestack-console.service" <<EOF
[Unit]
Description=Genestack Console API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=GSC_PREFIX=${PREFIX}
Environment=CONSOLE_CONFIG=${PREFIX}/config.yaml
WorkingDirectory=${PREFIX}
ExecStart=${bin} serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  cat > "$PREFIX/systemd/genestack-console-worker.service" <<EOF
[Unit]
Description=Genestack Console worker
After=network-online.target genestack-console.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PREFIX}
Environment=GSC_PREFIX=${PREFIX}
Environment=CONSOLE_CONFIG=${PREFIX}/config.yaml
ExecStart=${bin} worker --daemon --interval 5
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

render_systemd_unit() {
  if [ "${RUNTIME:-}" = "binary" ]; then
    render_systemd_binary
    return 0
  fi
  local engine_bin
  mkdir -p "$PREFIX/systemd"
  if [ "$ENGINE" = "podman" ]; then
    engine_bin="$(command -v podman)"
    # podman has no compose here — the unit starts the containers by name.
    local containers="genestack-console genestack-console-worker"
    [ "$WITH_PXE" -eq 1 ] && containers="$containers genestack-console-pxe"
    cat > "$PREFIX/systemd/genestack-console.service" <<EOF
[Unit]
Description=Genestack Console (podman containers)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${engine_bin} start ${containers}
ExecStop=${engine_bin} stop ${containers}
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
  else
    engine_bin="$(command -v docker || echo /usr/bin/docker)"
    local compose_bin=""
    if [ "${#COMPOSE[@]}" -gt 0 ] && [ "${COMPOSE[0]}" != "${DOCKER[0]:-docker}" ]; then
      compose_bin="$(command -v docker-compose || true)"
    fi
    if [ -n "$compose_bin" ]; then
      cat > "$PREFIX/systemd/genestack-console.service" <<EOF
[Unit]
Description=Genestack Console (docker compose stack)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PREFIX}
ExecStart=${compose_bin} -f ${PREFIX}/docker-compose.yml up -d
ExecStop=${compose_bin} -f ${PREFIX}/docker-compose.yml down
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF
    else
      cat > "$PREFIX/systemd/genestack-console.service" <<EOF
[Unit]
Description=Genestack Console (docker compose stack)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PREFIX}
ExecStart=${engine_bin} compose -f ${PREFIX}/docker-compose.yml up -d
ExecStop=${engine_bin} compose -f ${PREFIX}/docker-compose.yml down
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF
    fi
  fi
}

install_launchd() {
  phase "Phase 5/8: boot persistence (launchd)"
  mkdir -p "$PREFIX/bin" "$PREFIX/data"
  local docker_bin
  docker_bin="$(command -v docker || echo /usr/local/bin/docker)"
  cat > "$PREFIX/bin/console-up.sh" <<EOF
#!/bin/bash
set -euo pipefail
cd "$PREFIX"
if ! "$docker_bin" info >/dev/null 2>&1; then
  echo "docker is not running; start Docker Desktop and retry" >&2
  exit 1
fi
if "$docker_bin" compose version >/dev/null 2>&1; then
  exec "$docker_bin" compose -f "$PREFIX/docker-compose.yml" up -d
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f "$PREFIX/docker-compose.yml" up -d
fi
echo "docker compose not found" >&2
exit 1
EOF
  chmod 0755 "$PREFIX/bin/console-up.sh"
  local plist_dir="$HOME/Library/LaunchAgents"
  local plist="$plist_dir/dev.genestack.console.plist"
  mkdir -p "$plist_dir"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.genestack.console</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PREFIX/bin/console-up.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$PREFIX</string>
  <key>StandardOutPath</key>
  <string>$PREFIX/data/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$PREFIX/data/launchd.err.log</string>
</dict>
</plist>
EOF
  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    ok "wrote $plist (not loaded — SKIP_ENGINE)"
    return 0
  fi
  launchctl bootout "gui/$(id -u)/dev.genestack.console" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || launchctl load -w "$plist" >/dev/null 2>&1 || true
  ok "launchd agent loaded: dev.genestack.console (starts Console when you log in)"
}

install_systemd() {
  if [ "$HOST_KIND" = "darwin" ]; then
    install_launchd
    return 0
  fi
  if [ "$HOST_KIND" = "windows" ]; then
    phase "Phase 5/8: boot persistence"
    warn "Windows: no systemd/launchd — Docker Desktop restart: unless-stopped keeps the hub up"
    return 0
  fi
  phase "Phase 5/8: boot persistence (systemd)"
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found — skipping boot persistence (containers still restart via 'restart: unless-stopped')"
    return 0
  fi
  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    warn "GSC_SKIP_ENGINE=1 — writing unit under $PREFIX/systemd but not installing it"
    render_systemd_unit
    return 0
  fi

  render_systemd_unit
  local unit="$PREFIX/systemd/genestack-console.service"

  if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then
    as_root cp "$unit" /etc/systemd/system/genestack-console.service
    if [ -f "$PREFIX/systemd/genestack-console-worker.service" ]; then
      as_root cp "$PREFIX/systemd/genestack-console-worker.service" /etc/systemd/system/genestack-console-worker.service
    fi
    as_root systemctl daemon-reload
    as_root systemctl enable genestack-console.service >/dev/null 2>&1 || true
    if [ -f /etc/systemd/system/genestack-console-worker.service ]; then
      as_root systemctl enable genestack-console-worker.service >/dev/null 2>&1 || true
    fi
    ok "system unit installed and enabled: genestack-console.service"
  elif systemctl --user status >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cp "$unit" "$HOME/.config/systemd/user/genestack-console.service"
    systemctl --user daemon-reload
    systemctl --user enable genestack-console.service >/dev/null 2>&1 || true
    ok "user unit installed: ~/.config/systemd/user/genestack-console.service"
    warn "for boot persistence without login: sudo loginctl enable-linger $(id -un)"
  else
    warn "no usable systemd — skipping; the compose 'restart: unless-stopped' policy still applies"
  fi
}

# ---------------------------------------------------------------------------
# Phase: launch
# ---------------------------------------------------------------------------
prepare_dirs() {
  ensure_prefix
  mkdir -p "$PREFIX/data" "$PREFIX/pxe" "$PREFIX/vms" "$PREFIX/images" "$PREFIX/ssh"
  # data/ holds console.db + local-agent-token; restrict the dir (files get 0600
  # via harden_secret_perms / app writes).
  chmod 0700 "$PREFIX/data" 2>/dev/null || true
  harden_secret_perms
  # Console writes PXE under data_dir/pxe; the sidecar serves PREFIX/pxe.
  if [ ! -e "$PREFIX/data/pxe" ]; then
    ln -s ../pxe "$PREFIX/data/pxe"
  fi
  # The container runs as uid 1000 and must be able to write the sqlite DB
  # (data/) and PXE files (pxe/). Skip when we are not going to start it.
  if [ "${RUNTIME:-}" = "image" ] && [ "$GSC_SKIP_ENGINE" -eq 0 ] && [ "$(id -u)" -ne 1000 ] && ! needs_container_runtime; then
    need_root_or_die "chown of $PREFIX/data and $PREFIX/pxe for the container uid"
    as_root chown -R 1000:1000 "$PREFIX/data" "$PREFIX/pxe"
  fi
}

build_id() {
  # Git short hash for the GSC_BUILD image build arg; 'unknown' outside a
  # checkout. $PREFIX/src is a .git-less copy, so prefer the local checkout
  # when the installer runs from one.
  local dir
  dir="$(local_checkout)"
  [ -n "$dir" ] || dir="$PREFIX/src"
  git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo unknown
}

podman_up() {
  if [ "$WITH_POSTGRES" -eq 1 ]; then
    warn "GSC_WITH_POSTGRES=1 needs docker compose; the podman path stays on SQLite"
    warn "to use Postgres with podman, run it externally and set database_url in config.yaml"
  fi
  [ -n "$RUNTIME_IMAGE" ] || RUNTIME_IMAGE="genestack-console:local"
  if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
    podman build --build-arg "GSC_BUILD=$(build_id)" \
      -t "$RUNTIME_IMAGE" -f "$PREFIX/src/Containerfile" "$PREFIX/src"
  fi
  podman rm -f genestack-console genestack-console-worker >/dev/null 2>&1 || true
  podman run -d --name genestack-console --restart unless-stopped --pid host \
    -p "127.0.0.1:${PORT}:8080" \
    -v "$PREFIX/data:/app/data" \
    -v "$PREFIX/pxe:/app/data/pxe" \
    -v "$PREFIX/config.yaml:/app/config.yaml:ro" \
    -v "$PREFIX/vms:$PREFIX/vms" \
    "$RUNTIME_IMAGE" >/dev/null
  podman run -d --name genestack-console-worker --restart unless-stopped --pid host \
    -v "$PREFIX/data:/app/data" \
    -v "$PREFIX/pxe:/app/data/pxe" \
    -v "$PREFIX/config.yaml:/app/config.yaml:ro" \
    -v "$PREFIX/vms:$PREFIX/vms" \
    "$RUNTIME_IMAGE" \
    python -m app.worker.runner --daemon --interval 5 >/dev/null
  if [ "$WITH_PXE" -eq 1 ]; then
    # PXE sidecar: host networking is required — DHCP is L2 broadcast.
    podman build -t genestack-console-pxe:local -f "$PREFIX/src/pxe/Dockerfile" \
      "$PREFIX/src/pxe"
    podman rm -f genestack-console-pxe >/dev/null 2>&1 || true
    podman run -d --name genestack-console-pxe --restart unless-stopped \
      --network host \
      -v "$PREFIX/pxe:/srv/pxe" \
      genestack-console-pxe:local >/dev/null
  fi
}

launch_console() {
  phase "Phase 6/8: start the console"
  prepare_dirs
  if [ "${RUNTIME:-}" = "binary" ]; then
    if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
      warn "GSC_SKIP_ENGINE=1 — not starting the binary"
      return 0
    fi
    local bin="${CONSOLE_BIN:-$PREFIX/bin/genestack-console}"
    if command -v systemctl >/dev/null 2>&1 && { [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; }; then
      as_root systemctl restart genestack-console.service
      as_root systemctl restart genestack-console-worker.service || true
    else
      warn "start manually: $bin serve --host ${GSC_BIND_HOST} --port $PORT"
    fi
    ok "console binary started"
    return 0
  fi
  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    warn "GSC_SKIP_ENGINE=1 — skipping container build/start"
    return 0
  fi
  if [ "$ENGINE" = "docker" ]; then
    if [ "$GSC_FROM_SOURCE" -eq 1 ]; then
      (cd "$PREFIX" && GSC_BUILD="$(build_id)" "${COMPOSE[@]}" up -d --build)
    else
      (cd "$PREFIX" && "${COMPOSE[@]}" up -d)
    fi
  else
    podman_up
  fi
  ok "console containers up (api + worker)"
}

# ---------------------------------------------------------------------------
# Phase: bootstrap (health, admin user, credentials)
# ---------------------------------------------------------------------------
wait_health() {
  local url="http://127.0.0.1:${PORT}/health" _try
  for _try in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

engine_exec() {
  if [ "$ENGINE" = "docker" ]; then
    (cd "$PREFIX" && "${COMPOSE[@]}" exec -T console "$@")
  else
    podman exec genestack-console "$@"
  fi
}

print_creds_banner() {
  box \
    "Genestack Console admin credentials (shown ONCE)" \
    "" \
    "  UI:       http://127.0.0.1:${PORT}/ui" \
    "  Username: admin" \
    "  Password: ${1}" \
    "" \
    "Also stored (mode 600) in:" \
    "  ${PREFIX}/ADMIN_CREDENTIALS.txt"
}

print_demo_banner() {
  box \
    "Walkthrough sample is already there" \
    "" \
    "  Tenant:      demo" \
    "  Environment: walkthrough" \
    "" \
    "Open Overview (honeycomb / 3D) to click around a sample" \
    "full deployment. It is not your metal." \
    "Create a new environment when you are ready to provision."
}

bootstrap() {
  phase "Phase 7/8: bootstrap"
  if [ "$GSC_SKIP_ENGINE" -eq 1 ]; then
    warn "GSC_SKIP_ENGINE=1 — skipping health check and admin bootstrap"
    info "after starting the stack, create the admin user with:"
    info "  docker compose -f $PREFIX/docker-compose.yml exec console \\"
    info "    python -m app.cli create-user --username admin --platform-admin --password '<pw>'"
    return 0
  fi

  info "waiting for http://127.0.0.1:${PORT}/health"
  if ! wait_health; then
    if [ "$ENGINE" = "docker" ]; then
      (cd "$PREFIX" && "${COMPOSE[@]}" logs --tail 50 console) >&2 || true
    else
      podman logs --tail 50 genestack-console >&2 || true
    fi
    die "console did not become healthy in 120s — see logs above"
  fi
  ok "console is healthy"

  local creds="$PREFIX/ADMIN_CREDENTIALS.txt"
  if [ -f "$creds" ]; then
    ok "admin credentials already exist at $creds (not regenerating)"
    printf '\n'
    print_demo_banner
    printf '\n'
    return 0
  fi

  local password
  password="$(rand_token 18)"
  if [ "${RUNTIME:-}" = "binary" ]; then
    local bin="${CONSOLE_BIN:-$PREFIX/bin/genestack-console}"
    if ! GSC_PREFIX="$PREFIX" CONSOLE_CONFIG="$PREFIX/config.yaml" \
      "$bin" create-user --username admin --password "$password" --platform-admin >/dev/null; then
      die "failed to create the admin user with $bin create-user"
    fi
  elif ! engine_exec python -m app.cli create-user --username admin --password "$password" --platform-admin >/dev/null; then
    die "failed to create the admin user inside the container"
  fi

  ADMIN_KEY="$(admin_key_from_config)"
  umask 077
  cat > "$creds" <<EOF
Genestack Console — admin credentials
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

UI:        http://127.0.0.1:${PORT}/ui
API:       http://127.0.0.1:${PORT}/api/v1
Username:  admin
Password:  ${password}

Admin API key (X-API-Key header, break-glass):
  ${ADMIN_KEY}

Rotate these before exposing the console beyond this host.
EOF
  chmod 600 "$creds"
  printf '\n'
  print_creds_banner "$password"
  printf '\n'
  print_demo_banner
  printf '\n'
}

# ---------------------------------------------------------------------------
# Phase: AIO dev VM
# ---------------------------------------------------------------------------
aio_prepare() {
  local vm_dir="$PREFIX/vms/$AIO_NAME"
  mkdir -p "$vm_dir" "$PREFIX/images" "$PREFIX/ssh"

  # Base cloud image (cached).
  local base
  if [ -n "$AIO_IMAGE_PATH" ]; then
    [ -f "$AIO_IMAGE_PATH" ] || die "GSC_AIO_IMAGE_PATH does not exist: $AIO_IMAGE_PATH"
    base="$AIO_IMAGE_PATH"
    info "using local base image: $base"
  else
    base="$PREFIX/images/$(basename "$AIO_IMAGE_URL")"
    if [ -f "$base" ]; then
      ok "cloud image cached: $base"
    else
      info "downloading $AIO_IMAGE_URL"
      curl -fSL --continue-at - -o "$base.partial" "$AIO_IMAGE_URL"
      mv "$base.partial" "$base"
    fi
  fi

  # Overlay disk.
  if [ -f "$vm_dir/$AIO_NAME.qcow2" ]; then
    ok "overlay disk exists: $vm_dir/$AIO_NAME.qcow2"
  else
    qemu-img create -f qcow2 -b "$base" -F qcow2 "$vm_dir/$AIO_NAME.qcow2" "$AIO_DISK" >/dev/null
    ok "overlay disk created ($AIO_DISK, backed by the base image)"
  fi

  # SSH keypair for the VM.
  local key="$PREFIX/ssh/${AIO_NAME}_key"
  if [ -f "$key" ]; then
    ok "ssh keypair exists: $key"
  else
    ssh-keygen -t ed25519 -N "" -C "$AIO_NAME" -f "$key" >/dev/null
    ok "ssh keypair generated in $PREFIX/ssh"
  fi

  # Cloud-init seed. The VM carries genestack's real getting-started path:
  # clone the repo, run bootstrap.sh, then the AIO profile (kubespray
  # inventory + bin/install-*.sh) — driven by the operator or the console.
  if [ -f "$vm_dir/seed.iso" ]; then
    ok "cloud-init seed exists: $vm_dir/seed.iso"
  else
    local pubkey
    pubkey="$(cat "$key.pub")"
    cat > "$vm_dir/user-data" <<EOF
#cloud-config
hostname: ${AIO_NAME}
fqdn: ${AIO_NAME}.genestack.local
manage_etc_hosts: true
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ${pubkey}
package_update: true
packages:
  - git
  - curl
  - ca-certificates
write_files:
  - path: /usr/local/sbin/genestack-aio-setup.sh
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      # All-in-one genestack bring-up, following genestack's own
      # docs/genestack-getting-started.md:
      #   1. clone the repo (with submodules — kubespray lives in one)
      #   2. bootstrap.sh installs ansible/helm + the kubespray provider
      #   3. inventory at /etc/genestack/inventory, then bin/install-*.sh
      # The console's genestack.host_prepare / genestack.deploy operations
      # drive the same steps as jobs against the registered environment.
      set -euo pipefail
      if [ ! -d /opt/genestack/.git ]; then
        sudo git clone --recurse-submodules -j4 https://github.com/rackerlabs/genestack.git /opt/genestack
      fi
      sudo -E /opt/genestack/bootstrap.sh
      echo "AIO bootstrap done. Next: inventory under /etc/genestack/inventory,"
      echo "then deploy via the console or /opt/genestack/bin/install-*.sh."
  - path: /etc/motd
    content: |
      genestack all-in-one dev VM (provisioned by genestack-console install.sh)
        * full AIO setup:  sudo /usr/local/sbin/genestack-aio-setup.sh
        * genestack docs:  https://github.com/rackerlabs/genestack (docs/)
runcmd:
  - systemctl disable --now unattended-upgrades || true
EOF
    cat > "$vm_dir/meta-data" <<EOF
instance-id: iid-${AIO_NAME}-01
local-hostname: ${AIO_NAME}
EOF
    make_seed_iso "$vm_dir/user-data" "$vm_dir/meta-data" "$vm_dir/seed.iso"
    ok "cloud-init seed created"
  fi
}

make_seed_iso() {
  # make_seed_iso <user-data> <meta-data> <out.iso> — CIDATA volume for cloud-init.
  local user_data="$1" meta_data="$2" out="$3" stage
  if command -v cloud-localds >/dev/null 2>&1; then
    cloud-localds "$out" "$user_data" "$meta_data"
    return 0
  fi
  stage="$(mktemp -d "${TMPDIR:-/tmp}/gsc-cidata.XXXXXX")"
  cp "$user_data" "$stage/user-data"
  cp "$meta_data" "$stage/meta-data"
  if command -v hdiutil >/dev/null 2>&1; then
    hdiutil makehybrid -iso -joliet -default-volume-name CIDATA -o "$out" "$stage" >/dev/null
    rm -rf "$stage"
    return 0
  fi
  if command -v genisoimage >/dev/null 2>&1; then
    genisoimage -output "$out" -volid CIDATA -joliet -rock "$stage" >/dev/null
    rm -rf "$stage"
    return 0
  fi
  if command -v mkisofs >/dev/null 2>&1; then
    mkisofs -output "$out" -volid CIDATA -joliet -rock "$stage" >/dev/null
    rm -rf "$stage"
    return 0
  fi
  rm -rf "$stage"
  die "cannot build cloud-init ISO (need cloud-localds, hdiutil, genisoimage, or mkisofs)"
}

aio_firmware() {
  # UEFI firmware for qemu-system-aarch64 (Apple Silicon).
  local p prefix
  prefix="$(command -v brew >/dev/null 2>&1 && brew --prefix qemu 2>/dev/null || true)"
  for p in \
    ${prefix:+"$prefix/share/qemu/edk2-aarch64-code.fd"} \
    /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
    /usr/local/share/qemu/edk2-aarch64-code.fd \
    /usr/share/qemu/edk2-aarch64-code.fd
  do
    [ -n "$p" ] && [ -f "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

pid_alive() {
  # pid_alive <pidfile>
  local pid
  [ -f "$1" ] || return 1
  pid="$(awk 'NR==1{print $1}' "$1" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

aio_launch() {
  local vm_dir="$PREFIX/vms/$AIO_NAME"
  # NOTE: the pidfile lives at the TOP of the hypervisor root
  # ($PREFIX/vms/*.pid) because the console's discovery globs root/*.pid
  # first and pins the workdir from there; serial/disk paths stay absolute.
  local pidfile="$PREFIX/vms/$AIO_NAME.pid"
  if pid_alive "$pidfile"; then
    ok "VM already running (pidfile $pidfile)"
    return 0
  fi
  rm -f "$pidfile"
  local fw=""
  if [ "$OS_KERNEL" = "Darwin" ] && { [ "$OS_ARCH" = "arm64" ] || [ "$OS_ARCH" = "aarch64" ]; }; then
    fw="$(aio_firmware)" || die "UEFI firmware not found (edk2-aarch64-code.fd). brew install qemu"
  fi
  (
    cd "$vm_dir"
    if [ "$OS_KERNEL" = "Darwin" ] && { [ "$OS_ARCH" = "arm64" ] || [ "$OS_ARCH" = "aarch64" ]; }; then
      qemu-system-aarch64 \
        -name "guest=$AIO_NAME,debug-threads=on" \
        -machine virt,accel=hvf \
        -cpu host \
        -smp "$AIO_CPUS" \
        -m "$AIO_MEM_MB" \
        -drive "if=pflash,format=raw,readonly=on,file=$fw" \
        -drive "file=$vm_dir/$AIO_NAME.qcow2,if=virtio,format=qcow2" \
        -drive "file=$vm_dir/seed.iso,if=virtio,format=raw,readonly=on" \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$AIO_SSH_PORT-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -serial "file:$vm_dir/serial.log" \
        -pidfile "$pidfile" \
        -daemonize
    elif [ "$OS_KERNEL" = "Darwin" ]; then
      qemu-system-x86_64 \
        -name "guest=$AIO_NAME,debug-threads=on" \
        -machine q35,accel=hvf \
        -cpu host \
        -smp "$AIO_CPUS" \
        -m "$AIO_MEM_MB" \
        -drive "file=$vm_dir/$AIO_NAME.qcow2,if=virtio,format=qcow2" \
        -drive "file=$vm_dir/seed.iso,if=ide,format=raw,media=cdrom,readonly=on" \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$AIO_SSH_PORT-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -serial "file:$vm_dir/serial.log" \
        -pidfile "$pidfile" \
        -daemonize
    else
      qemu-system-x86_64 \
        -name "guest=$AIO_NAME,debug-threads=on" \
        -machine pc,accel=kvm \
        -cpu host \
        -smp "$AIO_CPUS" \
        -m "$AIO_MEM_MB" \
        -drive "file=$vm_dir/$AIO_NAME.qcow2,if=virtio,format=qcow2" \
        -drive "file=$vm_dir/seed.iso,if=ide,format=raw,media=cdrom,readonly=on" \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$AIO_SSH_PORT-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -serial "file:$vm_dir/serial.log" \
        -pidfile "$pidfile" \
        -daemonize
    fi
  )
  pid_alive "$pidfile" || die "qemu did not start — check $vm_dir/serial.log"
  ok "VM '$AIO_NAME' running (${AIO_CPUS} vCPU, ${AIO_MEM_MB} MiB, ssh on 127.0.0.1:${AIO_SSH_PORT})"
}

aio_register() {
  local base="http://127.0.0.1:${PORT}"
  local key
  key="$(admin_key_from_config)"
  local payload
  payload="$(jq -n \
    --arg name "$AIO_NAME" \
    --arg desc "All-in-one genestack dev VM (QEMU on this host). Inside the VM: sudo /usr/local/sbin/genestack-aio-setup.sh" \
    --arg ssh "ssh -i $PREFIX/ssh/${AIO_NAME}_key -p $AIO_SSH_PORT ubuntu@127.0.0.1" \
    --argjson ssh_port "$AIO_SSH_PORT" \
    --arg serial "$PREFIX/vms/$AIO_NAME/serial.log" \
    '{
      name: $name,
      tier: "dev",
      region: "local",
      description: $desc,
      genestack_path: "/opt/genestack",
      dry_run: true,
      metadata: {
        provider: "qemu-aio",
        vm_name: $name,
        hypervisor: "local qemu",
        ssh: $ssh,
        ssh_port: $ssh_port,
        serial_log: $serial
      }
    }')"

  if [ "$GSC_SKIP_ENGINE" -eq 1 ] || ! curl -fsS "$base/health" >/dev/null 2>&1; then
    warn "console not reachable — register the environment later with:"
    info "curl -fsS -X POST -H 'X-API-Key: <admin-key>' -H 'Content-Type: application/json' \\"
    info "  -d '$payload' $base/api/v1/environments"
    return 0
  fi

  if curl -fsS -H "X-API-Key: $key" "$base/api/v1/environments" 2>/dev/null \
    | jq -e --arg n "$AIO_NAME" 'any(.[]; .name == $n)' >/dev/null 2>&1; then
    ok "environment '$AIO_NAME' already registered"
    return 0
  fi

  local http_code
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' \
    -X POST -H "X-API-Key: $key" -H 'Content-Type: application/json' \
    -d "$payload" "$base/api/v1/environments")"
  if [ "$http_code" = "200" ] || [ "$http_code" = "201" ] || [ "$http_code" = "409" ]; then
    ok "environment '$AIO_NAME' registered in the console (HTTP $http_code)"
  else
    warn "environment registration returned HTTP $http_code — register manually later"
  fi
}

aio_vm() {
  phase "Phase 8/8: all-in-one dev VM"
  if ! confirm "Create the AIO dev VM now? (downloads a cloud image on first run; uses ${AIO_CPUS} vCPU / ${AIO_MEM_MB} MiB RAM)"; then
    warn "skipping AIO VM creation"
    return 0
  fi
  aio_prepare
  aio_launch
  aio_register
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall() {
  phase "Uninstall genestack-console from $PREFIX"
  if [ ! -d "$PREFIX" ]; then
    ok "nothing to do — $PREFIX does not exist"
    return 0
  fi
  if ! confirm "Remove $PREFIX, its containers, systemd unit, and any AIO VM?"; then
    die "aborted"
  fi

  # Stop the AIO VM if running.
  local pidfile="$PREFIX/vms/$AIO_NAME.pid" pid
  if pid_alive "$pidfile"; then
    pid="$(awk 'NR==1{print $1}' "$pidfile")"
    info "stopping AIO VM (pid $pid)"
    kill "$pid" 2>/dev/null || true
    sleep 2
    kill -9 "$pid" 2>/dev/null || true
  fi

  # Stop containers.
  detect_engine
  if [ "$ENGINE" = "docker" ] && [ "${#COMPOSE[@]}" -gt 0 ] && [ -f "$PREFIX/docker-compose.yml" ]; then
    local down_args=()
    # Drop the named Postgres volume too when the sidecar was enabled.
    [ "$WITH_POSTGRES" -eq 1 ] && down_args=(--volumes)
    (cd "$PREFIX" && "${COMPOSE[@]}" down "${down_args[@]}") || true
  elif [ "$ENGINE" = "podman" ]; then
    podman rm -f genestack-console genestack-console-worker genestack-console-pxe \
      >/dev/null 2>&1 || true
  fi

  if [ -f "$HOME/Library/LaunchAgents/dev.genestack.console.plist" ]; then
    launchctl bootout "gui/$(id -u)/dev.genestack.console" >/dev/null 2>&1 || \
      launchctl unload "$HOME/Library/LaunchAgents/dev.genestack.console.plist" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/dev.genestack.console.plist"
    ok "launchd agent removed"
  fi

  # Remove systemd units.
  if command -v systemctl >/dev/null 2>&1; then
    if [ -f /etc/systemd/system/genestack-console.service ]; then
      need_root_or_die "removing the system unit"
      as_root systemctl disable --now genestack-console.service >/dev/null 2>&1 || true
      as_root systemctl disable --now genestack-console-worker.service >/dev/null 2>&1 || true
      as_root systemctl disable --now genestack-console-pxe.service >/dev/null 2>&1 || true
      as_root rm -f /etc/systemd/system/genestack-console.service \
        /etc/systemd/system/genestack-console-worker.service \
        /etc/systemd/system/genestack-console-pxe.service \
        /etc/systemd/system/genestack-console-loopback.service
      as_root systemctl daemon-reload
      ok "system unit removed"
    fi
    if [ -f "$HOME/.config/systemd/user/genestack-console.service" ]; then
      systemctl --user disable --now genestack-console.service >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/genestack-console.service"
      systemctl --user daemon-reload || true
      ok "user unit removed"
    fi
  fi

  # The prefix may contain root/container-owned files (the container runs
  # as uid 1000 and owns data/); try plain removal first, escalate on failure.
  if ! rm -rf "${PREFIX:?}/" 2>/dev/null; then
    need_root_or_die "removing $PREFIX (contains root/container-owned files)"
    as_root rm -rf "${PREFIX:?}/"
  fi
  ok "removed $PREFIX — uninstall complete"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
summary() {
  phase "Install complete"
  local dry_note="Set dry_run: false in config.yaml before Deploy will touch servers."
  if [ -f "$PREFIX/config.yaml" ] && grep -Eq '^dry_run:[[:space:]]*true([[:space:]]|$)' "$PREFIX/config.yaml"; then
    dry_note="dry_run is true — Deploy will not touch servers until you set dry_run: false."
  elif [ -f "$PREFIX/config.yaml" ] && grep -Eq '^dry_run:[[:space:]]*false([[:space:]]|$)' "$PREFIX/config.yaml"; then
    dry_note="dry_run is false — Deploy can touch servers."
  fi
  box \
    "Genestack Console is installed at ${PREFIX}" \
    "" \
    "  UI:     http://127.0.0.1:${PORT}/ui" \
    "  Health: http://127.0.0.1:${PORT}/health" \
    "  Config: ${PREFIX}/config.yaml" \
    "  Creds:  ${PREFIX}/ADMIN_CREDENTIALS.txt (mode 600)" \
    "" \
    "Next: Open Guided setup in the UI" \
    "${dry_note}" \
    "" \
    "Reach the UI from another host via SSH tunnel or VPN:" \
    "  ssh -L ${PORT}:127.0.0.1:${PORT} <this-host>" \
    "The console binds loopback only." \
    "" \
    "Docs: docs/install.md — Linux/WSL2 binary, macOS/Windows Docker, --dev for the AIO VM"
  if [ "$HOST_KIND" = "darwin" ] || [ "$HOST_KIND" = "windows" ]; then
    info "${HOST_KIND}: Console is the Docker image; start Docker Desktop if the UI is down."
    info "Tenant demo / environment walkthrough is seeded for the UI."
  fi
  if [ "$HOST_KIND" = "wsl" ]; then
    info "WSL2: Linux binary. From Windows, browse http://127.0.0.1:${PORT}/ui"
  fi
  if [ "$WITH_AIO" -eq 1 ] && pid_alive "$PREFIX/vms/$AIO_NAME.pid"; then
    info "AIO VM: ssh -i $PREFIX/ssh/${AIO_NAME}_key -p $AIO_SSH_PORT ubuntu@127.0.0.1"
    info "It appears on the console Hosts page and as the '$AIO_NAME' environment."
  fi
}

main() {
  parse_args "$@"
  setup_sudo
  if [ "$DO_UNINSTALL" -eq 1 ]; then
    uninstall
    return 0
  fi
  if [ "$DO_UPDATE" -eq 1 ]; then
    do_update
    return $?
  fi
  preflight
  install_deps
  resolve_image
  ensure_genestack_root
  render_config
  render_compose
  install_systemd
  install_update_timer
  launch_console
  bootstrap
  if [ "$WITH_AIO" -eq 1 ]; then
    aio_vm
  fi
  harden_secret_perms
  summary
}

main "$@"
