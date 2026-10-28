"""First-cloud service sizing and private endpoints from the reviewed hardware."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import re

import yaml

from app.paths import package_root
from app.services.service_registry import PIPELINE_STAGES


def _default(tree, path, value):
    for key in path[:-1]:
        tree = tree.setdefault(key, {})
    tree.setdefault(path[-1], value)


def ensure_initial_flavor(cloud, doc):
    """An empty new cloud needs one usable VM choice; preserve existing catalogs."""
    existing = cloud.list_flavors()
    if existing:
        return {"created": False}
    machines = [
        m for m in doc["hardware_installation"]["reviewed_plan"]["machines"]
        if "compute" in m["roles"]
    ]
    if not machines:
        raise ValueError("A first VM size requires reviewed compute hardware")
    machine = max(machines, key=lambda m: (m["memory_gib"], m["cpu_threads"]))
    # Leave capacity for the cloud services and additional guests. This is a
    # starting choice, not a reservation or a change to Nova's allocation ratios.
    ram = min(2048, int(machine["memory_gib"] * 1024 / 8) // 512 * 512)
    cores = min(2, max(1, machine["cpu_threads"] // 8))
    boot = [d["capacity_bytes"] for d in machine["disks"] if d["purpose"] == "boot"]
    disk = min(16, max(boot, default=0) // 1024**3 // 4)
    if ram < 512 or disk < 4:
        raise ValueError("Reviewed compute capacity cannot fit a safe starting VM size")
    flavor = cloud.flavor_create(name="gs.small", vcpus=cores, ram=ram, disk=disk)
    expected = {"id": flavor.get("id"), "name": "gs.small", "vcpus": cores, "ram": ram, "disk": disk}
    if not expected["id"] or not any(
        all(row.get(key) == value for key, value in expected.items())
        for row in cloud.list_flavors()
    ):
        raise ValueError("The starting VM size was not confirmed by the compute API")
    return {"created": True, **expected}


def configure(original, environment_id):
    doc = copy.deepcopy(original)
    machines = doc["hardware_installation"]["reviewed_plan"]["machines"]
    if not machines or any(
        not isinstance(m.get("cpu_threads"), int)
        or isinstance(m.get("cpu_threads"), bool)
        or not isinstance(m.get("memory_gib"), (int, float))
        for m in machines
    ):
        raise ValueError("Cloud services require measured CPU and memory capacity")
    if sum(m["cpu_threads"] for m in machines) < 8 or sum(m["memory_gib"] for m in machines) < 24:
        raise ValueError(
            "Cloud services need at least 8 CPU threads and 24 GiB measured memory capacity"
        )
    controls = [m for m in machines if "control" in m["roles"]]
    if not controls:
        raise ValueError("Cloud services require a reviewed control node")
    replicas = min(3, len(controls))
    quorum = 3 if len(controls) >= 3 else 1
    network = doc.setdefault("network", {})
    if network.get("container_interface") or network.get("compute_interface"):
        raise ValueError("Use advanced deployment for explicit interface overrides")
    ovn = network.get("ovn") or {}
    if any(
        ovn.get(key)
        for key in [
            "external_interface",
            "external_vlan_interface",
            "external_vlan_parent",
            "external_vlan_id",
            "external_vlan_mtu",
        ]
    ):
        raise ValueError("An external provider port requires advanced network review")
    network["interfaces"] = "automatic"
    subnet = ipaddress.IPv4Network(network["cidr"])
    pxe = doc["pxe"]
    low, high = (ipaddress.IPv4Address(pxe[k]) for k in ["range_start", "range_end"])
    private_lan = any(
        subnet.subnet_of(ipaddress.IPv4Network(block))
        for block in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    )
    if not private_lan or low not in subnet or high not in subnet or low > high:
        raise ValueError("Cloud services require the reviewed private provisioning network")
    reserved = {ipaddress.IPv4Address(m["ip"]) for m in machines}
    reserved.update(
        ipaddress.IPv4Address(value)
        for value in [network.get("gateway"), pxe.get("next_server")]
        if value
    )
    prior = doc.get("cloud_services") or {}
    start = (
        ipaddress.IPv4Address(prior["endpoint_ip"])
        if prior.get("endpoint_ip")
        else subnet.network_address + 1
    )
    selected = None
    # Bounded to the reviewed LAN; reserve a contiguous eight-address block.
    for offset in range(min(subnet.num_addresses, 65536)):
        first = start + offset
        values = [first + i for i in range(8)]
        if all(
            value in subnet
            and value not in [subnet.network_address, subnet.broadcast_address]
            and value not in reserved
            and not low <= value <= high
            for value in values
        ):
            selected = values
            break
    if selected is None or (prior and str(selected[0]) != prior.get("endpoint_ip")):
        raise ValueError("No unchanged private endpoint address block is available outside DHCP")
    network.setdefault(
        "gateway_domain",
        "cloud-" + hashlib.sha256(environment_id.encode()).hexdigest()[:10] + ".home.arpa",
    )
    storage = doc["storage"]
    usable = (
        sum(d["capacity_bytes"] for m in machines for d in m["disks"] if d["purpose"] == "storage")
        * 7
        // 10
        // storage["replication_factor"]
        // 1024**3
    )
    cinder = storage["longhorn_cinder"]["capacity_gib"]
    remaining = usable - cinder
    database = min(8, max(2, remaining // (quorum * 4)))
    rabbit = min(4, max(2, remaining // (quorum * 8)))
    glance = min(20, max(4, remaining // 4))
    requested = cinder + quorum * (database + rabbit) + glance + replicas + 6
    if requested > usable:
        raise ValueError("Measured data storage capacity cannot fit the cloud service volumes")
    plan = {
        "schema_version": 1,
        "private_network_only": True,
        "endpoint_ip": str(selected[0]),
        "pool_start": str(selected[1]),
        "pool_end": str(selected[-1]),
        "service_replicas": replicas,
        "quorum_replicas": quorum,
        "cache_gib": 1,
        "database_gib": database,
        "rabbit_gib": rabbit,
        "image_gib": glance,
        "storage_requested_gib": requested,
        "storage_usable_gib": usable,
    }
    # Correcting reserve accounting must not resize retained service volumes.
    if prior and any(
        key not in plan or value != plan[key]
        for key, value in prior.items()
        if key != "storage_requested_gib"
    ):
        raise ValueError(
            "Cloud service sizing changed; review advanced configuration before replacing retained volumes"
        )
    doc["cloud_services"] = plan
    values = doc.setdefault("helm_overrides", {})
    global_values = values.setdefault("global", {})
    operator = values.setdefault("mariadb-operator", {})
    for tree in [
        operator,
        operator.setdefault("webhook", {}),
        operator.setdefault("certController", {}),
    ]:
        _default(tree, ["ha", "enabled"], replicas > 1)
        _default(tree, ["ha", "replicas"], replicas)
    for key in ["api", "conductor", "scheduler", "consoleauth", "metadata", "novncproxy", "server"]:
        _default(global_values, ["pod", "replicas", key], replicas)
    _default(global_values, ["endpoints", "oslo_cache", "statefulset", "replicas"], replicas)
    _default(values.setdefault("glance", {}), ["volume", "size"], str(glance) + "Gi")
    memcache = values.setdefault("memcached", {})
    _default(memcache, ["pod", "replicas", "server"], replicas)
    _default(memcache, ["conf", "memcached", "memory"], 768)
    _default(
        memcache, ["pod", "resources", "server", "requests"], {"cpu": "200m", "memory": "256Mi"}
    )
    _default(memcache, ["pod", "resources", "server", "limits"], {"cpu": "2", "memory": "1536Mi"})
    monitoring = values.setdefault("kube-prometheus-stack", {})
    for name, field in [("prometheus", "storageSpec"), ("alertmanager", "storage")]:
        _default(
            monitoring,
            [name, name + "Spec", field, "volumeClaimTemplate", "spec"],
            {
                "storageClassName": "general",
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "2Gi" if name == "prometheus" else "1Gi"}},
            },
        )
    components = doc.setdefault("components", {})
    needed = {"keystone", "placement", "glance", "nova", "neutron", "libvirt", "cinder"}
    for stage in PIPELINE_STAGES:
        for item in stage["items"]:
            if item["type"] == "service":
                components.setdefault(item["name"], item["name"] in needed)
    return doc, plan


def rendered_files(doc):
    plan = doc.get("cloud_services")
    if not plan:
        return {}
    domain = doc["network"]["gateway_domain"]
    dumps = lambda body: yaml.safe_dump(body, sort_keys=False)
    files = {
        "runtime/" + name: (package_root() / "app/assets" / name).read_text()
        for name in ["site-network.sh", "libvirt-talos-post-render.sh"]
    }
    pools = []
    for name, addresses in [
        ("gateway-api-internal", [plan["endpoint_ip"] + "/32"]),
        ("primary", [plan["pool_start"] + "-" + plan["pool_end"]]),
    ]:
        pools.extend(
            [
                {
                    "apiVersion": "metallb.io/v1beta1",
                    "kind": "IPAddressPool",
                    "metadata": {"name": name, "namespace": "metallb-system"},
                    "spec": {"addresses": addresses, "autoAssign": False},
                },
                {
                    "apiVersion": "metallb.io/v1beta1",
                    "kind": "L2Advertisement",
                    "metadata": {"name": name, "namespace": "metallb-system"},
                    "spec": {"ipAddressPools": [name]},
                },
            ]
        )
    files["manifests/metallb/metallb-openstack-service-lb.yml"] = yaml.safe_dump_all(
        pools, sort_keys=False
    )
    files["envoy-gateways.yaml"] = dumps(
        {
            "domain": domain,
            "acme": {"enabled": False},
            "gateways": {
                "internal": {
                    "namespace": "envoy-gateway",
                    "type": "internal",
                    "domain": domain,
                    "metallb_pool": "gateway-api-internal",
                    "gateway_class": "internal-eg",
                }
            },
            "routes": [
                {"name": name, "exposure": "internal", "section_name": "cluster-tls"}
                for name in ["keystone", "placement", "glance", "nova", "neutron", "cinder"]
            ],
        }
    )
    files["kustomize/mariadb-operator/overlay/kustomization.yaml"] = dumps(
        {
            "resources": ["../base"],
            "patches": [
                {
                    "target": {"group": "apps", "version": "v1", "kind": "Deployment"},
                    "patch": yaml.safe_dump(
                        [
                            {
                                "op": "add",
                                "path": "/spec/strategy",
                                "value": {
                                    "type": "RollingUpdate",
                                    "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                                },
                            }
                        ],
                        sort_keys=False,
                    ),
                }
            ],
        }
    )
    quorum = plan["quorum_replicas"]
    database = {
        "apiVersion": "k8s.mariadb.com/v1alpha1",
        "kind": "MariaDB",
        "metadata": {"name": "mariadb-cluster", "namespace": "openstack"},
        "spec": {
            "replicas": quorum,
            "affinity": {"podAntiAffinity": None},
            "replication": {"enabled": quorum > 1},
            "storage": {
                "size": str(plan["database_gib"]) + "Gi",
                "volumeClaimTemplate": {
                    "resources": {"requests": {"storage": str(plan["database_gib"]) + "Gi"}}
                },
            },
            "service": {"type": "ClusterIP"},
            "primaryService": {"type": "ClusterIP"},
            "secondaryService": {"type": "ClusterIP"},
        },
    }
    rabbit = {
        "apiVersion": "rabbitmq.com/v1beta1",
        "kind": "RabbitmqCluster",
        "metadata": {"name": "rabbitmq", "namespace": "openstack"},
        "spec": {
            "replicas": quorum,
            "affinity": {"podAntiAffinity": None},
            "persistence": {"storage": str(plan["rabbit_gib"]) + "Gi"},
            "override": {"service": {"spec": {"type": "ClusterIP"}}},
        },
    }
    if quorum == 1:
        # All chart clients use the primary DNS name, even on a standalone DB.
        database["spec"].update(
            primaryConnection=None,
            secondaryConnection=None,
            secondaryService=None,
            primaryService=None,
        )
    else:
        database["spec"].pop("affinity")
        rabbit["spec"].pop("affinity")
    for service, patch in [("mariadb-cluster", database), ("rabbitmq-cluster", rabbit)]:
        prefix = "kustomize/" + service + "/overlay/"
        files[prefix + "console-sizing.yaml"] = dumps(patch)
        resources = ["../base"]
        if service == "mariadb-cluster" and quorum == 1:
            resources.append("console-primary.yaml")
            files[prefix + "console-primary.yaml"] = dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "mariadb-cluster-primary", "namespace": "openstack"},
                    "spec": {
                        "selector": {"app.kubernetes.io/instance": "mariadb-cluster"},
                        "ports": [{"name": "mysql", "port": 3306, "targetPort": 3306}],
                    },
                }
            )
        files[prefix + "kustomization.yaml"] = dumps(
            {"resources": resources, "patches": [{"path": "console-sizing.yaml"}]}
        )
    return files


def compatible_infrastructure(source, helper):
    """Adapt only the recognized upstream NIC block; never fetch private code."""
    modern = 'source "${GENESTACK_BASE_DIR:-/opt/genestack}/bin/lib/site-network.sh"'
    if source.count(modern) == 1 and "genestack_annotate_provider_ports" in source:
        if any(c in str(helper) for c in ['"', "$", "`", "\\", "\n"]):
            raise ValueError("Unsafe scoped network helper path")
        return source.replace(modern, 'source "' + str(helper) + '"')
    pattern = r'(?ms)^if \[ -z "\$\{CONTAINER_INTERFACE\}" \]; then\n.*?(?=^kubectl annotate \\\n)'
    blocks = list(re.finditer(pattern, source))
    if len(blocks) != 1 or "COMPUTE_INTERFACE" not in blocks[0].group():
        raise ValueError(
            "Unrecognized infrastructure network setup; update the deployer before continuing"
        )
    if any(c in str(helper) for c in ['"', "$", "`", "\\", "\n"]):
        raise ValueError("Unsafe scoped network helper path")
    source = (
        source[: blocks[0].start()]
        + 'source "'
        + str(helper)
        + '"\ngenestack_site_network\n\n'
        + source[blocks[0].end() :]
    )
    ports = r'(?m)^kubectl annotate \\\n        nodes \\\n        -l openstack-compute-node=enabled -l openstack-network-node=enabled \\\n        ovn.openstack.org/ports="br-ex:\$\{OVN_EXTERNAL_INTERFACE\}"'
    source, count = re.subn(ports, "genestack_annotate_provider_ports", source)
    if count != 1:
        raise ValueError("Unrecognized provider annotation; update the deployer before continuing")
    return source.replace("kubectl annotate \\", "kubectl annotate --overwrite \\")


def finish_rendered_files(doc, files):
    """Keep autoscalers within this cloud and retain other rendered patches."""
    plan = doc.get("cloud_services")
    if not plan:
        return
    replicas = plan["service_replicas"]
    for service in ["keystone", "placement", "glance", "nova", "neutron", "cinder"]:
        key = "kustomize/" + service + "/overlay/kustomization.yaml"
        overlay = yaml.safe_load(files.get(key, "resources: [../base]"))
        overlay.setdefault("patches", []).append(
            {
                "target": {"group": "autoscaling", "kind": "HorizontalPodAutoscaler"},
                "patch": yaml.safe_dump(
                    [
                        {"op": "add", "path": "/spec/minReplicas", "value": replicas},
                        {"op": "add", "path": "/spec/maxReplicas", "value": min(6, replicas * 2)},
                    ],
                    sort_keys=False,
                ),
            }
        )
        files[key] = yaml.safe_dump(overlay, sort_keys=False)
    key = "kustomize/memcached/overlay/kustomization.yaml"
    overlay = yaml.safe_load(files.get(key, "resources: [../base]"))
    overlay.setdefault("patches", []).append(
        {
            "target": {
                "group": "apps",
                "version": "v1",
                "kind": "StatefulSet",
                "name": "memcached-memcached",
            },
            "patch": yaml.safe_dump(
                [
                    {
                        "op": "test",
                        "path": "/spec/volumeClaimTemplates/0/metadata/name",
                        "value": "memcached-data",
                    },
                    {
                        "op": "add",
                        "path": "/spec/volumeClaimTemplates/0/spec/resources/requests/storage",
                        "value": str(plan.get("cache_gib", 1)) + "Gi",
                    },
                ],
                sort_keys=False,
            ),
        }
    )
    files[key] = yaml.safe_dump(overlay, sort_keys=False)
    key = "kustomize/libvirt/overlay/kustomization.yaml"
    overlay = yaml.safe_load(files.get(key, "resources: [../base]"))
    overlay.setdefault("patches", []).append(
        {
            "patch": yaml.safe_dump(
                {
                    "apiVersion": "apps/v1",
                    "kind": "DaemonSet",
                    "metadata": {"name": "libvirt-libvirt-default"},
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "genestack.dev/libvirt-talos-runtime": hashlib.sha256(
                                        (package_root() / "app/assets/libvirt-talos-post-render.sh").read_bytes()
                                    ).hexdigest()
                                }
                            },
                            "spec": {
                                "containers": [
                                    {
                                        "name": name,
                                        "volumeMounts": [
                                            {"mountPath": "/run", "$patch": "delete"},
                                            {"name": "run", "mountPath": "/run/libvirt"},
                                        ],
                                    }
                                    for name in ["libvirt", "libvirt-exporter"]
                                ],
                                "initContainers": [
                                    {"name": "libvirt-init-modules", "$patch": "delete"}
                                ],
                                "volumes": [
                                    {
                                        "name": "run",
                                        "hostPath": {
                                            "path": "/run/libvirt",
                                            "type": "DirectoryOrCreate",
                                        },
                                    },
                                    {"name": "etc-modprobe-d", "$patch": "delete"},
                                    {"name": "host-rootfs", "$patch": "delete"},
                                    {
                                        "name": "etc-libvirt-qemu",
                                        "hostPath": {
                                            "path": "/var/lib/libvirt/config/qemu",
                                            "type": "DirectoryOrCreate",
                                        },
                                    },
                                ],
                            }
                        }
                    },
                },
                sort_keys=False,
            )
        }
    )
    files[key] = yaml.safe_dump(overlay, sort_keys=False)
