"""Per-environment executor selection: agent -> ssh deploy host -> local.

The preference order is agent (when one is connected for the environment,
checked with a cheap DB query — app/services/agents.agent_available), then
the env's ssh deploy host, then local execution on the console host. Job
handlers (pipeline stages, enable_service, deploy) and the config push all
route through :func:`pick_executor` so the choice is made in one place and
works identically from the worker daemon process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.db import SessionLocal
from app.services import agents as agents_service

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import Environment
    from app.services.envcontext import EnvContext


@dataclass(frozen=True)
class ExecutorChoice:
    """Where commands for an environment run."""

    kind: str  # "agent" | "ssh" | "local"
    agent_env_id: str | None = None
    ssh_target: str | None = None


def pick_executor(
    env: Environment | None,
    ctx: EnvContext,
    settings: (
        Settings | None
    ) = None,  # noqa: ARG001 — reserved for future executor config
) -> ExecutorChoice:
    """Choose the executor for an environment: agent > ssh deploy host > local.

    The agent check is DB-only (credential with a recent frame), so it works
    from the worker process where no live agent registry exists.
    """
    if env is not None:
        db = SessionLocal()
        try:
            if agents_service.agent_available(db, env.id):
                return ExecutorChoice("agent", agent_env_id=env.id)
        finally:
            db.close()
    if ctx.ssh_target:
        return ExecutorChoice("ssh", ssh_target=ctx.ssh_target)
    return ExecutorChoice("local")
