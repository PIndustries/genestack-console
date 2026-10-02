"""What: List servers and start, stop, reboot, or delete one.
Where: app/modules/openstack/__init__.py. This class lists the function files below, in
order.
Why: Server power stays separate from installing OpenStack.
"""

from __future__ import annotations

from app.modules.base import Module


class OpenStackModule(Module):
    name = "openstack"
    functions = (
        "servers_list",
        "server_actions",
    )
