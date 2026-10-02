"""Alert evaluation: default rules, snapshot conditions, webhook delivery.

The collector calls ``evaluate_snapshot`` after persisting each cluster
snapshot; ``seed_default_rules`` runs once at startup to install the global
rule set. Both are defensive by contract: they commit their own changes and
never raise, so alerting can never break the collection loop.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AlertEvent, AlertRule, ClusterSnapshot, Environment
from app.services.events import publish_sync

log = logging.getLogger(__name__)

# OpenStack control-plane services whose helm releases must stay deployed.
CORE_SERVICES = [
    "mariadb",
    "rabbitmq",
    "keystone",
    "memcached",
    "glance",
    "nova",
    "neutron",
    "horizon",
]

# (name, condition, severity) for the global rules seeded on an empty table.
_DEFAULT_RULES: list[tuple[str, str, str]] = [
    ("node-not-ready", "node_not_ready", "critical"),
    ("pod-crashloop", "pod_crashloop", "warning"),
    ("probe-failed", "probe_failed", "critical"),
    ("core-service-down", "service_down", "critical"),
]

_WEBHOOK_TIMEOUT_SECONDS = 5

# Carrier-grade NAT range (100.64.0.0/10). Not flagged by ipaddress.is_private,
# but still non-public and reachable from inside many networks — reject it.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything a tenant operator must not be able to POST to."""
    if (
        ip.is_private  # RFC1918 (10/8, 172.16/12, 192.168/16) + FC00/7
        or ip.is_loopback
        or ip.is_link_local  # 169.254/16, incl. the 169.254.169.254 metadata IP
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return False


def assert_safe_webhook_url(url: str) -> None:
    """Reject webhook URLs that would let a tenant operator hit internal hosts.

    ``webhook_url`` is operator-supplied, so it is untrusted input: an attacker
    who controls it can point alert delivery at ``http://169.254.169.254`` (cloud
    metadata) or at the internal RFC1918/CGNAT ranges. We restrict it to
    http/https and to hosts whose resolved addresses are all public. Resolution
    goes through ``getaddrinfo`` so hostname aliases that point at internal
    ranges are caught, not just the literal hostname. Raises ``ValueError`` with
    the reason.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"webhook URL scheme must be http or https, got {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise ValueError("webhook URL has no hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise ValueError(f"webhook URL host does not resolve: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise ValueError(f"webhook URL host resolves to a blocked address: {ip}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def seed_default_rules(db: Session) -> int:
    """Insert the 4 global default rules if the table is empty. Idempotent."""
    if db.scalar(select(func.count()).select_from(AlertRule)):
        return 0
    for name, condition, severity in _DEFAULT_RULES:
        db.add(
            AlertRule(
                environment_id=None,
                name=name,
                condition=condition,
                threshold=1.0,
                severity=severity,
                enabled=True,
            )
        )
    db.commit()
    return len(_DEFAULT_RULES)


def _measure(rule: AlertRule, snapshot: ClusterSnapshot) -> tuple[bool, dict[str, Any]]:
    """Evaluate one rule against a snapshot: (condition_met, details)."""
    summary = snapshot.summary or {}
    threshold = rule.threshold if rule.threshold is not None else 1.0

    if rule.condition == "probe_failed":
        measured = 0.0 if snapshot.probe_ok else 1.0
        details: dict[str, Any] = {"error": snapshot.error}
    elif rule.condition == "node_not_ready":
        measured = float(
            (summary.get("nodes_total") or 0) - (summary.get("nodes_ready") or 0)
        )
        details = {
            "nodes": [
                node.get("name")
                for node in (snapshot.nodes or [])
                if not node.get("ready")
            ]
        }
    elif rule.condition == "pod_crashloop":
        crashlooping = summary.get("crashlooping") or []
        measured = float(len(crashlooping))
        details = {"crashlooping": list(crashlooping)}
    elif rule.condition == "service_down":
        down = [
            {
                "name": release.get("name"),
                "ns": release.get("ns"),
                "status": release.get("status"),
            }
            for release in (snapshot.helm or [])
            if release.get("status") != "deployed"
            and any(core in (release.get("name") or "") for core in CORE_SERVICES)
        ]
        measured = float(len(down))
        details = {"services": down}
    else:
        # Unknown condition: never fires.
        return False, {}
    return measured >= threshold, details


def _firing_event(db: Session, rule_id: int, env_id: str) -> AlertEvent | None:
    return db.scalar(
        select(AlertEvent)
        .where(
            AlertEvent.rule_id == rule_id,
            AlertEvent.environment_id == env_id,
            AlertEvent.status == "firing",
        )
        .order_by(AlertEvent.fired_at.desc())
    )


def _event_payload(
    kind: str,
    rule: AlertRule,
    env: Environment,
    event: AlertEvent,
) -> dict[str, Any]:
    return {
        "type": kind,
        "event_id": event.id,
        "rule_id": rule.id,
        "rule_name": rule.name,
        "condition": rule.condition,
        "severity": rule.severity,
        "environment_id": env.id,
        "environment_name": env.name,
        "fired_at": event.fired_at.isoformat() if event.fired_at else None,
        "resolved_at": event.resolved_at.isoformat() if event.resolved_at else None,
        "details": event.details,
    }


def _evaluate_rule(
    db: Session, rule: AlertRule, env: Environment, snapshot: ClusterSnapshot
) -> AlertEvent | None:
    """Apply the firing/resolved state machine for one (rule, env) pair."""
    condition_met, details = _measure(rule, snapshot)
    existing = _firing_event(db, rule.id, env.id)

    if condition_met:
        if existing is not None:
            # Already firing: refresh details only, no new event/notify.
            existing.details = details
            db.commit()
            return None
        event = AlertEvent(
            rule_id=rule.id,
            environment_id=env.id,
            status="firing",
            details=details,
        )
        db.add(event)
        db.commit()
        payload = _event_payload("alert_fired", rule, env, event)
        publish_sync("alerts", payload)
        fire_webhook(rule.webhook_url, payload)
        # A saved channel is optional. A missing one is logged inside
        # fire_channel and does not fail evaluation.
        try:
            from app.services.notify import fire_channel

            fire_channel(db, rule.channel_id, payload)
        except Exception:  # noqa: BLE001
            log.warning("alert channel delivery skipped for rule %s", rule.id, exc_info=True)
        return event

    if existing is not None:
        existing.status = "resolved"
        existing.resolved_at = _utcnow()
        db.commit()
        publish_sync("alerts", _event_payload("alert_resolved", rule, env, existing))
        return existing
    return None


def evaluate_snapshot(
    db: Session, env: Environment, snapshot: ClusterSnapshot
) -> list[AlertEvent]:
    """Evaluate all applicable rules against one snapshot.

    Returns the events whose state changed (newly firing or newly resolved).
    Never raises: a bad rule or snapshot is logged and skipped.
    """
    changed: list[AlertEvent] = []
    try:
        rules = db.scalars(
            select(AlertRule).where(
                AlertRule.enabled.is_(True),
                or_(
                    AlertRule.environment_id.is_(None),
                    AlertRule.environment_id == env.id,
                ),
            )
        ).all()
        for rule in rules:
            try:
                event = _evaluate_rule(db, rule, env, snapshot)
            except Exception:  # noqa: BLE001
                log.exception(
                    "alert rule %s evaluation failed for env %s", rule.id, env.id
                )
                db.rollback()
                continue
            if event is not None:
                changed.append(event)
    except Exception:  # noqa: BLE001
        log.exception("alert evaluation failed for env %s", getattr(env, "id", None))
        db.rollback()
    return changed


def fire_webhook(url: str | None, payload: dict[str, Any]) -> None:
    """POST the payload as JSON to ``url`` in a daemon thread.

    Delivery never blocks the caller and never raises; failures are logged.
    """
    if not url:
        return
    # Defence in depth: rules are validated at write time, but a bad URL could
    # predate the guard or be injected out-of-band. Never reach internal hosts.
    try:
        assert_safe_webhook_url(url)
    except ValueError as exc:
        log.warning("skipping alert webhook to %s: %s", url, exc)
        return

    def _send() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:  # noqa: BLE001
            log.warning("alert webhook delivery failed: %s", url, exc_info=True)

    threading.Thread(target=_send, daemon=True).start()
