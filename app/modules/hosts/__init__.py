"""What: Ubuntu autoinstall, MicroK8s, and an existing Kubespray cluster.
Where: app/modules/hosts/__init__.py. This class lists the function files below, in
order.
Why: These paths stay next to each other and out of the Talos operations.
"""

from __future__ import annotations

from app.modules.base import Module


class HostsModule(Module):
    name = "hosts"
    functions = (
        "ubuntu",
        "microk8s",
        "kubespray",
    )
