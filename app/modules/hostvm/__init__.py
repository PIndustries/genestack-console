"""What: List, power, and create virtual machines on the console host.
Where: app/modules/hostvm/__init__.py. This class lists the function files below, in
order.
Why: Lab VMs stay separate from bare-metal servers. The lab job is how a console
starts its own guests.
"""

from __future__ import annotations

from app.modules.base import Module


class HostVmModule(Module):
    name = "hostvm"
    functions = (
        "hostvm_list",
        "power",
        "lab",
    )
