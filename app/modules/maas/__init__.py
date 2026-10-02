"""What: Older machine calls kept so existing clients still run.
Where: app/modules/maas/__init__.py. This class lists the function files below, in
order.
Why: Installing Talos is the baremetal module. The console answers DHCP and serves the boot file.
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
