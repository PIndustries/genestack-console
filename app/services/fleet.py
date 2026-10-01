"""Fleet status board assembly (read-only).

Builds the GET /api/v1/fleet payload: the caller's tenants plus every
environment they can see, each with the six workflow step states reduced
to a compact traffic-light mapping.

Two properties matter here:

- No live probes: workflow builds run with ``include_operate_probe=False``
  so a fleet of N environments never costs N kubectl calls (each with a
  5s timeout) — the operate step reports "not checked".
- Never raises: one broken environment degrades to an all-pending row
  with an ``error`` note instead of breaking the whole board.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Environment, Membership, Tenant
from app.schemas import Principal
from app.services import agents as agents_service
from app.services.workflow import build_workflow

# Fixed lifecycle order — also the order current_step is resolved in
STEP_ORDER = ("connect", "inventory", "config", "push", "deploy", "operate")


def _tenants_for(db: Session, principal: Principal) -> list[dict[str, Any]]:
    """The caller's tenants; platform admins get all tenants with role null."""
    if principal.platform_admin:
        rows = db.scalars(select(Tenant).order_by(Tenant.name)).all()
        return [{"id": t.id, "name": t.name, "role": None} for t in rows]
    stmt = (
        select(Tenant, Membership.role)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == principal.user_id)
        .order_by(Tenant.name)
    )
    return [
        {"id": tenant.id, "name": tenant.name, "role": role.value}
        for tenant, role in db.execute(stmt).all()
    ]


def _visible_environments(db: Session, principal: Principal) -> list[Environment]:
    """Mirror the environments list: member tenants only, all for admins."""
    stmt = select(Environment).order_by(Environment.name)
    if not principal.platform_admin:
        member_tenants = select(Membership.tenant_id).where(
            Membership.user_id == principal.user_id
        )
        stmt = stmt.where(Environment.tenant_id.in_(member_tenants))
    return list(db.scalars(stmt).all())


def _current_step(steps: dict[str, str]) -> str | None:
    """First step (in STEP_ORDER) not done; None when all are done."""
    for step_id in STEP_ORDER:
        if steps.get(step_id) != "done":
            return step_id
    return None


def _deploy_view(deploy_step: dict[str, Any]) -> dict[str, Any] | None:
    """Compact deploy progress from the workflow deploy step, else None."""
    details = deploy_step.get("details") or {}
    if details.get("job_id") is None:
        return None
    return {
        "job_id": details.get("job_id"),
        "status": details.get("status"),
        "stages_completed": details.get("stages_completed"),
        "stages_total": details.get("stages_total"),
        "dry_run": details.get("dry_run"),
    }


def _env_view(
    db: Session,
    env: Environment,
    tenant_names: dict[str, str],
    settings: Settings,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": env.id,
        "name": env.name,
        "region": env.region,
        "tier": env.tier,
        "tenant_id": env.tenant_id,
        "tenant_name": tenant_names.get(env.tenant_id) if env.tenant_id else None,
        "dry_run": (
            bool(env.dry_run) if env.dry_run is not None else bool(settings.dry_run)
        ),
    }
    try:
        workflow = build_workflow(db, env, settings, include_operate_probe=False)
        steps = {step["id"]: step["state"] for step in workflow["steps"]}
        deploy_step = next(s for s in workflow["steps"] if s["id"] == "deploy")
    except Exception as exc:  # noqa: BLE001 — one env must never break the board
        steps = {step_id: "pending" for step_id in STEP_ORDER}
        deploy_step = {"details": {}}
        view["error"] = str(exc)
    view["steps"] = steps
    view["current_step"] = _current_step(steps)
    view["deploy"] = _deploy_view(deploy_step)
    # Agent connectivity (enrollment from the DB + live registry state) —
    # cheap reads, and a failure here must not take the board down either.
    try:
        agent_status = agents_service.status_for_env(db, env.id)
        view["agent"] = {
            "enrolled": bool(agent_status.get("enrolled")),
            "connected": bool(agent_status.get("connected")),
        }
    except Exception:  # noqa: BLE001 — degrade to "no agent" for one env
        view["agent"] = {"enrolled": False, "connected": False}
    return view


def build_fleet(
    db: Session,
    principal: Principal,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Assemble the fleet board payload for one caller."""
    settings = settings or get_settings()
    tenant_names = {t.id: t.name for t in db.scalars(select(Tenant)).all()}
    return {
        "tenants": _tenants_for(db, principal),
        "environments": [
            _env_view(db, env, tenant_names, settings)
            for env in _visible_environments(db, principal)
        ],
    }
