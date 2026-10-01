"""Actual process-pipe streaming contracts and native console authorization."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models import Environment
from app.services import native_kubernetes as source
from app.services.envcontext import EnvContext


def context():
    return EnvContext(
        environment=None,
        genestack_root=Path("/tmp"),
        config_dir=None,
        dry_run=True,
        kubeconfig="/test/only/kubeconfig",
    )


def event():
    return {
        "type": "MODIFIED",
        "object": {
            "kind": "Pod",
            "metadata": {
                "uid": "u",
                "name": "pod",
                "namespace": "ns",
                "resourceVersion": "7",
                "annotations": {"secret": "DO-NOT-SEND"},
            },
            "spec": {
                "containers": [{"env": [{"name": "SECRET", "value": "DO-NOT-SEND"}]}]
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    }


def test_watch_parser_handles_fragmented_multiline_json_and_allows_only_metadata():
    async def run():
        reader = asyncio.StreamReader()
        raw = json.dumps(event(), indent=2).encode()

        async def feed():
            for offset in range(0, len(raw), 7):
                reader.feed_data(raw[offset : offset + 7])
                await asyncio.sleep(0)
            reader.feed_eof()

        task = asyncio.create_task(feed())
        data = [
            source.watch_metadata(item, "pods")
            async for item in source.watch_objects(reader)
        ]
        await task
        assert len(data) == 1 and data[0]["object"]["status"] == "healthy"
        assert "DO-NOT-SEND" not in json.dumps(data)
        assert data[0]["object"]["resource_version"] == "7"

    asyncio.run(run())


def test_real_pipe_log_follow_redacts_orders_and_cleans(monkeypatch):
    processes = []

    async def spawn(argv, ctx):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            'print("2026-09-25T00:00:00Z password=HIDDEN"); print("ready")',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(source, "_spawn", spawn)
    released = []

    async def run():
        frames = [
            json.loads(frame[6:])
            async for frame in source.source_stream(
                ["kubectl", "logs"],
                context(),
                "env",
                resource=None,
                namespace="ns",
                pod="pod",
                authorized=lambda: True,
                release=lambda: released.append(True),
            )
            if frame.startswith("data:")
        ]
        assert [f["sequence"] for f in frames] == list(range(1, len(frames) + 1))
        assert frames[-1]["payload"]["type"] == "complete"
        lines = [
            f["payload"]["line"] for f in frames if f["payload"]["type"] == "pod_log"
        ]
        assert len(lines) == 2 and lines[-1] == "ready" and "HIDDEN" not in str(lines)
        assert len({f["epoch"] for f in frames}) == 1

    asyncio.run(run())
    assert released == [True] and processes[0].returncode == 0


def test_cancelled_client_terminates_real_child_and_releases_slot(monkeypatch):
    processes = []

    async def spawn(argv, ctx):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr(source, "_spawn", spawn)
    released = []

    async def run():
        stream = source.source_stream(
            ["kubectl"],
            context(),
            "env",
            resource="pods",
            authorized=lambda: True,
            release=lambda: released.append(True),
        )
        assert "connected" in await anext(stream)
        await stream.aclose()

    asyncio.run(run())
    assert released == [True] and processes[0].returncode is not None


def test_watch_error_does_not_echo_raw_error_or_spec():
    with pytest.raises(ValueError, match="Kubernetes watch ended"):
        source.watch_metadata(
            {"type": "ERROR", "object": {"message": "SECRET"}}, "pods"
        )


def make_env(client, admin_headers, kubeconfig=False):
    result = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "live-" + uuid.uuid4().hex},
    )
    assert result.status_code == 201
    eid = result.json()["id"]
    if kubeconfig:
        with SessionLocal() as db:
            env = db.get(Environment, eid)
            env.kubeconfig_path = "/test/only/kubeconfig"
            db.commit()
    return eid


def test_stream_routes_auth_allowlist_missing_configuration_and_ticket_reuse(
    client, admin_headers, monkeypatch
):
    eid = make_env(client, admin_headers)
    path = f"/api/v1/environments/{eid}/native/kubernetes/watch"
    assert client.get(path).status_code == 401
    assert (
        client.get(path + "?resource=secrets", headers=admin_headers).status_code == 422
    )
    monkeypatch.setattr(
        "app.routers.native_kubernetes.shutil.which", lambda _: "/test/kubectl"
    )
    assert client.get(path, headers=admin_headers).status_code == 409
    ticket = client.post("/api/v1/auth/ticket", headers=admin_headers).json()["ticket"]
    assert client.get(path + "?ticket=" + ticket).status_code == 409
    assert client.get(path + "?ticket=" + ticket).status_code == 401


def test_console_descriptor_never_returns_upstream_tokens(
    client, admin_headers, monkeypatch
):
    eid = make_env(client, admin_headers)
    monkeypatch.setattr(
        "app.routers.native_consoles.novnc.create_console_session",
        lambda **_: {
            "ok": True,
            "session_id": "nvc_test-session",
            "embed_url": "https://bmc/?token=SECRET",
        },
    )
    response = client.post(
        f"/api/v1/environments/{eid}/native/consoles/cloud/server",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["frame_encoding"] == "rfb" and "SECRET" not in response.text
    assert not response.json()["decoded_images"]
    assert (
        client.post(
            f"/api/v1/environments/{eid}/native/consoles/cloud/server",
            headers={"X-API-Key": "dev-viewer-key"},
        ).status_code
        == 403
    )


def test_shared_ilo_descriptor_is_binary_not_fake_images(
    client, admin_headers, monkeypatch
):
    eid = make_env(client, admin_headers)
    monkeypatch.setattr(
        "app.routers.native_consoles.ilo_service.open_console_session",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        "app.routers.native_consoles.ilo_console.create_ilo_console_session",
        lambda **_: {
            "ok": True,
            "session_id": "ilo_test-session",
            "embed_url": "https://bmc/?token=SECRET",
        },
    )
    response = client.post(
        f"/api/v1/environments/{eid}/native/consoles/baremetal/node",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["frame_encoding"] == "hpe-ilo-dvc"
    assert response.json()["channels"] == [1, 2]
    assert "SECRET" not in response.text and not response.json()["decoded_images"]


def test_stream_cross_tenant_denied_before_source_spawn(
    client, admin_headers, monkeypatch
):
    from tests.test_tenants import (
        _create_env,
        _create_tenant,
        _create_user,
        _login_headers,
    )

    tenant = _create_tenant(client, admin_headers)
    owned = _create_env(client, admin_headers, tenant_id=tenant["id"])
    other = _create_env(client, admin_headers)
    user = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant["id"], "role": "viewer"}],
    )
    headers = _login_headers(client, user["username"])
    monkeypatch.setattr("app.routers.native_kubernetes.shutil.which", lambda _: None)
    assert (
        client.get(
            f"/api/v1/environments/{owned['id']}/native/kubernetes/watch",
            headers=headers,
        ).status_code
        == 503
    )
    assert (
        client.get(
            f"/api/v1/environments/{other['id']}/native/kubernetes/watch",
            headers=headers,
        ).status_code
        == 403
    )


def test_real_watch_process_streams_metadata_and_raw_failures_are_hidden(monkeypatch):
    async def spawn(argv, ctx):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; print("
            + repr(json.dumps(event()))
            + '); sys.stderr.write("password=SECRET"); sys.exit(1)',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    monkeypatch.setattr(source, "_spawn", spawn)

    async def run():
        frames = [
            json.loads(frame[6:])
            async for frame in source.source_stream(
                ["kubectl"],
                context(),
                "env",
                resource="pods",
                authorized=lambda: True,
                release=lambda: None,
            )
            if frame.startswith("data:")
        ]
        assert frames[1]["payload"]["type"] == "kubernetes_watch"
        assert frames[-1]["payload"]["reason"] == "source_unavailable"
        assert "SECRET" not in str(frames) and "DO-NOT-SEND" not in str(frames)

    asyncio.run(run())


def test_watch_object_limit_and_log_line_limit(monkeypatch):
    monkeypatch.setattr(source, "MAX_OBJECT_BYTES", 16)
    monkeypatch.setattr(source, "MAX_LINE_BYTES", 16)

    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"oversize":"' + b"x" * 20 + b'"}')
        reader.feed_eof()
        with pytest.raises(ValueError, match="limit"):
            await anext(source.watch_objects(reader))
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 20 + b"\n")
        reader.feed_eof()
        with pytest.raises(ValueError, match="limit"):
            await anext(source.log_lines(reader))

    asyncio.run(run())


def test_malformed_watch_metadata_cannot_serialize_nested_secret_values():
    row = event()
    row["object"]["metadata"]["name"] = {"password": "SECRET"}
    row["object"]["status"]["conditions"] = True
    data = source.watch_metadata(row, "pods")
    assert data["object"]["name"] == "" and data["object"]["status"] == "unknown"
    assert "SECRET" not in str(data)
    assert source.watch_metadata({"type": {"secret": "SECRET"}}, "pods") is None


def test_json_log_secret_keys_are_redacted_with_timestamps():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'2026-09-25T00:00:00Z {"password":"SECRET", "access_token":"TOKENVALUE", "message":"ready"}\n'
        )
        reader.feed_eof()
        line = await anext(source.log_lines(reader))
        assert "SECRET" not in line and "TOKENVALUE" not in line and "ready" in line

    asyncio.run(run())
