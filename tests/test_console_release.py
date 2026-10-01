"""console.release compiles then publishes; dry-run does not shell out."""


def test_console_release_is_catalogued():
    from app.services.catalog import get_operation

    op = get_operation("console.release")
    assert op is not None
    assert op.handler == "console_release"
    assert op.required_role == "admin"


def test_console_release_dry_run(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "console.release", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["status"] in ("success", "failed")
    # Suite config is dry_run: true — must not invoke Nuitka.
    log = body.get("log_text") or ""
    result = body.get("result") or {}
    assert "nuitka" not in log.lower()
    assert result.get("dry_run") is True or body["status"] == "success"
