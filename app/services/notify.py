"""Notification channels stored on this console.

An admin saves a Slack, Discord, or Teams webhook, or Resend or Twilio
credentials. The secret is encrypted at rest. Delivery is best-effort: a
missing channel or a failed POST is logged and skipped, and never raised
into alert evaluation.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy.orm import Session

from app.models import NotifyChannel
from app.services.crypto import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

KINDS = ("slack", "discord", "teams", "resend", "twilio")
WEBHOOK_KINDS = frozenset({"slack", "discord", "teams"})
_SECRET_KEYS = frozenset({"webhook_url", "api_key", "auth_token"})
_KIND_FIELDS = {
    "slack": ("webhook_url",),
    "discord": ("webhook_url",),
    "teams": ("webhook_url",),
    "resend": ("api_key", "from_email", "to"),
    "twilio": ("account_sid", "auth_token", "from_number", "to_number"),
}
_TIMEOUT_SECONDS = 5
_MAX_MESSAGE = 1500
RESEND_URL = "https://api.resend.com/emails"
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
MASK = "********"


def alert_text(payload: dict[str, Any]) -> str:
    """One line an operator can read in chat, email, or SMS."""
    text = (
        f"{payload.get('type')}: {payload.get('severity')} "
        f"{payload.get('rule_name')} ({payload.get('environment_name')})"
    )
    return text[:_MAX_MESSAGE]


def normalize_config(kind: str, config: dict[str, Any] | None) -> dict[str, str]:
    """Keep the fields for ``kind`` and reject a webhook that is not public.

    Raises ``ValueError`` with a reason safe to show the operator.
    """
    if kind not in _KIND_FIELDS:
        raise ValueError(f"Unknown notification kind {kind!r}")
    raw = config or {}
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    cleaned: dict[str, str] = {}
    for field in _KIND_FIELDS[kind]:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        cleaned[field] = value.strip()
    if kind in WEBHOOK_KINDS:
        from app.services.alerts import assert_safe_webhook_url

        assert_safe_webhook_url(cleaned["webhook_url"])
    return cleaned


def pack_config(config: dict[str, str]) -> str:
    """Fernet JSON for the ``config_encrypted`` column."""
    return encrypt_secret(json.dumps(config)) or ""


def unpack_config(stored: str | None) -> dict[str, Any]:
    if not stored:
        return {}
    plain = decrypt_secret(stored)
    if not plain:
        return {}
    try:
        loaded = json.loads(plain)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """Copy with secret fields replaced. Empty secrets are omitted."""
    masked: dict[str, Any] = {}
    for key, value in config.items():
        if key in _SECRET_KEYS:
            if value:
                masked[key] = MASK
            continue
        masked[key] = value
    return masked


def build_delivery(kind: str, config: dict[str, Any], message: str) -> urllib.request.Request:
    """Build the HTTP request for one channel. Does not send it.

    Webhook kinds are checked again here so a URL that became unsafe after
    it was saved is not contacted. Raises ``ValueError`` on a bad webhook.
    """
    text = (message or "")[:_MAX_MESSAGE]
    if kind in WEBHOOK_KINDS:
        url = str(config.get("webhook_url") or "")
        from app.services.alerts import assert_safe_webhook_url

        assert_safe_webhook_url(url)
        body_key = "content" if kind == "discord" else "text"
        return urllib.request.Request(
            url,
            data=json.dumps({body_key: text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    if kind == "resend":
        payload = {
            "from": config.get("from_email") or "",
            "to": [config.get("to") or ""],
            "subject": text[:120],
            "text": text,
        }
        return urllib.request.Request(
            RESEND_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.get('api_key') or ''}",
            },
            method="POST",
        )
    if kind == "twilio":
        sid = urllib.parse.quote(str(config.get("account_sid") or ""), safe="")
        token = str(config.get("auth_token") or "")
        form = urllib.parse.urlencode(
            {
                "From": config.get("from_number") or "",
                "To": config.get("to_number") or "",
                "Body": text,
            }
        ).encode("utf-8")
        basic = base64.b64encode(f"{config.get('account_sid') or ''}:{token}".encode()).decode()
        return urllib.request.Request(
            TWILIO_URL.format(account_sid=sid),
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            method="POST",
        )
    raise ValueError(f"Unknown notification kind {kind!r}")


def send_delivery(req: urllib.request.Request) -> None:
    """POST ``req``. Never raises."""
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            resp.read()
    except Exception:  # noqa: BLE001
        log.warning("notification delivery failed", exc_info=True)


def _queue(req: urllib.request.Request) -> None:
    threading.Thread(target=send_delivery, args=(req,), daemon=True).start()


def fire_channel(db: Session, channel_id: str | None, payload: dict[str, Any]) -> None:
    """Deliver ``payload`` on a saved channel, in a daemon thread.

    The row is read on the caller thread. The HTTP call does not use the
    session. A missing or disabled channel is skipped. Never raises.
    """
    if not channel_id:
        return
    try:
        channel = db.get(NotifyChannel, channel_id)
        if channel is None:
            log.info("notification channel %s is missing", channel_id)
            return
        if not channel.enabled:
            return
        config = unpack_config(channel.config_encrypted)
        req = build_delivery(channel.kind, config, alert_text(payload))
    except Exception:  # noqa: BLE001
        log.warning("notification channel %s was not sent", channel_id, exc_info=True)
        return
    _queue(req)
