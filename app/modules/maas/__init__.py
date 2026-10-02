"""What: List machines and run commission, deploy, and release on a MAAS server.
Where: app/modules/maas/__init__.py. This class lists the function files below, in
order.
Why: The console's own DHCP and boot files are the baremetal module, not this one.
"""

from __future__ import annotations

from app.modules.base import Module


class MaasModule(Module):
    name = "maas"
    functions = (
        "machines_list",
        "machine_power_status",
        "machine_action",
        "talos_image_upload",
    )
