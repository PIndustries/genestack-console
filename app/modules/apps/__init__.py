"""What: Deploy one application the console knows about.
Where: app/modules/apps/__init__.py. This class lists the function files below, in
order.
Why: An application hook does not belong in the cluster install.
"""

from __future__ import annotations

from app.modules.base import Module


class AppModule(Module):
    name = "apps"
    functions = (
        "deploy",
    )
