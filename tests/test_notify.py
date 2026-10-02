"""Notification channels: admin writes, viewer lists a masked secret, delivery shape."""

from __future__ import annotations

import base64
import json
import urllib.parse
import uuid

from app.db import SessionLocal
from app.models import AlertRule, NotifyChannel
from app.services.crypto import decrypt_secret
from app.services.notify import build_delivery, send_delivery


def _create_env(client, headers) -> str:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"notify-env-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_admin_create_masks_secret_at_rest_and_on_read(client, admin_headers):
    resp = client.post(
        "/api/v1/notify/channels",
        headers=admin_headers,
        json={
            "name": "on-call",
            "kind": "slack",
            "config": {"webhook_url": "https://example.com/hook"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["config"]["webhook_url"] == "********"
    assert "example.com/hook" not in json.dumps(body)

    db = SessionLocal()
    try:
        row = db.get(NotifyChannel, body["id"])
        stored = json.loads(decrypt_secret(row.config_encrypted))
        assert stored["webhook_url"] == "https://example.com/hook"
    finally:
        db.close()


def test_viewer_lists_masked_channel_and_cannot_create(
    client, admin_headers, viewer_headers
):
    created = client.post(
        "/api/v1/notify/channels",
        headers=admin_headers,
        json={
            "name": "desk",
            "kind": "discord",
            "config": {"webhook_url": "https://example.com/hook"},
        },
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/notify/channels", headers=viewer_headers)
    assert listed.status_code == 200, listed.text
    match = [row for row in listed.json() if row["id"] == created.json()["id"]]
    assert match
    assert match[0]["name"] == "desk"
    assert match[0]["config"]["webhook_url"] == "********"
    assert "example.com/hook" not in json.dumps(listed.json())

    denied = client.post(
        "/api/v1/notify/channels",
        headers=viewer_headers,
        json={
            "name": "nope",
            "kind": "slack",
            "config": {"webhook_url": "https://example.com/hook"},
        },
    )
    assert denied.status_code == 403, denied.text


def test_webhook_to_internal_host_rejected(client, admin_headers):
    resp = client.post(
        "/api/v1/notify/channels",
        headers=admin_headers,
        json={
            "name": "bad",
            "kind": "teams",
            "config": {"webhook_url": "http://127.0.0.1/hook"},
        },
    )
    assert resp.status_code == 400, resp.text


def test_build_delivery_slack_resend_twilio(monkeypatch):
    sent: list = []

    def fake_urlopen(req, timeout=None):
        sent.append((req, timeout))

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp()

    monkeypatch.setattr("app.services.notify.urllib.request.urlopen", fake_urlopen)

    slack = build_delivery(
        "slack", {"webhook_url": "https://example.com/hook"}, "alert: critical disk (lab)"
    )
    assert slack.full_url == "https://example.com/hook"
    assert json.loads(slack.data.decode()) == {"text": "alert: critical disk (lab)"}
    send_delivery(slack)

    resend = build_delivery(
        "resend",
        {"api_key": "re_test", "from_email": "ops@example.com", "to": "oncall@example.com"},
        "alert: warning cpu (lab)",
    )
    assert resend.full_url == "https://api.resend.com/emails"
    assert resend.get_header("Authorization") == "Bearer re_test"
    body = json.loads(resend.data.decode())
    assert body["from"] == "ops@example.com"
    assert body["to"] == ["oncall@example.com"]
    assert body["text"] == "alert: warning cpu (lab)"
    send_delivery(resend)

    twilio = build_delivery(
        "twilio",
        {
            "account_sid": "ACxxx",
            "auth_token": "secret-token",
            "from_number": "+15550001111",
            "to_number": "+15550002222",
        },
        "alert: critical node (lab)",
    )
    assert twilio.full_url == (
        "https://api.twilio.com/2010-04-01/Accounts/ACxxx/Messages.json"
    )
    expected_basic = base64.b64encode(b"ACxxx:secret-token").decode()
    assert twilio.get_header("Authorization") == f"Basic {expected_basic}"
    form = urllib.parse.parse_qs(twilio.data.decode())
    assert form["From"] == ["+15550001111"]
    assert form["To"] == ["+15550002222"]
    assert form["Body"] == ["alert: critical node (lab)"]
    send_delivery(twilio)

    assert len(sent) == 3
    assert all(timeout == 5 for _req, timeout in sent)


def test_alert_rule_channel_id(client, admin_headers):
    created = client.post(
        "/api/v1/notify/channels",
        headers=admin_headers,
        json={
            "name": "pager",
            "kind": "slack",
            "config": {"webhook_url": "https://example.com/hook"},
        },
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]
    env_id = _create_env(client, admin_headers)

    missing = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": "bad-channel",
            "condition": "node_not_ready",
            "environment_id": env_id,
            "channel_id": "missing-channel",
        },
    )
    assert missing.status_code == 400, missing.text

    rule = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": "nodes",
            "condition": "node_not_ready",
            "environment_id": env_id,
            "channel_id": channel_id,
            "webhook_url": "https://example.com/hook",
        },
    )
    assert rule.status_code == 201, rule.text
    assert rule.json()["channel_id"] == channel_id

    cleared = client.patch(
        f"/api/v1/alerts/rules/{rule.json()['id']}",
        headers=admin_headers,
        json={"channel_id": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["channel_id"] is None

    client.patch(
        f"/api/v1/alerts/rules/{rule.json()['id']}",
        headers=admin_headers,
        json={"channel_id": channel_id},
    )
    deleted = client.delete(f"/api/v1/notify/channels/{channel_id}", headers=admin_headers)
    assert deleted.status_code == 204, deleted.text
    db = SessionLocal()
    try:
        stored = db.get(AlertRule, rule.json()["id"])
        assert stored.channel_id is None
    finally:
        db.close()
