#!/usr/bin/env bash
# Keep libvirt's cgroup placement without depending on a systemd host bus.
set -euo pipefail
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
script=$(yq -r 'select(.kind == "ConfigMap" and .metadata.name == "libvirt-libvirt-default-bin") | .data."libvirt.sh"' "$tmp")
# Match the chart's literal shell expression, not this process's environment.
# shellcheck disable=SC2016
marker='cgexec -g ${CGROUPS%,}:/osh-libvirt systemd-run --scope --slice=system libvirtd --listen'
if [[ $(grep -Fc "$marker" <<< "$script") != 2 ]]; then
    echo 'Unrecognized libvirt startup layout; review Talos compatibility' >&2
    exit 1
fi
yq '(select(.kind == "ConfigMap" and (.metadata.name == "libvirt-bin" or .metadata.name == "libvirt-libvirt-default-bin")) | .data."libvirt.sh") |= sub("systemd-run --scope --slice=system libvirtd --listen"; "libvirtd --listen")' "$tmp" |
    bash "${GENESTACK_OVERRIDES_DIR:?}/kustomize/kustomize.sh" "$@"
