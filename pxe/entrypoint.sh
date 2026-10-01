#!/bin/sh
# Entrypoint for the Genestack Console PXE sidecar.
#
# Starts:
#   1. dnsmasq (foreground) — authoritative DHCP + PXE chainload on the
#      provisioning network, configured from /srv/pxe/dnsmasq.conf, which the
#      console renders (app/services/pxe.py) into the shared volume.
#   2. busybox httpd on :8080 serving /srv/pxe — the Talos boot assets and
#      boot.ipxe. busybox httpd ships with alpine's busybox (no extra apk
#      package, no python runtime) and static asset serving needs nothing
#      more; keep it dumb on purpose.
set -eu

PXE_ROOT="${PXE_ROOT:-/srv/pxe}"
CONF="$PXE_ROOT/dnsmasq.conf"
HTTP_PORT="${PXE_HTTP_PORT:-8080}"

# dnsmasq needs a config: refuse to DHCP with nothing rendered. The console
# writes dnsmasq.conf before this sidecar is expected to answer clients.
if [ ! -f "$CONF" ]; then
  echo "pxe sidecar: $CONF not found — the console renders it on first provision" >&2
  echo "pxe sidecar: sleeping; restart the container after the console writes it" >&2
  exec sleep infinity
fi

# dnsmasq reads its config from /etc/dnsmasq.d/pxe.conf; link the rendered
# file from the shared volume there.
mkdir -p /etc/dnsmasq.d
ln -sf "$CONF" /etc/dnsmasq.d/pxe.conf

# httpd in the background, dnsmasq (-k = foreground) as the supervising
# process so the container dies if the DHCP server dies.
httpd -f -p "$HTTP_PORT" -h "$PXE_ROOT" &
exec dnsmasq -k --conf-file=/etc/dnsmasq.d/pxe.conf
