#!/usr/bin/env bash
# test-install.sh — self-check harness for scripts/genestack-console.sh.
#
# Runs the installer in its safest modes against a scratch prefix and asserts
# the things that can be asserted on this host; everything that needs a
# container engine / KVM / fresh machine degrades to a clear SKIP, never a
# false failure.
#
# Usage:
#   scripts/test-install.sh                 # static + file-level checks
#   GSC_TEST_WITH_AIO=1 scripts/test-install.sh
#                                           # additionally launch a real AIO VM
#                                           # (needs /dev/kvm, qemu, cloud-localds;
#                                           #  set GSC_AIO_IMAGE_PATH to a local
#                                           #  base image to skip the download)
#
# Env:
#   GSC_TEST_PREFIX   scratch prefix (default /tmp/gsc-test)
#   GSC_TEST_PORT     scratch port  (default 18099)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$HERE/genestack-console.sh"
AGENT_INSTALLER="$HERE/../agent/install.sh"
PREFIX="${GSC_TEST_PREFIX:-/tmp/gsc-test}"
PORT="${GSC_TEST_PORT:-18099}"
# Keep the PATH wrapper inside the scratch tree.
export GSC_CLI_LINK="${PREFIX}-cli/genestack-console"
WITH_AIO="${GSC_TEST_WITH_AIO:-0}"

PASS=0
FAIL=0
SKIP=0

ok()   { PASS=$((PASS + 1)); printf '  \033[1;32mPASS\033[0m %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; }
skip() { SKIP=$((SKIP + 1)); printf '  \033[1;33mSKIP\033[0m %s\n' "$*"; }
hdr()  { printf '\n== %s\n' "$*"; }

assert_file() {
  if [ -f "$1" ]; then ok "file exists: $1"; else bad "missing file: $1"; fi
}

assert_grep() {
  # assert_grep <pattern> <file> <label>
  if [ -f "$2" ] && grep -q "$1" "$2"; then
    ok "$3"
  else
    bad "$3 (pattern '$1' not found in $2)"
  fi
}

assert_not_grep() {
  if [ -f "$2" ] && grep -q "$1" "$2"; then
    bad "$3 (forbidden pattern '$1' found in $2)"
  else
    ok "$3"
  fi
}

file_mode() {
  # Portable mode bits (600/644/…) for Darwin stat -f and GNU stat -c.
  if [ ! -e "$1" ]; then
    echo ""
    return 1
  fi
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

assert_mode_600() {
  local mode
  mode="$(file_mode "$1" || true)"
  if [ "$mode" = "600" ]; then
    ok "$(basename "$1") mode 0600"
  else
    bad "$(basename "$1") must be mode 0600 (got ${mode:-missing})"
  fi
}

cleanup() {
  # Best-effort teardown of anything the harness may have started.
  if [ -f "$PREFIX/vms/genestack-aio.pid" ]; then
    local pid
    pid="$(awk 'NR==1{print $1}' "$PREFIX/vms/genestack-aio.pid" 2>/dev/null || true)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  fi
  if command -v docker >/dev/null 2>&1 && [ -f "$PREFIX/docker-compose.yml" ]; then
    (cd "$PREFIX" && docker compose down) >/dev/null 2>&1 || true
  fi
  if command -v podman >/dev/null 2>&1; then
    podman rm -f genestack-console genestack-console-worker genestack-console-pxe \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$PREFIX"
  rm -f "${STUB_BIN:-}"
}
trap cleanup EXIT

# Fresh scratch prefix for a deterministic run.
rm -rf "$PREFIX"

# ---------------------------------------------------------------------------
hdr "static checks"
# ---------------------------------------------------------------------------
if bash -n "$INSTALLER"; then ok "bash -n genestack-console.sh"; else bad "bash -n install.sh"; fi
if bash -n "$HERE/test-install.sh"; then ok "bash -n test-install.sh"; else bad "bash -n test-install.sh"; fi
if bash -n "$AGENT_INSTALLER"; then ok "bash -n agent/install.sh"; else bad "bash -n agent/install.sh"; fi

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S warning "$INSTALLER"; then
    ok "shellcheck genestack-console.sh"
  else
    bad "shellcheck genestack-console.sh"
  fi
  if shellcheck -S warning "$AGENT_INSTALLER"; then
    ok "shellcheck agent/install.sh"
  else
    bad "shellcheck agent/install.sh"
  fi
else
  skip "shellcheck not installed"
fi

if "$INSTALLER" --help | grep -q -- '--with-aio-vm'; then
  ok "--help documents --with-aio-vm"
else
  bad "--help output"
fi
if "$INSTALLER" --help | grep -q -- '--dev'; then
  ok "--help documents --dev (laptop AIO)"
else
  bad "--help missing --dev"
fi
if "$INSTALLER" --help | grep -q 'macOS'; then
  ok "--help documents macOS / Docker path"
else
  bad "--help should mention macOS"
fi
if "$INSTALLER" --help | grep -q 'Windows'; then
  ok "--help documents Windows / WSL2 / Docker"
else
  bad "--help should mention Windows"
fi
if "$INSTALLER" --help | grep -q 'fleet hub'; then
  ok "--help presents fleet hub as the default"
else
  bad "--help should call out the fleet hub as the default story"
fi
if "$INSTALLER" --help | grep -q -- '--advertise-url'; then
  ok "--help documents --advertise-url"
else
  bad "--help missing --advertise-url"
fi
if "$INSTALLER" --help | grep -q -- '--docker'; then
  ok "--help documents --docker"
else
  bad "--help missing --docker"
fi
if "$INSTALLER" --help | grep -q 'GSC_REPO_REF'; then
  ok "--help documents GSC_REPO_REF"
else
  bad "--help missing GSC_REPO_REF"
fi
if "$INSTALLER" --help | grep -q 'PIndustries/genestack-console'; then
  ok "--help shows the public console repository"
else
  bad "--help should show GSC_REPO_URL=...PIndustries/genestack-console.git"
fi
if "$INSTALLER" --help | grep -q 'genestack-console update'; then
  ok "--help documents genestack-console update"
else
  bad "--help missing genestack-console update"
fi
if "$INSTALLER" --help | grep -q -- '--version'; then
  ok "--help documents --version"
else
  bad "--help missing --version"
fi
if "$INSTALLER" --self-test; then
  ok "--self-test version compare"
else
  bad "--self-test version compare"
fi
if grep -q 'python3' "$INSTALLER"; then
  ok "installer requires python3 (rand_token/fernet_key)"
else
  bad "installer should apt-install/require python3"
fi
if grep -q 'pkg_installed python3' "$INSTALLER"; then
  ok "python3 is an apt dependency"
else
  bad "python3 missing from apt deps"
fi

# Stub ELF so file-layout runs do not fetch the 89MB published binary.
STUB_BIN="$(mktemp)"
printf '\177ELF' > "$STUB_BIN"
chmod +x "$STUB_BIN"
export GSC_BINARY_URL="$STUB_BIN"

# ---------------------------------------------------------------------------
hdr "install run 1 (GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1, scratch prefix)"
# ---------------------------------------------------------------------------
RUN1_LOG="$(mktemp)"
if GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
    --prefix "$PREFIX" --port "$PORT" --non-interactive >"$RUN1_LOG" 2>&1; then
  ok "installer exit 0 (run 1)"
else
  bad "installer exit 0 (run 1) — log: $RUN1_LOG"
fi

assert_file "$PREFIX/config.yaml"
# Secret material must be owner-only after install and re-run.
assert_mode_600 "$PREFIX/config.yaml"
assert_grep 'dev_auto_login: false' "$PREFIX/config.yaml" "config: dev_auto_login false"
assert_not_grep 'dev-admin-key' "$PREFIX/config.yaml" "config: no shipped dev-* keys"
assert_grep 'gsc-admin-' "$PREFIX/config.yaml" "config: rotated admin api key"
assert_grep "^\s*- ${PREFIX}/vms" "$PREFIX/config.yaml" "config: hypervisor root = <prefix>/vms"
assert_grep "http://127.0.0.1:${PORT}" "$PREFIX/config.yaml" "config: CORS pinned to served origin"
assert_grep 'advertise_url:' "$PREFIX/config.yaml" "config: hub.advertise_url present"
assert_grep "root: /opt/genestack" "$PREFIX/config.yaml" "config: genestack.root is the host tree"
assert_grep "data_dir: ${PREFIX}/data" "$PREFIX/config.yaml" "config: absolute data_dir on the prefix"
assert_grep "host: 127.0.0.1" "$PREFIX/config.yaml" "config: server.host is loopback until the operator edits it"
assert_grep "port: ${PORT}" "$PREFIX/config.yaml" "config: server.port from --port"
assert_grep 'Edit these and restart' "$PREFIX/config.yaml" "config: bind is documented as a restart, not a rebuild"
assert_grep 'dry_run: true' "$PREFIX/config.yaml" "config: dry_run true (rehearse until flipped)"
assert_grep 'Jobs rehearse only until you set dry_run: false' "$PREFIX/config.yaml" \
  "config: dry_run safety comment"
assert_grep 'Admin → OVH accounts' "$PREFIX/config.yaml" "config: commented ovh stub"
if grep -q 'all-in-one dev VM' "$RUN1_LOG"; then
  bad "AIO VM must stay off unless --with-aio-vm"
else
  ok "no-AIO default (fleet hub only)"
fi
if [ -f "$PREFIX/docker-compose.yml" ]; then
  bad "binary default must not write a compose file (no source, no container)"
else
  ok "no compose file on the binary default path"
fi
if [ -e "$PREFIX/src" ]; then
  bad "binary default must not install a source tree at $PREFIX/src"
else
  ok "no source tree on the binary default path"
fi
assert_file "$PREFIX/bin/genestack-console" "compiled (stub) binary installed"
assert_file "$PREFIX/systemd/genestack-console.service" "systemd unit generated under prefix"
assert_grep 'genestack-console serve' "$PREFIX/systemd/genestack-console.service" \
  "systemd unit execs the compiled binary"
assert_grep 'GSC_SKIP_ENGINE=1' "$RUN1_LOG" "engine phases skipped with a clear note"
if [ -f "$PREFIX/ADMIN_CREDENTIALS.txt" ]; then
  bad "credentials file should NOT exist when the engine phase is skipped"
else
  ok "no credentials file before the console is running"
fi

CONFIG_SUM_BEFORE="$(sha256sum "$PREFIX/config.yaml" | awk '{print $1}')"

# Plant world-readable secrets, then confirm re-run hardens them.
umask 022
echo leftover > "$PREFIX/config.yaml.bak"
mkdir -p "$PREFIX/data"
echo token > "$PREFIX/data/local-agent-token"
echo db > "$PREFIX/data/console.db"
chmod 0644 "$PREFIX/config.yaml.bak" "$PREFIX/data/local-agent-token" "$PREFIX/data/console.db"

# ---------------------------------------------------------------------------
hdr "install run 2 (idempotent re-run)"
# ---------------------------------------------------------------------------
RUN2_LOG="$(mktemp)"
if GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
    --prefix "$PREFIX" --port "$PORT" --non-interactive >"$RUN2_LOG" 2>&1; then
  ok "installer exit 0 (run 2)"
else
  bad "installer exit 0 (run 2) — log: $RUN2_LOG"
fi
CONFIG_SUM_AFTER="$(sha256sum "$PREFIX/config.yaml" | awk '{print $1}')"
if [ "$CONFIG_SUM_BEFORE" = "$CONFIG_SUM_AFTER" ]; then
  ok "config unchanged on re-run (keys/secrets preserved)"
else
  bad "config changed on re-run — keys would rotate and break clients"
fi
assert_grep 'keeping existing keys' "$RUN2_LOG" "re-run reports config preserved"
for f in "$PREFIX/config.yaml" "$PREFIX/config.yaml.bak" "$PREFIX/data/local-agent-token" "$PREFIX/data/console.db"; do
  assert_mode_600 "$f"
done
assert_grep 'binary already at' "$RUN2_LOG" "re-run reports binary preserved"

# ---------------------------------------------------------------------------
hdr "advertise-url + cors-origin first-run config"
# ---------------------------------------------------------------------------
ADV_PREFIX="${PREFIX}-adv"
rm -rf "$ADV_PREFIX"
if GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
    --prefix "$ADV_PREFIX" --port "$PORT" --non-interactive \
    --advertise-url "https://hub.example.test:8443" \
    --cors-origin "https://ui.example.test" >"$RUN2_LOG" 2>&1; then
  ok "installer exit 0 (--advertise-url / --cors-origin)"
else
  bad "installer exit 0 (--advertise-url / --cors-origin) — log: $RUN2_LOG"
fi
assert_grep 'advertise_url: "https://hub.example.test:8443"' "$ADV_PREFIX/config.yaml" \
  "config: hub.advertise_url from --advertise-url"
assert_grep 'https://ui.example.test' "$ADV_PREFIX/config.yaml" \
  "config: extra CORS origin from --cors-origin"
rm -rf "$ADV_PREFIX"

# ---------------------------------------------------------------------------
hdr "PXE is in-process (GSC_WITH_PXE does not add a sidecar)"
# ---------------------------------------------------------------------------
if [ -f "$PREFIX/docker-compose.yml" ]; then
  bad "binary default must not write a compose file"
else
  ok "compose: no file (PXE is not a sidecar)"
fi

RUN3_LOG="$(mktemp)"
if GSC_WITH_PXE=1 GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
    --prefix "$PREFIX" --port "$PORT" --non-interactive >"$RUN3_LOG" 2>&1; then
  ok "installer exit 0 (GSC_WITH_PXE=1)"
else
  bad "installer exit 0 (GSC_WITH_PXE=1) — log: $RUN3_LOG"
fi
if [ -f "$PREFIX/docker-compose.yml" ]; then
  bad "GSC_WITH_PXE=1 must not write a compose sidecar on the binary path"
else
  ok "GSC_WITH_PXE=1: still no compose file (PXE runs in the binary)"
fi
assert_grep 'in-process' "$RUN3_LOG" "installer says PXE is in-process"
if grep -q 'genestack-console-pxe' "$RUN3_LOG"; then
  bad "installer must not start a pxe sidecar container"
else
  ok "no pxe sidecar container mentioned on the binary path"
fi
if [ -d "$PREFIX/pxe" ]; then ok "pxe data dir created"; else bad "missing dir: $PREFIX/pxe"; fi

# ---------------------------------------------------------------------------
hdr "--docker writes compose without a source tree"
# ---------------------------------------------------------------------------
DOCKER_PREFIX="${PREFIX}-docker"
rm -rf "$DOCKER_PREFIX"
if GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" --docker \
    --prefix "$DOCKER_PREFIX" --port "$PORT" --non-interactive >"$RUN3_LOG" 2>&1; then
  ok "installer exit 0 (--docker)"
else
  bad "installer exit 0 (--docker) — log: $RUN3_LOG"
fi
assert_file "$DOCKER_PREFIX/docker-compose.yml" "--docker writes compose"
assert_not_grep 'context: ./src' "$DOCKER_PREFIX/docker-compose.yml" \
  "--docker compose does not build from source"
assert_grep 'image: genestack-console:stable' "$DOCKER_PREFIX/docker-compose.yml" \
  "--docker compose uses the published image tag"
if [ -e "$DOCKER_PREFIX/src" ]; then
  bad "--docker must not install a source tree"
else
  ok "--docker: no source tree"
fi
rm -rf "$DOCKER_PREFIX"

# ---------------------------------------------------------------------------
hdr "health endpoint (compiled binary)"
# ---------------------------------------------------------------------------
DIST_BIN="$HERE/../dist/genestack-console-linux-amd64"
if [ -x "$DIST_BIN" ]; then
  HEALTH_PREFIX="${PREFIX}-health"
  rm -rf "$HEALTH_PREFIX"
  if GSC_BINARY_URL="$DIST_BIN" GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
      --prefix "$HEALTH_PREFIX" --port "$PORT" --non-interactive >/tmp/gsc-health-install.log 2>&1; then
    ok "installer exit 0 (real binary, skip engine)"
    CONSOLE_CONFIG="$HEALTH_PREFIX/config.yaml" GSC_PREFIX="$HEALTH_PREFIX" \
      "$HEALTH_PREFIX/bin/genestack-console" serve --host 127.0.0.1 --port "$PORT" \
      >/tmp/gsc-health-serve.log 2>&1 &
    health_pid=$!
    health_ok=0
    for _try in $(seq 1 30); do
      if curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; then
        health_ok=1
        break
      fi
      sleep 1
    done
    if [ "$health_ok" -eq 1 ]; then
      ok "GET /health returns ok on :$PORT from compiled binary"
    else
      bad "GET /health on :$PORT from compiled binary"
      tail -20 /tmp/gsc-health-serve.log >&2 || true
    fi
    kill "$health_pid" 2>/dev/null || true
    wait "$health_pid" 2>/dev/null || true
  else
    bad "installer with real binary exited non-zero"
  fi
  rm -rf "$HEALTH_PREFIX"
else
  skip "no dist/genestack-console-linux-amd64 — compile first for live /health"
fi

# ---------------------------------------------------------------------------
hdr "KVM guard"
# ---------------------------------------------------------------------------
if [ -e /dev/kvm ]; then
  skip "/dev/kvm present here — the 'KVM missing' error path needs a host without KVM"
else
  kvm_out="$(GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
      --prefix "$PREFIX" --port "$PORT" --non-interactive --with-aio-vm 2>&1 || true)"
  if printf '%s' "$kvm_out" | grep -q '/dev/kvm not found'; then
    ok "--with-aio-vm without /dev/kvm fails with guidance"
  else
    bad "--with-aio-vm without /dev/kvm should fail with guidance"
  fi
fi

# ---------------------------------------------------------------------------
hdr "live AIO VM (GSC_TEST_WITH_AIO=1)"
# ---------------------------------------------------------------------------
if [ "$WITH_AIO" != "1" ]; then
  skip "set GSC_TEST_WITH_AIO=1 to launch a real AIO VM under the scratch prefix"
elif [ ! -e /dev/kvm ] || ! command -v qemu-system-x86_64 >/dev/null 2>&1 || ! command -v cloud-localds >/dev/null 2>&1; then
  skip "missing /dev/kvm, qemu, or cloud-localds for the live AIO test"
else
  if GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1 "$INSTALLER" \
      --prefix "$PREFIX" --port "$PORT" --non-interactive --with-aio-vm; then
    ok "installer --with-aio-vm exit 0"
  else
    bad "installer --with-aio-vm exit 0"
  fi
  assert_file "$PREFIX/vms/genestack-aio/genestack-aio.qcow2"
  assert_file "$PREFIX/vms/genestack-aio/seed.iso"
  assert_file "$PREFIX/ssh/genestack-aio_key"
  assert_file "$PREFIX/ssh/genestack-aio_key.pub"
  if [ -f "$PREFIX/vms/genestack-aio.pid" ]; then
    pid="$(awk 'NR==1{print $1}' "$PREFIX/vms/genestack-aio.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      ok "AIO VM qemu process running (pid $pid)"
    else
      bad "AIO VM pidfile pid $pid not alive"
    fi
  else
    bad "AIO VM pidfile missing at the top of the hypervisor root"
  fi
  assert_file "$PREFIX/vms/genestack-aio/serial.log"
fi

# ---------------------------------------------------------------------------
hdr "update command"
# ---------------------------------------------------------------------------
UPD="$(mktemp -d)"
UPD_LOG="$(mktemp)"
if "$INSTALLER" --prefix "$UPD" update >"$UPD_LOG" 2>&1; then
  bad "update with no binary should fail"
else
  if grep -q 'unknown argument' "$UPD_LOG"; then
    bad "positional update was rejected as an unknown argument"
  else
    ok "positional update is a command"
  fi
fi
rm -rf "$UPD"

plant_console() {
  # plant_console PREFIX VERSION
  local root="$1" ver="$2"
  mkdir -p "$root/bin"
  cat > "$root/bin/genestack-console" <<EOF
#!/bin/sh
# MARKER-${ver}
echo "genestack-console ${ver}"
EOF
  chmod 0755 "$root/bin/genestack-console"
}

UPD_SAME="$(mktemp -d)"
SAME_JSON="$(mktemp)"
printf '{"version":"2026.10.04.3","binary":"%s"}\n' "$STUB_BIN" > "$SAME_JSON"
plant_console "$UPD_SAME" "2026.10.04.3"
cat > "$UPD_SAME/config.yaml" <<EOF
update:
  url: ${SAME_JSON}
sentinel: keep-me
EOF
SAME_LOG="$(mktemp)"
if "$INSTALLER" --prefix "$UPD_SAME" update >"$SAME_LOG" 2>&1; then
  ok "update exit 0 when already current"
else
  bad "update exit 0 when already current — log: $SAME_LOG"
fi
if grep -q 'already current' "$SAME_LOG" && grep -q 'MARKER-2026.10.04.3' "$UPD_SAME/bin/genestack-console"; then
  ok "already current does not replace the binary"
else
  bad "already current should keep the binary"
fi
if grep -q 'sentinel: keep-me' "$UPD_SAME/config.yaml"; then
  ok "update leaves config.yaml alone"
else
  bad "update rewrote config.yaml"
fi
rm -rf "$UPD_SAME"
rm -f "$SAME_JSON"

UPD_NEW="$(mktemp -d)"
NEW_JSON="$(mktemp)"
printf '{"version":"2026.10.04.3","binary":"%s"}\n' "$STUB_BIN" > "$NEW_JSON"
plant_console "$UPD_NEW" "2026.10.04.2"
cat > "$UPD_NEW/config.yaml" <<EOF
update:
  url: ${NEW_JSON}
EOF
NEW_LOG="$(mktemp)"
if "$INSTALLER" --prefix "$UPD_NEW" --check update >"$NEW_LOG" 2>&1; then
  ok "update --check exit 0"
else
  bad "update --check exit 0 — log: $NEW_LOG"
fi
if grep -q 'did not replace' "$NEW_LOG" && grep -q 'MARKER-2026.10.04.2' "$UPD_NEW/bin/genestack-console"; then
  ok "--check prints the newer version and does not replace the binary"
else
  bad "--check should not replace the binary"
fi
if "$INSTALLER" --prefix "$UPD_NEW" update >"$NEW_LOG" 2>&1; then
  ok "update exit 0 when a newer build is published"
else
  bad "update exit 0 when a newer build is published — log: $NEW_LOG"
fi
NEW_MAGIC="$(od -An -tx1 -N4 "$UPD_NEW/bin/genestack-console" 2>/dev/null | tr -d ' \n' || true)"
if [ "$NEW_MAGIC" = "7f454c46" ] && grep -q '2026.10.04.2' "$NEW_LOG" && grep -q '2026.10.04.3' "$NEW_LOG"; then
  ok "update replaced the binary with the published ELF"
else
  bad "update did not install the newer ELF — log: $NEW_LOG"
fi
if [ -f "$GSC_CLI_LINK" ] && grep -q 'genestack-console-wrapper' "$GSC_CLI_LINK" && grep -q 'update' "$GSC_CLI_LINK"; then
  ok "PATH wrapper offers genestack-console update"
else
  bad "PATH wrapper missing at $GSC_CLI_LINK"
fi
rm -rf "$UPD_NEW"
rm -f "$NEW_JSON"

# A named release, including an older one, and a repeat install that follows latest.
REL_BASE="$(mktemp -d)"
mkdir -p "$REL_BASE/v2026.10.04.2" "$REL_BASE/v2026.10.04.4"
printf '{"version":"2026.10.04.2","binary":"%s"}\n' "$STUB_BIN" > "$REL_BASE/v2026.10.04.2/version.json"
printf '{"version":"2026.10.04.4","binary":"%s"}\n' "$STUB_BIN" > "$REL_BASE/v2026.10.04.4/version.json"
PIN_ROOT="$(mktemp -d)"
plant_console "$PIN_ROOT" "2026.10.04.4"
PIN_LOG="$(mktemp)"
if GSC_RELEASE_BASE="$REL_BASE" "$INSTALLER" --prefix "$PIN_ROOT" v2026.10.04.2 --check update >"$PIN_LOG" 2>&1; then
  ok "positional release tag is accepted"
else
  bad "positional release tag was rejected — log: $PIN_LOG"
fi
if ! grep -q 'did not replace' "$PIN_LOG"; then
  bad "positional --check should not replace the binary"
fi
if GSC_RELEASE_BASE="$REL_BASE" "$INSTALLER" --prefix "$PIN_ROOT" --version 2026.10.04.2 update >"$PIN_LOG" 2>&1; then
  ok "update --version exit 0"
else
  bad "update --version exit 0 — log: $PIN_LOG"
fi
PIN_MAGIC="$(od -An -tx1 -N4 "$PIN_ROOT/bin/genestack-console" 2>/dev/null | tr -d ' \n' || true)"
if [ "$PIN_MAGIC" = "7f454c46" ] && grep -q 'selected 2026.10.04.2' "$PIN_LOG"; then
  ok "named release replaces the binary even when it is older"
else
  bad "named release did not install the selected build — log: $PIN_LOG"
fi
rm -rf "$PIN_ROOT"

# Repeat of the default install: upgrade, and do not downgrade, and keep config.
smart_install() {
  local root="$1" log="$2"
  shift 2
  mkdir -p "$root"
  env -u GSC_BINARY_URL \
    HOME="$(mktemp -d)" \
    GSC_FORCE_BINARY=1 \
    GSC_RELEASE_BASE="$REL_BASE" \
    GSC_SKIP_APT=1 \
    GSC_SKIP_ENGINE=1 \
    GSC_CLI_LINK="$root/cli/genestack-console" \
    "$INSTALLER" --prefix "$root" "$@" >"$log" 2>&1
}
UP_ROOT="$(mktemp -d)"
mkdir -p "$UP_ROOT"
plant_console "$UP_ROOT" "2026.10.04.2"
cat > "$UP_ROOT/config.yaml" <<EOF
update:
  url: ${REL_BASE}/v2026.10.04.4/version.json
sentinel: keep-me
EOF
UP_LOG="$(mktemp)"
if smart_install "$UP_ROOT" "$UP_LOG"; then
  ok "repeat install exit 0"
else
  bad "repeat install exit 0 — log: $UP_LOG"
fi
UP_MAGIC="$(od -An -tx1 -N4 "$UP_ROOT/bin/genestack-console" 2>/dev/null | tr -d ' \n' || true)"
if [ "$UP_MAGIC" = "7f454c46" ] && grep -q 'sentinel: keep-me' "$UP_ROOT/config.yaml"; then
  ok "repeat install pulled the newer release and kept config"
else
  bad "repeat install did not pull the newer release — log: $UP_LOG"
fi
# The file is now the stub ELF, which cannot report a version. Plant again
# to prove a plain re-run does not move backward.
plant_console "$UP_ROOT" "2026.10.04.4"
cat > "$UP_ROOT/config.yaml" <<EOF
update:
  url: ${REL_BASE}/v2026.10.04.2/version.json
sentinel: keep-me
EOF
if smart_install "$UP_ROOT" "$UP_LOG"; then
  ok "repeat install exit 0 when already newer"
else
  bad "repeat install exit 0 when already newer — log: $UP_LOG"
fi
if grep -q 'MARKER-2026.10.04.4' "$UP_ROOT/bin/genestack-console" && grep -q 'sentinel: keep-me' "$UP_ROOT/config.yaml"; then
  ok "repeat install does not downgrade"
else
  bad "repeat install downgraded or rewrote config — log: $UP_LOG"
fi
# Naming the older release does install it.
if smart_install "$UP_ROOT" "$UP_LOG" --version 2026.10.04.2; then
  ok "install --version exit 0"
else
  bad "install --version exit 0 — log: $UP_LOG"
fi
UP_MAGIC="$(od -An -tx1 -N4 "$UP_ROOT/bin/genestack-console" 2>/dev/null | tr -d ' \n' || true)"
if [ "$UP_MAGIC" = "7f454c46" ] && grep -q 'sentinel: keep-me' "$UP_ROOT/config.yaml"; then
  ok "install --version selected that release and kept config"
else
  bad "install --version did not select that release — log: $UP_LOG"
fi
rm -rf "$UP_ROOT" "$REL_BASE"
rm -f "$PIN_LOG" "$UP_LOG"

# ---------------------------------------------------------------------------
hdr "uninstall"
# ---------------------------------------------------------------------------
if "$INSTALLER" --prefix "$PREFIX" --non-interactive --uninstall >/dev/null 2>&1; then
  ok "uninstall exit 0"
else
  bad "uninstall exit 0"
fi
if [ ! -e "$PREFIX" ]; then
  ok "prefix removed"
else
  bad "prefix still exists: $PREFIX"
fi
if [ "$WITH_AIO" = "1" ] && [ -n "${pid:-}" ]; then
  if kill -0 "$pid" 2>/dev/null; then
    bad "AIO VM still running after uninstall (pid $pid)"
  else
    ok "AIO VM stopped by uninstall"
  fi
fi

# ---------------------------------------------------------------------------
hdr "summary"
# ---------------------------------------------------------------------------
printf '  PASS=%d FAIL=%d SKIP=%d\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
