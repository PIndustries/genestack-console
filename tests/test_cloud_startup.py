"""Startup configuration must reach Nova intact or fail before creating a VM."""

from unittest.mock import Mock, patch

import pytest

from app.services import openstack_ops
from tests.test_openstack_ops import _create_env, _env

SCRIPT = '#!/bin/sh\nprintf "héllo"\n'
ARGS = {"name": "web", "image": "image", "flavor": "small", "network": "private"}


def test_startup_is_forwarded_to_native_client():
    native = Mock()
    native.server_create.return_value = {"id": "vm"}
    with (
        patch.object(
            openstack_ops, "_api_call", side_effect=lambda ctx, fn: {"ok": True, **fn(native)}
        ),
        patch.object(openstack_ops, "_run_openstack") as cli,
    ):
        result = openstack_ops.server_create(_env(), **ARGS, user_data=SCRIPT, dry_run=False)
    assert result["ok"]
    assert native.server_create.call_args.kwargs["user_data"] == SCRIPT
    cli.assert_not_called()


def test_startup_without_native_access_never_creates_an_unconfigured_vm():
    with (
        patch.object(openstack_ops, "_api_call", return_value=None),
        patch.object(openstack_ops, "_run_openstack") as cli,
    ):
        result = openstack_ops.server_create(_env(), **ARGS, user_data=SCRIPT, dry_run=False)
    assert not result["ok"]
    assert "native OpenStack API" in result["error"]
    cli.assert_not_called()


def test_startup_dry_run_does_not_dial_or_expose_content():
    with (
        patch.object(openstack_ops, "_api_call") as api,
        patch.object(openstack_ops, "_run_openstack") as cli,
    ):
        result = openstack_ops.server_create(_env(), **ARGS, user_data=SCRIPT, dry_run=True)
    assert result["dry_run"] and result["ok"]
    assert result["user_data_bytes"] == len(SCRIPT.encode())
    assert SCRIPT not in str(result)
    api.assert_not_called()
    cli.assert_not_called()


@pytest.mark.parametrize("script", ["é" * 8193, "a" * 16385, "bad\x00script", "\ud800", 123])
def test_invalid_startup_rejected_before_any_api_or_cli(script):
    with (
        patch.object(openstack_ops, "_api_call") as api,
        patch.object(openstack_ops, "_run_openstack") as cli,
    ):
        result = openstack_ops.server_create(_env(), **ARGS, user_data=script, dry_run=False)
    assert not result["ok"]
    api.assert_not_called()
    cli.assert_not_called()


def test_nova_error_cannot_echo_startup_content():
    with patch.object(
        openstack_ops,
        "_api_call",
        return_value={"ok": False, "error": SCRIPT, "message": SCRIPT, "returncode": 1},
    ):
        result = openstack_ops.server_create(_env(), **ARGS, user_data=SCRIPT, dry_run=False)
    assert SCRIPT not in str(result)


def test_startup_route_forwards_unmodified_text(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(openstack_ops, "server_create", return_value={"ok": True}) as create:
        r = client.post(
            f"/api/v1/environments/{env['id']}/cloud/servers",
            headers=admin_headers,
            json={**ARGS, "user_data": SCRIPT},
        )
    assert r.status_code == 200
    assert create.call_args.kwargs["user_data"] == SCRIPT


def test_viewer_cannot_supply_startup(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    with patch.object(openstack_ops, "server_create") as create:
        r = client.post(
            f"/api/v1/environments/{env['id']}/cloud/servers",
            headers=viewer_headers,
            json={**ARGS, "user_data": SCRIPT},
        )
    assert r.status_code == 403
    create.assert_not_called()
