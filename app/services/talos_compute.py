"""Use Talos-managed KVM modules rather than a container writing the host OS."""

from __future__ import annotations

import copy

VENDORS = {"GenuineIntel": ("vmx", "kvm_intel"), "AuthenticAMD": ("svm", "kvm_amd")}


def from_cpuinfo(text):
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields.setdefault(key.strip(), value.strip())
    vendor = fields.get("vendor_id")
    if vendor not in VENDORS or VENDORS[vendor][0] not in fields.get("flags", "").split():
        raise ValueError(
            "Compute needs hardware virtualization enabled and exposed to the reviewed x86 node"
        )
    return VENDORS[vendor][1]


def documents(hostname, talos, servers):
    modules = talos.get("compute_kernel_modules") or {}
    if hostname not in modules:
        return []
    selected = modules[hostname]
    if selected not in [value[1] for value in VENDORS.values()] or "compute" not in servers[
        hostname
    ].get("roles", []):
        raise ValueError("Compute kernel modules must match a reviewed compute node")
    return [
        {"apiVersion": "v1alpha1", "kind": "KernelModuleConfig", "name": name}
        for name in ["kvm", selected]
    ]


def configure(doc, hostname, cpuinfo):
    result = copy.deepcopy(doc)
    selected = from_cpuinfo(cpuinfo)
    modules = result.setdefault("talos", {}).setdefault("compute_kernel_modules", {})
    if hostname in modules and modules[hostname] != selected:
        raise ValueError("Saved compute kernel support differs from the measured CPU")
    modules[hostname] = selected
    documents(hostname, result["talos"], result["servers"])
    return result
