"""What: Reboot, reset, or upgrade a Talos node, and drain or apply on Kubernetes.
Where: app/modules/platform/__init__.py. This class lists the function files below, in
order.
Why: Day-2 steps stay out of the first-install files. k8s_node_drain and k8s_apply live
here too.
"""

from __future__ import annotations

from app.modules.base import Module


class PlatformModule(Module):
    name = "platform"
    functions = (
        "talos_reboot",
        "talos_shutdown",
        "talos_reset",
        "talos_upgrade",
        "talos_upgrade_many",
        "talos_apply_config",
        "k8s_node_drain",
        "k8s_apply",
    )
