"""What: See an agent, run an allow-listed command, or install the agent.
Where: app/modules/agents/__init__.py. This class lists the function files below, in
order.
Why: Agent steps stay together and out of the install files.
"""

from __future__ import annotations

from app.modules.base import Module


class AgentModule(Module):
    name = "agents"
    functions = (
        "status",
        "command",
        "install",
    )
