"""OpenStack ops tests: executor mocked, list parsing, actions, router, catalog."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

from app.models import Environment
from app.services import openstack_ops
from app.services.catalog import get_operation, mutating_operation_ids
from app.services.envcontext import build_context
from app.services.osclient import OpenStackError

VALID_ID = "07a70f6b-a715-4aeb-9bc7-9bb214d384f6"
VALID_ID_2 = "b1f2c3d4-1111-4abc-8def-0123456789ab"

ROWS = [
    {
        "ID": VALID_ID,
        "Name": "web-1",
        "Status": "ACTIVE",
        "Task State": None,
        "Power State": "Running",
        "Flavor Name": "m1.small",
        "Image Name": "ubuntu-22.04",
        "Networks": {"private": ["10.0.0.11"]},
        "Created": "2026-07-30T12:00:00Z",
        "Host": "compute-1",
        "Project ID": "proj-1",
        "Project Name": "admin",
    },
    {
        "ID": VALID_ID_2,
        "Name": "db-1",
        "Status": "SHUTOFF",
        "Task State": None,
        "Power State": "Shut Down",
        "Flavor Name": "m1.large",
        "Image Name": "centos-9-stream",
        "Networks": {"private": ["10.0.0.12"]},
        "Created": "2026-07-29T09:30:00Z",
        "Host": "compute-2",
        "Project ID": "proj-2",
    },
]


def _run_result(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
    message: str = "ok",
    dry_run: bool = False,
) -> dict:
    return {
        "dry_run": dry_run,
        "cmd": ["kubectl", "exec", "..."],
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "message": message,
    }


def _env() -> Environment:
    # Transient row — build_context only reads attribute fields.
    return Environment(name=f"os-ops-{uuid.uuid4().hex[:8]}")


# ---------------------------------------------------------------------------
# list_servers: parsing and failure paths (executor mocked)
# ---------------------------------------------------------------------------


def test_list_servers_parses_rows():
    with patch.object(
        openstack_ops,
        "_run_openstack",
        return_value=_run_result(stdout=json.dumps(ROWS)),
    ) as mock_run:
        result = openstack_ops.list_servers(_env())

    assert result["source"] == "live"
    assert result["error"] is None
    assert len(result["vms"]) == 2
    first = result["vms"][0]
    assert first == {
        "id": VALID_ID,
        "name": "web-1",
        "status": "ACTIVE",
        "power_state": "Running",
        "flavor": "m1.small",
        "image": "ubuntu-22.04",
        "addresses": {"private": ["10.0.0.11"]},
        "created": "2026-07-30T12:00:00Z",
        "host": "compute-1",
        "project_id": "proj-1",
        "project_name": "admin",
    }
    assert result["vms"][1]["status"] == "SHUTOFF"
    assert result["vms"][1]["project_id"] == "proj-2"
    assert result["vms"][1]["project_name"] is None
    # The CLI invocation asks for all projects, long format, JSON output.
    args = mock_run.call_args.args[1]
    assert args == ["server", "list", "--all-projects", "--long", "-f", "json"]


def test_list_servers_empty_list_is_live():
    with patch.object(
        openstack_ops, "_run_openstack", return_value=_run_result(stdout="[]")
    ):
        result = openstack_ops.list_servers(_env())
    assert result == {"vms": [], "source": "live", "error": None}


def test_list_servers_failure_reports_unavailable():
    with patch.object(
        openstack_ops,
        "_run_openstack",
        return_value=_run_result(
            returncode=1,
            stderr='Error from server (NotFound): pods "openstack-admin-client" not found',
            message="failed rc=1",
        ),
    ):
        result = openstack_ops.list_servers(_env())
    assert result["source"] == "unavailable"
    assert result["vms"] == []
    assert "openstack-admin-client" in result["error"]


def test_list_servers_timeout_reports_unavailable():
    with patch.object(
        openstack_ops,
        "_run_openstack",
        return_value=_run_result(
            returncode=124,
            stderr="Timeout after 15s",
            message="Command timed out after 15s",
        ),
    ):
        result = openstack_ops.list_servers(_env())
    assert result["source"] == "unavailable"
    assert (
        "timed out" in result["error"].lower() or "timeout" in result["error"].lower()
    )


def test_list_servers_invalid_json_reports_unavailable():
    with patch.object(
        openstack_ops, "_run_openstack", return_value=_run_result(stdout="not json{")
    ):
        result = openstack_ops.list_servers(_env())
    assert result["source"] == "unavailable"
    assert "invalid openstack output" in result["error"]


def test_list_servers_prefers_native_api_when_kubeconfig():
    rows = [
        {
            "id": VALID_ID,
            "name": "web-1",
            "status": "ACTIVE",
            "power_state": 1,
            "flavor": "m1.small",
            "image": "ubuntu-22.04",
            "addresses": {"private": [{"addr": "10.0.0.11"}]},
            "host": "compute-1",
            "created": "2026-07-30T12:00:00Z",
            "project_id": "proj-1",
            "project_name": "admin",
        }
    ]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: ARG002
            return None

        def list_servers(self):
            return rows

    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=FakeClient()),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        result = openstack_ops.list_servers(_env())
    assert result == {"vms": rows, "source": "openstack-api", "error": None}
    mock_run.assert_not_called()


def test_list_servers_falls_back_to_cli_on_api_failure():
    class FakeClient:
        def __enter__(self):
            raise RuntimeError("kube proxy unreachable")

        def __exit__(self, *exc):  # noqa: ARG002
            return None

    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=FakeClient()),
        patch.object(
            openstack_ops,
            "_run_openstack",
            return_value=_run_result(stdout=json.dumps(ROWS)),
        ) as mock_run,
    ):
        result = openstack_ops.list_servers(_env())
    assert result["source"] == "live"
    assert result["error"] is None
    assert len(result["vms"]) == 2
    mock_run.assert_called_once()


def test_list_servers_skips_api_without_kubeconfig():
    with (
        patch.object(openstack_ops, "_kube_path", return_value=None),
        patch.object(openstack_ops, "OpenStackClient") as mock_client,
        patch.object(
            openstack_ops, "_run_openstack", return_value=_run_result(stdout="[]")
        ) as mock_run,
    ):
        result = openstack_ops.list_servers(_env())
    assert result == {"vms": [], "source": "live", "error": None}
    mock_client.assert_not_called()
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Executor argv construction
# ---------------------------------------------------------------------------


def test_run_openstack_builds_kubectl_exec_argv():
    ctx = build_context(_env())
    with patch.object(
        openstack_ops.bridge, "run_command", return_value=_run_result()
    ) as mock_bridge:
        openstack_ops._run_openstack(ctx, ["server", "list"], dry_run=False)
    argv = mock_bridge.call_args.args[0]
    assert argv[:6] == [
        "kubectl",
        "exec",
        "-n",
        "openstack",
        "openstack-admin-client",
        "--",
    ]
    assert argv[6:] == ["openstack", "server", "list"]
    # Kubeconfig travels via env, not argv (console-local paths break ssh runs).
    assert not any("--kubeconfig" in str(a) for a in argv)


# ---------------------------------------------------------------------------
# server_action: argv, dry-run, validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["start", "stop", "reboot", "delete"])
def test_server_action_argv_per_action(action):
    captured: dict = {}

    def fake_run(ctx, args, **kwargs):  # noqa: ARG001
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _run_result(dry_run=kwargs.get("dry_run", False))

    with patch.object(openstack_ops, "_run_openstack", side_effect=fake_run):
        result = openstack_ops.server_action(
            _env(), None, action, VALID_ID, dry_run=False
        )

    assert captured["args"] == ["server", action, VALID_ID]
    assert captured["kwargs"]["dry_run"] is False
    assert result["ok"] is True
    assert result["action"] == action
    assert result["server_id"] == VALID_ID


def test_server_hard_reboot_argv():
    captured: dict = {}

    def fake_run(ctx, args, **kwargs):  # noqa: ARG001
        captured["args"] = args
        return _run_result(dry_run=False)

    with patch.object(openstack_ops, "_run_openstack", side_effect=fake_run):
        result = openstack_ops.server_action(
            _env(), None, "hard-reboot", VALID_ID, dry_run=False
        )
    assert captured["args"] == ["server", "reboot", "--hard", VALID_ID]
    assert result["ok"] is True


def test_server_action_dry_run_does_not_execute():
    with patch.object(
        openstack_ops.bridge, "run_command", return_value=_run_result(dry_run=True)
    ) as mock_bridge:
        result = openstack_ops.server_action(
            _env(), None, "stop", VALID_ID, dry_run=True
        )
    assert mock_bridge.call_args.kwargs["dry_run"] is True
    assert result["ok"] is True
    assert result["dry_run"] is True


@pytest.mark.parametrize(
    "bad_id",
    [
        "'; rm -rf /",
        "server; reboot",
        "../etc/passwd",
        "not-a-uuid",
        "",
        None,
    ],
)
def test_server_action_rejects_invalid_server_id(bad_id):
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.server_action(
            _env(), None, "delete", bad_id, dry_run=False
        )
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "invalid server_id" in result["error"]
    # Rejected before any subprocess is built.
    mock_run.assert_not_called()


def test_server_action_rejects_unknown_action():
    result = openstack_ops.server_action(
        _env(), None, "explode", VALID_ID, dry_run=False
    )
    assert result["ok"] is False
    assert result["returncode"] == 2


def test_server_action_failure_propagates():
    with patch.object(
        openstack_ops,
        "_run_openstack",
        return_value=_run_result(
            returncode=1, stderr="No Server found", message="failed rc=1"
        ),
    ):
        result = openstack_ops.server_action(
            _env(), None, "start", VALID_ID, dry_run=False
        )
    assert result["ok"] is False
    assert result["returncode"] == 1


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


def test_catalog_registers_openstack_ops():
    list_op = get_operation("openstack.servers.list")
    assert list_op is not None
    assert list_op.mutating is False
    assert list_op.required_role == "viewer"

    for op_id in (
        "openstack.server.start",
        "openstack.server.stop",
        "openstack.server.reboot",
        "openstack.server.delete",
    ):
        op = get_operation(op_id)
        assert op is not None, op_id
        assert op.mutating is True
        assert op_id in mutating_operation_ids()
        required = {p.name for p in op.params if p.required}
        assert "server_id" in required

    assert "openstack.servers.list" not in mutating_operation_ids()
    assert get_operation("openstack.server.delete").required_role == "admin"


# ---------------------------------------------------------------------------
# Router: /api/v1/environments/{id}/vms
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wire_router(request):
    # Avoid pulling FastAPI `app` into unit tests that only mock executors.
    if "client" not in request.fixturenames and "app" not in request.fixturenames:
        return
    app = request.getfixturevalue("app")
    from app.routers import vms

    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/v1/environments/{environment_id}/vms" not in paths:
        app.include_router(vms.router)


def _create_env(client, admin_headers, name=None, tenant_id=None):
    body = {"name": name or f"vms-env-{uuid.uuid4().hex[:8]}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _vm_payload() -> dict:
    return {
        "vms": [
            {
                "id": VALID_ID,
                "name": "web-1",
                "status": "ACTIVE",
                "power_state": "Running",
                "flavor": "m1.small",
                "image": "ubuntu-22.04",
                "addresses": {"private": ["10.0.0.11"]},
                "created": "2026-07-30T12:00:00Z",
                "host": "compute-1",
                "project_id": "proj-1",
                "project_name": "admin",
            }
        ],
        "source": "live",
        "error": None,
    }


def test_vms_endpoint_returns_live_list(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(openstack_ops, "list_servers", return_value=_vm_payload()):
        resp = client.get(
            f"/api/v1/environments/{env['id']}/vms", headers=admin_headers
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["environment_id"] == env["id"]
    assert body["source"] == "live"
    assert body["error"] is None
    assert body["vms"][0]["name"] == "web-1"
    assert body["vms"][0]["power_state"] == "Running"
    assert body["vms"][0]["project_id"] == "proj-1"
    assert body["vms"][0]["project_name"] == "admin"
    assert body["vms"][0]["host"] == "compute-1"
    assert body["vms"][0]["addresses"] == {"private": ["10.0.0.11"]}


def test_vms_endpoint_unavailable_source_still_200(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(
        openstack_ops,
        "list_servers",
        return_value={
            "vms": [],
            "source": "unavailable",
            "error": "timed out after 15s",
        },
    ):
        resp = client.get(
            f"/api/v1/environments/{env['id']}/vms", headers=admin_headers
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "unavailable"
    assert body["vms"] == []
    assert "timed out" in body["error"]


def test_vms_endpoint_tenant_isolation(client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"vta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"vtb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"vms-viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    with patch.object(openstack_ops, "list_servers", return_value=_vm_payload()):
        # Own tenant's env: readable.
        resp = client.get(f"/api/v1/environments/{env_a['id']}/vms", headers=headers)
        assert resp.status_code == 200, resp.text
        # Other tenant's env: 403 (same behavior as the state router).
        assert (
            client.get(
                f"/api/v1/environments/{env_b['id']}/vms", headers=headers
            ).status_code
            == 403
        )
        # Nonexistent env: 404.
        assert (
            client.get(
                "/api/v1/environments/does-not-exist/vms", headers=headers
            ).status_code
            == 404
        )


# ---------------------------------------------------------------------------
# Jobs: actions through the queue (dry-run env, executor reaches the log)
# ---------------------------------------------------------------------------


def _submit_job(client, headers, env_id, operation, params):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params, "run_sync": True},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    return resp.json()


def test_job_server_start_dry_run_logs_argv(client, admin_headers):
    env = _create_env(client, admin_headers)
    sid = str(uuid.uuid4())
    job = _submit_job(
        client, admin_headers, env["id"], "openstack.server.start", {"server_id": sid}
    )
    # Global dry_run (test config): the job succeeds without executing.
    assert job["status"] == "success", job
    assert f"server start {sid}" in job["log_text"]
    assert "[dry-run]" in job["log_text"]
    assert "openstack-admin-client" in job["log_text"]


def test_job_server_action_rejects_garbage_server_id(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        job = _submit_job(
            client,
            admin_headers,
            env["id"],
            "openstack.server.delete",
            {"server_id": "'; rm -rf /"},
        )
    assert job["status"] == "failed", job
    assert "invalid server_id" in (job["error"] or "")
    # The injection string never reached a subprocess.
    mock_run.assert_not_called()


def test_job_servers_list_op_dispatches(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(
        openstack_ops, "list_servers", return_value=_vm_payload()
    ) as mock_list:
        job = _submit_job(
            client, admin_headers, env["id"], "openstack.servers.list", {}
        )
    assert job["status"] == "success", job
    assert mock_list.called
    assert "1 servers" in job["log_text"]


def test_job_servers_list_native_api_source_is_success(client, admin_headers):
    env = _create_env(client, admin_headers)
    payload = {**_vm_payload(), "source": "openstack-api"}
    with patch.object(openstack_ops, "list_servers", return_value=payload):
        job = _submit_job(
            client, admin_headers, env["id"], "openstack.servers.list", {}
        )
    assert job["status"] == "success", job
    assert "source=openstack-api" in job["log_text"]


# ---------------------------------------------------------------------------
# cloud inventory + native mutations
# ---------------------------------------------------------------------------


def _inventory_bundle_payload() -> str:
    return json.dumps(
        {
            "servers": {"error": None, "items": ROWS},
            "images": {
                "error": None,
                "items": [
                    {"ID": VALID_ID, "Name": "Cirros 0.6.2 64-bit", "Status": "active"}
                ],
            },
            "flavors": {
                "error": None,
                "items": [
                    {
                        "ID": VALID_ID_2,
                        "Name": "m1.tiny",
                        "RAM": 512,
                        "Disk": 1,
                        "VCPUs": 1,
                        "Is Public": True,
                    }
                ],
            },
            "volumes": {"error": None, "items": []},
            "networks": {
                "error": None,
                "items": [
                    {
                        "ID": VALID_ID,
                        "Name": "demo-net",
                        "Subnets": [],
                        "Status": "ACTIVE",
                    }
                ],
            },
            "subnets": {"error": None, "items": []},
            "routers": {"error": None, "items": []},
            "floating_ips": {"error": None, "items": []},
            "security_groups": {"error": None, "items": []},
            "keypairs": {"error": None, "items": []},
            "projects": {"error": None, "items": []},
            "users": {"error": None, "items": []},
        }
    )


def test_cloud_inventory_parses_sections():
    openstack_ops.invalidate_cloud_cache(_env().id)
    env = _env()
    env.id = "inv-" + uuid.uuid4().hex[:8]
    with patch.object(
        openstack_ops,
        "_run_admin",
        return_value=_run_result(stdout=_inventory_bundle_payload()),
    ) as mock_admin:
        result = openstack_ops.cloud_inventory(env)
        again = openstack_ops.cloud_inventory(env)
    assert mock_admin.call_count == 1
    assert again.get("cached") is True
    assert result["available"] is True
    assert result["source"] == "live"
    assert result["servers"][0]["name"] == "web-1"
    assert result["images"][0]["name"] == "Cirros 0.6.2 64-bit"
    assert result["flavors"][0]["name"] == "m1.tiny"
    assert result["networks"][0]["name"] == "demo-net"
    assert result["volumes"] == []


def test_server_create_argv():
    captured: dict = {}

    def fake_run(ctx, args, **kwargs):  # noqa: ARG001
        captured["args"] = args
        captured["dry_run"] = kwargs.get("dry_run")
        return _run_result(dry_run=False)

    with patch.object(openstack_ops, "_run_openstack", side_effect=fake_run):
        result = openstack_ops.server_create(
            _env(),
            None,
            name="demo-1",
            image="Cirros 0.6.2 64-bit",
            flavor="m1.tiny",
            network="demo-net",
            dry_run=False,
        )
    assert result["ok"] is True
    assert captured["args"] == [
        "server",
        "create",
        "--image",
        "Cirros 0.6.2 64-bit",
        "--flavor",
        "m1.tiny",
        "--network",
        "demo-net",
        "demo-1",
    ]


def test_server_create_rejects_bad_name():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.server_create(
            _env(),
            None,
            name="; rm -rf /",
            image="cirros",
            flavor="m1.tiny",
            network="n",
            dry_run=False,
        )
    assert result["ok"] is False
    assert "invalid name" in result["error"]
    mock_run.assert_not_called()


def test_network_create_rejects_bad_cidr():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.network_create(
            _env(), None, name="demo-net", cidr="999/99", dry_run=False
        )
    assert result["ok"] is False
    assert "invalid cidr" in result["error"]
    mock_run.assert_not_called()


def test_network_create_argv():
    captured: list[list[str]] = []

    def fake_run(ctx, args, **kwargs):  # noqa: ARG001
        captured.append(args)
        return _run_result(dry_run=False)

    with patch.object(openstack_ops, "_run_openstack", side_effect=fake_run):
        result = openstack_ops.network_create(
            _env(), None, name="demo-net", cidr="10.0.0.0/24", dry_run=False
        )
    assert result["ok"] is True
    assert captured[0] == ["network", "create", "demo-net"]
    assert captured[1] == [
        "subnet",
        "create",
        "--network",
        "demo-net",
        "--subnet-range",
        "10.0.0.0/24",
        "demo-net-subnet",
    ]


def test_cloud_endpoint_returns_inventory(client, admin_headers):
    env = _create_env(client, admin_headers)
    payload = {
        "available": True,
        "source": "live",
        "error": None,
        "servers": [],
        "images": [
            {"id": VALID_ID, "name": "cirros", "status": "active", "size": None}
        ],
        "flavors": [],
        "volumes": [],
        "networks": [],
        "subnets": [],
        "routers": [],
        "floating_ips": [],
        "security_groups": [],
        "keypairs": [],
        "projects": [],
        "users": [],
    }
    with patch.object(openstack_ops, "cloud_inventory", return_value=payload):
        resp = client.get(
            f"/api/v1/environments/{env['id']}/cloud", headers=admin_headers
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["images"][0]["name"] == "cirros"


def test_cloud_server_create_endpoint(client, admin_headers):
    env = _create_env(client, admin_headers)
    with patch.object(
        openstack_ops,
        "server_create",
        return_value={
            "ok": True,
            "action": "create",
            "name": "demo-1",
            "returncode": 0,
            "message": "create accepted",
        },
    ) as mock_create:
        resp = client.post(
            f"/api/v1/environments/{env['id']}/cloud/servers",
            headers=admin_headers,
            json={
                "name": "demo-1",
                "image": "cirros",
                "flavor": "m1.tiny",
                "network": "demo-net",
            },
        )
    assert resp.status_code == 200, resp.text
    assert mock_create.called
    assert resp.json()["ok"] is True


def test_cloud_server_action_rejects_bad_uuid(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/cloud/servers/not-a-uuid/reboot",
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_cloud_server_delete_viewer_403(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    resp = client.delete(
        f"/api/v1/environments/{env['id']}/cloud/servers/{VALID_ID}",
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_cloud_mutate_viewer_403(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/cloud/servers",
        headers=viewer_headers,
        json={
            "name": "demo-1",
            "image": "cirros",
            "flavor": "m1.tiny",
            "network": "demo-net",
        },
    )
    assert resp.status_code == 403


class _FakeOSClient:
    def __init__(self, **impl):
        self.calls = []
        for name, fn in impl.items():
            setattr(self, name, fn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ARG002
        return None


def test_network_create_external_skips_subnet_when_no_cidr():
    captured: list[list[str]] = []

    def fake_run(ctx, args, **kwargs):  # noqa: ARG001
        captured.append(args)
        return _run_result(dry_run=False)

    with patch.object(openstack_ops, "_run_openstack", side_effect=fake_run):
        result = openstack_ops.network_create(
            _env(), None, name="public", cidr=None, external=True, dry_run=False
        )
    assert result["ok"] is True
    assert captured == [["network", "create", "--external", "public"]]


def test_network_create_tenant_still_requires_cidr():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.network_create(
            _env(), None, name="demo-net", cidr=None, external=False, dry_run=False
        )
    assert result["ok"] is False
    assert "invalid cidr" in result["error"]
    mock_run.assert_not_called()


def test_sg_rule_create_native_and_never_raises():
    def _create(**kwargs):
        return {"id": "rule-1", **kwargs}

    fake = _FakeOSClient(security_group_rule_create=_create)
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=fake),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        result = openstack_ops.security_group_rule_create(
            _env(),
            None,
            sg_id=VALID_ID,
            direction="ingress",
            protocol="tcp",
            port_range_min=22,
            port_range_max=22,
            remote_ip_prefix="0.0.0.0/0",
            dry_run=False,
        )
    assert result["ok"] is True
    assert result["source"] == "openstack-api"
    assert result["rule"]["protocol"] == "tcp"
    mock_run.assert_not_called()

    boom = _FakeOSClient(
        security_group_rule_create=lambda **_k: (_ for _ in ()).throw(
            OpenStackError("neutron down")
        )
    )
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=boom),
    ):
        failed = openstack_ops.security_group_rule_create(
            _env(), None, sg_id=VALID_ID, direction="ingress", dry_run=False
        )
    assert failed["ok"] is False
    assert "neutron down" in failed["error"]


def test_sg_rule_create_rejects_bad_uuid():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.security_group_rule_create(
            _env(), None, sg_id="not-a-uuid", direction="ingress", dry_run=False
        )
    assert result["ok"] is False
    assert "invalid sg_id" in result["error"]
    mock_run.assert_not_called()


def test_sg_rule_delete_rejects_bad_rule_id():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.security_group_rule_delete(
            _env(), None, sg_id=VALID_ID, rule_id="'; rm -rf /", dry_run=False
        )
    assert result["ok"] is False
    assert "invalid rule_id" in result["error"]
    mock_run.assert_not_called()


def test_router_create_and_interface_native():
    fake = _FakeOSClient(
        router_create=lambda **kw: {"id": "r1", **kw},
        router_add_interface=lambda **kw: {"port_id": "p1", **kw},
    )
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=fake),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        created = openstack_ops.router_create(
            _env(), None, name="edge", external_network=VALID_ID, dry_run=False
        )
        attached = openstack_ops.router_add_interface(
            _env(), None, router_id=VALID_ID, subnet_id=VALID_ID_2, dry_run=False
        )
    assert created["ok"] is True
    assert created["router"]["external_network"] == VALID_ID
    assert attached["ok"] is True
    assert attached["interface"]["subnet_id"] == VALID_ID_2
    mock_run.assert_not_called()


def test_router_create_dry_run_skips_kube():
    with (
        patch.object(openstack_ops, "OpenStackClient") as mock_client,
        patch.object(
            openstack_ops, "_run_openstack", return_value=_run_result(dry_run=True)
        ) as mock_run,
    ):
        result = openstack_ops.router_create(
            _env(), None, name="edge", external_network=VALID_ID, dry_run=True
        )
    mock_client.assert_not_called()
    mock_run.assert_called_once()
    assert result["ok"] is True
    assert result["dry_run"] is True


def test_quotas_update_native_and_dry_run():
    fake = _FakeOSClient(
        quotas_update=lambda **kw: {
            "compute": {"instances": 15},
            "network": {"router": 4},
        }
    )
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=fake),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        result = openstack_ops.quotas_update(
            _env(),
            None,
            compute={"instances": 15},
            network={"router": 4},
            dry_run=False,
        )
    assert result["ok"] is True
    assert result["quotas"]["compute"]["instances"] == 15
    mock_run.assert_not_called()

    with (
        patch.object(openstack_ops, "OpenStackClient") as mock_client,
        patch.object(
            openstack_ops, "_run_openstack", return_value=_run_result(dry_run=True)
        ),
    ):
        dry = openstack_ops.quotas_update(
            _env(), None, compute={"instances": 8}, dry_run=True
        )
    mock_client.assert_not_called()
    assert dry["ok"] is True
    assert dry["dry_run"] is True


def test_quotas_update_rejects_empty():
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        result = openstack_ops.quotas_update(
            _env(), None, compute={}, network={}, dry_run=False
        )
    assert result["ok"] is False
    assert "no quota fields" in result["error"]
    mock_run.assert_not_called()


def test_ops_wrappers_never_raise_on_api_error():
    boom = _FakeOSClient(
        security_group_rule_delete=lambda *_a, **_k: (_ for _ in ()).throw(
            OpenStackError("nope")
        ),
        router_create=lambda **_k: (_ for _ in ()).throw(OpenStackError("nope")),
        router_add_interface=lambda **_k: (_ for _ in ()).throw(OpenStackError("nope")),
        quotas_update=lambda **_k: (_ for _ in ()).throw(OpenStackError("nope")),
        network_create=lambda **_k: (_ for _ in ()).throw(OpenStackError("nope")),
    )
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=boom),
    ):
        results = [
            openstack_ops.security_group_rule_delete(
                _env(), None, sg_id=VALID_ID, rule_id=VALID_ID_2, dry_run=False
            ),
            openstack_ops.router_create(
                _env(), None, name="r1", external_network=VALID_ID, dry_run=False
            ),
            openstack_ops.router_add_interface(
                _env(), None, router_id=VALID_ID, subnet_id=VALID_ID_2, dry_run=False
            ),
            openstack_ops.quotas_update(
                _env(), None, compute={"instances": 2}, dry_run=False
            ),
            openstack_ops.network_create(
                _env(), None, name="public", external=True, dry_run=False
            ),
        ]
    assert all(r["ok"] is False and "nope" in (r.get("error") or "") for r in results)


def test_cloud_sg_rule_endpoints(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    with patch.object(
        openstack_ops,
        "security_group_rule_create",
        return_value={
            "ok": True,
            "action": "security_group_rule_create",
            "returncode": 0,
            "message": "accepted",
        },
    ) as mock_create:
        resp = client.post(
            f"/api/v1/environments/{env['id']}/cloud/security-groups/{VALID_ID}/rules",
            headers=admin_headers,
            json={
                "direction": "ingress",
                "protocol": "tcp",
                "port_range_min": 22,
                "port_range_max": 22,
                "remote_ip_prefix": "0.0.0.0/0",
            },
        )
    assert resp.status_code == 200, resp.text
    assert mock_create.called
    assert (
        client.post(
            f"/api/v1/environments/{env['id']}/cloud/security-groups/{VALID_ID}/rules",
            headers=viewer_headers,
            json={"direction": "ingress"},
        ).status_code
        == 403
    )
    with patch.object(
        openstack_ops,
        "security_group_rule_delete",
        return_value={
            "ok": True,
            "action": "security_group_rule_delete",
            "returncode": 0,
        },
    ):
        resp = client.delete(
            f"/api/v1/environments/{env['id']}/cloud/security-groups/{VALID_ID}/rules/{VALID_ID_2}",
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text
    bad = client.post(
        f"/api/v1/environments/{env['id']}/cloud/security-groups/not-a-uuid/rules",
        headers=admin_headers,
        json={"direction": "ingress"},
    )
    assert bad.status_code == 400


def test_cloud_router_and_quota_endpoints(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    with patch.object(
        openstack_ops,
        "router_create",
        return_value={
            "ok": True,
            "action": "router_create",
            "returncode": 0,
            "message": "accepted",
        },
    ) as mock_r:
        resp = client.post(
            f"/api/v1/environments/{env['id']}/cloud/routers",
            headers=admin_headers,
            json={"name": "edge", "external_network": VALID_ID},
        )
    assert resp.status_code == 200, resp.text
    assert mock_r.called
    with patch.object(
        openstack_ops,
        "router_add_interface",
        return_value={"ok": True, "action": "router_add_interface", "returncode": 0},
    ):
        resp = client.post(
            f"/api/v1/environments/{env['id']}/cloud/routers/{VALID_ID}/interfaces",
            headers=admin_headers,
            json={"subnet_id": VALID_ID_2},
        )
    assert resp.status_code == 200, resp.text
    with patch.object(
        openstack_ops,
        "quotas_update",
        return_value={
            "ok": True,
            "action": "quotas_update",
            "returncode": 0,
            "quotas": {"compute": {"instances": 9}},
        },
    ) as mock_q:
        resp = client.put(
            f"/api/v1/environments/{env['id']}/cloud/quotas",
            headers=admin_headers,
            json={"compute": {"instances": 9}, "network": {"router": 3}},
        )
    assert resp.status_code == 200, resp.text
    assert mock_q.called
    assert (
        client.put(
            f"/api/v1/environments/{env['id']}/cloud/quotas",
            headers=viewer_headers,
            json={"compute": {"instances": 1}},
        ).status_code
        == 403
    )
    with patch.object(
        openstack_ops,
        "network_create",
        return_value={
            "ok": True,
            "action": "network_create",
            "returncode": 0,
            "external": True,
        },
    ) as mock_net:
        resp = client.post(
            f"/api/v1/environments/{env['id']}/cloud/networks",
            headers=admin_headers,
            json={"name": "public", "external": True},
        )
    assert resp.status_code == 200, resp.text
    assert mock_net.call_args.kwargs.get("external") is True


def test_cloud_console_url_operator_only(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    url = f"/api/v1/environments/{env['id']}/cloud/servers/{VALID_ID}/console"
    assert client.get(url, headers=viewer_headers).status_code == 403
    with patch.object(
        openstack_ops,
        "server_console",
        return_value={"url": None, "error": "no console", "server_id": VALID_ID},
    ):
        resp = client.get(url, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] is None
