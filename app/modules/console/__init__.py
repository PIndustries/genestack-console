"""What: Health check, backup, vacuum, and the local compile.
Where: app/modules/console/__init__.py. This class lists the function files below, in
order.
Why: They stay out of the deploy and boot folders.
"""

from __future__ import annotations

from app.modules.base import Module


class ConsoleModule(Module):
    name = "console"
    functions = (
        "internal_health",
        "backup",
        "vacuum",
        "release",
    )
