"""What: Plan and apply Terraform for a hardware account.
Where: app/modules/hardware/__init__.py. This class lists the function files below, in
order.
Why: Terraform stays out of the server boot files.
"""

from __future__ import annotations

from app.modules.base import Module


class HardwareModule(Module):
    name = "hardware"
    functions = (
        "terraform_actions",
    )
