"""Which login a managed machine uses.

An explicit ``ssh_user`` on the inventory row wins. A machine Genestack
installed as Ubuntu has no password and accepts this environment's key as
``ubuntu``. Anything else uses the deploy host's user, then root.
"""

from __future__ import annotations

from typing import Any

from app.models import Environment


def login_for_server(entry: dict[str, Any] | None, env: Environment, boot_stage: str = "") -> str:
    explicit = str((entry or {}).get("ssh_user") or "").strip()
    if explicit:
        return explicit
    if str(boot_stage or "").strip().lower() == "ubuntu":
        return "ubuntu"
    return str(getattr(env, "deployer_ssh_user", None) or "").strip() or "root"
