"""What: Register a server, power it, and hand it a boot file.
Where: app/modules/baremetal/__init__.py. This class lists the function files below, in
order.
Why: A new boot step is a new file plus one line in this class.
"""

from __future__ import annotations

from app.modules.base import Module


class BaremetalModule(Module):
    name = "baremetal"
    functions = (
        "node_register",
        "nodes_list",
        "bmc_scan",
        "node_actions",
    )
