"""What: List and power virtual machines on the console host.
Where: app/modules/hostvm/__init__.py. This class lists the function files below, in
order.
Why: Lab VMs stay separate from bare-metal servers.
"""

from __future__ import annotations

from app.modules.base import Module


class HostVmModule(Module):
    name = "hostvm"
    functions = (
        "hostvm_list",
        "power",
    )
