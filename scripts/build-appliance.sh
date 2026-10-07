#!/usr/bin/env bash
# Build the bootc appliance from a compiled console binary.
# Writes dist/genestack-console-appliance-<version>-amd64.qcow2.xz
# and dist/genestack-console-appliance-<version>-amd64.iso
#
# Requires podman, so the image builder can see the local bootc image.
#   ./scripts/build-appliance.sh
#   ./scripts/build-appliance.sh dist/genestack-console-linux-amd64
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ELF="${1:-$ROOT/dist/genestack-console-linux-amd64}"
VERSION="$(cd "$ROOT" && python3 -c 'from app.version import VERSION; print(VERSION)')"
OUT_DIR="${GSC_DIST:-$ROOT/dist}"
BASE="${GSC_BOOTC_BASE:-docker.io/library/ubuntu:26.04}"
BOOTC_VERSION="${GSC_BOOTC_VERSION:-v1.16.14}"
BUILDER="${GSC_IMAGE_BUILDER:-ghcr.io/osbuild/image-builder-cli:latest}"
NAME="genestack-console-appliance-${VERSION}-amd64.qcow2"
ISO_NAME="genestack-console-appliance-${VERSION}-amd64.iso"
REF="localhost/genestack-console-appliance:${VERSION}"
ISO_REF="localhost/genestack-console-installer:${VERSION}"

if [ ! -f "$ELF" ]; then
  echo "missing console binary: $ELF" >&2
  echo "run ./scripts/compile-console.sh on Linux x86_64 first" >&2
  exit 1
fi
if ! command -v podman >/dev/null 2>&1; then
  echo "podman is required to build the appliance disk" >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64|amd64) ;;
  *)
    echo "the appliance disk is built on x86_64 (this machine is $(uname -m))" >&2
    exit 1
    ;;
esac

run_priv() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

stage="$(mktemp -d)"
work="$(mktemp -d)"
trap 'rm -rf "$stage" "$work"' EXIT

cp "$ROOT/images/bootc/Containerfile" "$stage/Containerfile"
cp "$ROOT/images/bootc/prepare.sh" "$stage/prepare.sh"
cp "$ROOT/images/bootc/genestack-console.service" "$stage/genestack-console.service"
cp "$ROOT/images/bootc/genestack-console-worker.service" "$stage/genestack-console-worker.service"
cp "$ROOT/images/bootc/genestack-console-prepare.service" "$stage/genestack-console-prepare.service"
cp "$ROOT/images/bootc/10-genestack-cloud.cfg" "$stage/10-genestack-cloud.cfg"
cp "$ELF" "$stage/genestack-console"
chmod 0755 "$stage/prepare.sh" "$stage/genestack-console"

echo "==> bootc image ${REF}"
run_priv podman build \
  --build-arg "BASE=${BASE}" \
  --build-arg "BOOTC_VERSION=${BOOTC_VERSION}" \
  -t "$REF" \
  -f "$stage/Containerfile" \
  "$stage"

echo "==> installer image ${ISO_REF}"
run_priv podman build \
  --build-arg "BASE=${BASE}" \
  --build-arg "APPLIANCE_REF=${REF}" \
  -t "$ISO_REF" \
  -f "$ROOT/images/bootc/iso/Containerfile" \
  "$ROOT/images/bootc/iso"

mkdir -p "$work/output"
echo "==> qcow2"
run_priv podman run \
  --rm \
  --privileged \
  --pull=newer \
  --security-opt label=disable \
  -v "$work/output:/output" \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  "$BUILDER" \
  build \
  --output-dir /output \
  --bootc-ref "$REF" \
  --bootc-default-fs ext4 \
  qcow2

run_priv chown -R "$(id -u):$(id -g)" "$work/output"
disk=""
while IFS= read -r candidate; do
  disk="$candidate"
  break
done < <(find "$work/output" -type f -name '*.qcow2')
if [ -z "$disk" ]; then
  echo "image builder wrote no qcow2 under $work/output" >&2
  find "$work/output" -type f >&2 || true
  exit 1
fi

mkdir -p "$OUT_DIR"
echo "==> compress ${NAME}.xz"
xz -T0 -6 -c "$disk" > "$OUT_DIR/${NAME}.xz"
echo "OK: $OUT_DIR/${NAME}.xz"

mkdir -p "$work/iso"
echo "==> iso"
run_priv podman run \
  --rm \
  --privileged \
  --pull=newer \
  --security-opt label=disable \
  -v "$work/iso:/output" \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  "$BUILDER" \
  build \
  --output-dir /output \
  --bootc-ref "$ISO_REF" \
  --bootc-installer-payload-ref "$REF" \
  bootc-generic-iso

run_priv chown -R "$(id -u):$(id -g)" "$work/iso"
iso=""
while IFS= read -r candidate; do
  iso="$candidate"
  break
done < <(find "$work/iso" -type f -name '*.iso')
if [ -z "$iso" ]; then
  echo "image builder wrote no iso under $work/iso" >&2
  find "$work/iso" -type f >&2 || true
  exit 1
fi
cp "$iso" "$OUT_DIR/${ISO_NAME}"
echo "OK: $OUT_DIR/${ISO_NAME}"
