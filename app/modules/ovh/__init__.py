"""What: Reinstall an OVH dedicated server or attach it to a vRack.
Where: app/modules/ovh/__init__.py. This class lists the function files below, in order.
Why: The provider steps stay in one place.
"""

from __future__ import annotations

from app.modules.base import Module


class OvhModule(Module):
    name = "ovh"
    functions = (
        "byoi_reinstall",
        "vrack_attach",
    )
