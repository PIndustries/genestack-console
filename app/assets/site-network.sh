#!/usr/bin/env bash
# Select cluster underlay inputs independently from an explicit provider port.
# This helper never attaches, bridges or reconfigures a host interface.
genestack_site_network() {
  case "${GENESTACK_NETWORK_INTERFACES:-manual}" in
    automatic)
      if [ -n "${CONTAINER_INTERFACE:-}${CONTAINER_VLAN_INTERFACE:-}${COMPUTE_INTERFACE:-}" ]; then
        echo "[ERROR] Automatic interfaces cannot be combined with explicit interface overrides"
        return 1
      fi
      export CONTAINER_INTERFACE="" CONTAINER_VLAN_INTERFACE="" COMPUTE_INTERFACE=""
      echo "Using cluster node IPs to select the pod-network interfaces"
      ;;
    manual)
      if [ -z "${CONTAINER_INTERFACE:-}" ]; then
        CONTAINER_INTERFACE=$(ip -details -json link show | \
          jq -r '[.[] | if .linkinfo.info_kind // .link_type == "loopback" or
                 (.ifname | test("idrac+")) then empty else .ifname end ] | .[0]')
        export CONTAINER_INTERFACE
        echo "[WARNING] The interface for the OVN network is required."
        echo "          The script will use the default route interface ${CONTAINER_INTERFACE:-}"
      fi

      if [ -z "${CONTAINER_VLAN_INTERFACE:-}" ]; then
        echo "[WARNING] The vlan interface for the OVN network is required."
        echo "          The script will use the default route interface ${CONTAINER_INTERFACE:-}"
        export CONTAINER_VLAN_INTERFACE="${CONTAINER_INTERFACE:-}"
      fi

      if [ -z "${COMPUTE_INTERFACE:-}" ]; then
        COMPUTE_INTERFACE=$(ip -details -json link show | \
          jq -r '[.[] | if .linkinfo.info_kind // .link_type == "loopback" or
                 (.ifname | test("idrac+")) then empty else .ifname end ] | .[-1]')
        export COMPUTE_INTERFACE
        echo "[WARNING] The interface for the compute network is required."
        echo "          The script will use the last interface found ${COMPUTE_INTERFACE:-}"
      fi

      ;;
    *) echo "[ERROR] GENESTACK_NETWORK_INTERFACES must be automatic or manual"; return 1 ;;
  esac
  if [ -n "${OVN_EXTERNAL_VLAN_INTERFACE:-}${OVN_EXTERNAL_VLAN_PARENT:-}${OVN_EXTERNAL_VLAN_ID:-}${OVN_EXTERNAL_VLAN_MTU:-}" ]; then
    for required_var in OVN_EXTERNAL_VLAN_INTERFACE OVN_EXTERNAL_VLAN_PARENT OVN_EXTERNAL_VLAN_ID OVN_EXTERNAL_VLAN_MTU; do
      if [ -z "${!required_var:-}" ]; then
        echo "[ERROR] ${required_var} must be set when configuring OVN-created VLAN interfaces"
        return 1
      fi
    done

    export OVN_EXTERNAL_INTERFACE="${OVN_EXTERNAL_VLAN_INTERFACE}"
    VLAN_SPEC="${OVN_EXTERNAL_VLAN_INTERFACE}:${OVN_EXTERNAL_VLAN_PARENT}:${OVN_EXTERNAL_VLAN_ID}:${OVN_EXTERNAL_VLAN_MTU}"
    if [ -n "${OVN_VLANS:-}" ]; then
      export OVN_VLANS="${OVN_VLANS},${VLAN_SPEC}"
    else
      export OVN_VLANS="${VLAN_SPEC}"
    fi
  fi

  if [ "${GENESTACK_NETWORK_INTERFACES:-manual}" = manual ] && [ "${COMPUTE_INTERFACE}" = "${CONTAINER_INTERFACE}" ]; then
    echo "[ERROR] The compute interface cannot be the same as the container interface"
    return 1
  fi
  # A compute/default-route NIC is never an implicit external bridge port.
  export OVN_EXTERNAL_INTERFACE="${OVN_EXTERNAL_INTERFACE:-}"
  if [ -z "$OVN_EXTERNAL_INTERFACE" ]; then
    echo "No provider port selected; the external bridge has no physical uplink"
  fi
}

# Keep repeated setup runs from retaining an old physical uplink.
genestack_annotate_provider_ports() {
  if [ -n "${OVN_EXTERNAL_INTERFACE:-}" ]; then
    kubectl annotate --overwrite nodes \
      -l openstack-compute-node=enabled -l openstack-network-node=enabled \
      "ovn.openstack.org/ports=br-ex:${OVN_EXTERNAL_INTERFACE}"
  else
    kubectl annotate nodes \
      -l openstack-compute-node=enabled -l openstack-network-node=enabled \
      ovn.openstack.org/ports-
  fi
}
