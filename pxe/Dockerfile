# Genestack Console PXE/DHCP sidecar.
#
# Runs dnsmasq (authoritative DHCP + PXE chainload on the provisioning
# network) and a tiny static HTTP server (busybox httpd on :8080) serving
# /srv/pxe — the Talos boot assets (assets/vmlinuz, assets/initramfs.xz) and
# the rendered boot.ipxe the console writes into the shared volume.
#
# IMPORTANT: this container MUST run with network_mode: host. DHCP is an L2
# broadcast protocol; bridge/NAT networking cannot see client DHCPDISCOVERs.
# The console host must sit on (L2-reach) the provisioning network.
#
# Config + assets arrive via the mounted /srv/pxe volume:
#   /srv/pxe/dnsmasq.conf   rendered by app/services/pxe.py (console)
#   /srv/pxe/boot.ipxe      iPXE script chainloaded by PXE clients
#   /srv/pxe/assets/        talos kernel + initramfs
# After the console rewrites dnsmasq.conf, restart this container
# (`docker restart genestack-console-pxe`) — dnsmasq does not reload DHCP
# config on SIGHUP.
FROM alpine:3.21

RUN apk add --no-cache dnsmasq

# 67/udp: DHCP, 8080/tcp: boot asset HTTP server.
EXPOSE 67/udp 8080/tcp

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
