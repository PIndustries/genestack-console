"""Alert rules and events endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import (
    check_tenant_access,
    get_db,
    require_operator,
    require_viewer,
)
from app.models import AlertEvent, AlertRule, Environment, Membership, NotifyChannel
from app.schemas import (
    AlertConditionName,
    AlertEventOut,
    AlertEventStatusName,
    AlertRuleIn,
    AlertRuleOut,
    AlertSeverityName,
    Principal,
)
from app.services.alerts import assert_safe_webhook_url
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def _assert_safe_webhook_url(webhook_url: Optional[str]) -> None:
    """400 on URLs that point at internal/metadata addresses."""
    if not webhook_url:
        return
    try:
        assert_safe_webhook_url(webhook_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class AlertRulePatch(BaseModel):
    """Partial update for an alert rule; unset fields are left unchanged."""

    environment_id: Optional[str] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    condition: Optional[AlertConditionName] = None
    threshold: Optional[float] = None
    severity: Optional[AlertSeverityName] = None
    enabled: Optional[bool] = None
    webhook_url: Optional[str] = Field(default=None, max_length=512)
    channel_id: Optional[str] = Field(
        default=None, description="Saved notification channel. Empty string clears it."
    )


def _visible_env_ids(db: Session, principal: Principal) -> list[str] | None:
    """Environment ids the principal may see; None means unrestricted."""
    if principal.platform_admin:
        return None
    member_tenants = select(Membership.tenant_id).where(
        Membership.user_id == principal.user_id
    )
    return list(
        db.scalars(
            select(Environment.id).where(Environment.tenant_id.in_(member_tenants))
        ).all()
    )


def _check_env_write_access(
    db: Session, principal: Principal, environment_id: Optional[str]
) -> None:
    """Global rules are platform-admin only; env rules need tenant operator."""
    if environment_id is None:
        if not principal.platform_admin:
            raise HTTPException(
                status_code=403, detail="Global alert rules require platform admin"
            )
        return
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    check_tenant_access(db, principal, env.tenant_id, "operator")


def _get_rule_scoped(db: Session, principal: Principal, rule_id: int) -> AlertRule:
    """Load a rule and enforce write access for its scope (404/403)."""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    _check_env_write_access(db, principal, rule.environment_id)
    return rule


def _checked_channel_id(db: Session, channel_id: Optional[str]) -> Optional[str]:
    """Return a stored channel id, or None when the caller clears it.

    An unknown id is a 400. An empty string clears the link.
    """
    if channel_id is None:
        return None
    channel_id = channel_id.strip()
    if not channel_id:
        return None
    if db.get(NotifyChannel, channel_id) is None:
        raise HTTPException(status_code=400, detail="Unknown notification channel")
    return channel_id


def _event_out(db: Session, event: AlertEvent) -> AlertEventOut:
    """AlertEventOut with rule_name/severity/environment_name filled in.

    Matches the SSE alert payload fields so UI tables never render "—" for
    rule, severity, or environment. Missing rule/env rows degrade to None.
    """
    out = AlertEventOut.model_validate(event)
    rule = db.get(AlertRule, event.rule_id)
    if rule is not None:
        out.rule_name = rule.name
        out.severity = rule.severity
    env = db.get(Environment, event.environment_id)
    if env is not None:
        out.environment_name = env.name
    return out


@router.get("/rules", response_model=list[AlertRuleOut])
def list_rules(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[AlertRuleOut]:
    stmt = select(AlertRule).order_by(AlertRule.id)
    env_ids = _visible_env_ids(db, principal)
    if env_ids is not None:
        # Global rules plus rules for visible environments
        stmt = stmt.where(
            (AlertRule.environment_id.is_(None))
            | (AlertRule.environment_id.in_(env_ids))
        )
    return [AlertRuleOut.model_validate(r) for r in db.scalars(stmt).all()]


@router.post("/rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    body: AlertRuleIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> AlertRuleOut:
    _check_env_write_access(db, principal, body.environment_id)
    _assert_safe_webhook_url(body.webhook_url)
    rule = AlertRule(
        environment_id=body.environment_id,
        name=body.name,
        condition=body.condition,
        threshold=body.threshold,
        severity=body.severity,
        enabled=body.enabled,
        webhook_url=body.webhook_url,
        channel_id=_checked_channel_id(db, body.channel_id),
    )
    db.add(rule)
    db.flush()

    JobRunner(db).write_audit(
        actor=principal.username,
        action="alert_rule.create",
        resource_type="alert_rule",
        resource_id=str(rule.id),
        environment_id=rule.environment_id,
        details={"name": rule.name, "condition": rule.condition},
        success=True,
    )
    db.commit()
    db.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.patch("/rules/{rule_id}", response_model=AlertRuleOut)
def update_rule(
    rule_id: int,
    body: AlertRulePatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> AlertRuleOut:
    rule = _get_rule_scoped(db, principal, rule_id)

    data = body.model_dump(exclude_unset=True)
    # Retargeting a rule requires write access to the new scope as well
    if "environment_id" in data and data["environment_id"] != rule.environment_id:
        _check_env_write_access(db, principal, data["environment_id"])
    if "webhook_url" in data:
        _assert_safe_webhook_url(data["webhook_url"])
    if "channel_id" in data:
        data["channel_id"] = _checked_channel_id(db, data["channel_id"])

    for key, value in data.items():
        setattr(rule, key, value)
    db.add(rule)
    db.flush()

    JobRunner(db).write_audit(
        actor=principal.username,
        action="alert_rule.update",
        resource_type="alert_rule",
        resource_id=str(rule.id),
        environment_id=rule.environment_id,
        details={"fields": list(data.keys())},
        success=True,
    )
    db.commit()
    db.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> None:
    rule = _get_rule_scoped(db, principal, rule_id)

    JobRunner(db).write_audit(
        actor=principal.username,
        action="alert_rule.delete",
        resource_type="alert_rule",
        resource_id=str(rule.id),
        environment_id=rule.environment_id,
        details={"name": rule.name},
        success=True,
    )
    db.delete(rule)
    db.commit()


@router.get("/events", response_model=list[AlertEventOut])
def list_events(
    status: Optional[AlertEventStatusName] = None,
    environment_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[AlertEventOut]:
    env_ids = _visible_env_ids(db, principal)
    if environment_id is not None and env_ids is not None:
        if environment_id not in env_ids:
            raise HTTPException(status_code=404, detail="Environment not found")

    stmt = select(AlertEvent).order_by(AlertEvent.fired_at.desc(), AlertEvent.id.desc())
    if env_ids is not None:
        stmt = stmt.where(AlertEvent.environment_id.in_(env_ids))
    if environment_id is not None:
        stmt = stmt.where(AlertEvent.environment_id == environment_id)
    if status is not None:
        stmt = stmt.where(AlertEvent.status == status)
    rows = db.scalars(stmt.limit(limit)).all()
    return [_event_out(db, e) for e in rows]


@router.post("/events/{event_id}/ack", response_model=AlertEventOut)
def acknowledge_event(
    event_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> AlertEventOut:
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Alert event not found")
    env = db.get(Environment, event.environment_id)
    if not principal.platform_admin:
        if env is None:
            raise HTTPException(status_code=404, detail="Alert event not found")
        check_tenant_access(db, principal, env.tenant_id, "operator")

    event.acknowledged = True
    db.add(event)
    db.flush()

    JobRunner(db).write_audit(
        actor=principal.username,
        action="alert_event.ack",
        resource_type="alert_event",
        resource_id=str(event.id),
        environment_id=event.environment_id,
        details={"rule_id": event.rule_id},
        success=True,
    )
    db.commit()
    db.refresh(event)
    return _event_out(db, event)


@router.get("/summary")
def alerts_summary(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    """Firing-alert counts for UI badges, across tenant-visible envs."""
    stmt = (
        select(AlertEvent, AlertRule.severity)
        .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
        .where(AlertEvent.status == "firing")
    )
    env_ids = _visible_env_ids(db, principal)
    if env_ids is not None:
        stmt = stmt.where(AlertEvent.environment_id.in_(env_ids))

    by_severity: dict[str, int] = {}
    by_environment: dict[str, int] = {}
    firing = 0
    for event, severity in db.execute(stmt).all():
        firing += 1
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_environment[event.environment_id] = (
            by_environment.get(event.environment_id, 0) + 1
        )
    return {
        "firing": firing,
        "by_severity": by_severity,
        "by_environment": by_environment,
    }
