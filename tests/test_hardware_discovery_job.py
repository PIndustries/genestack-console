"""Discover from an empty environment using normal scoped APIs and jobs."""

from tests.test_baremetal import _create_env, _make_node, _submit


def test_discovery_job_records_readonly_hardware(client, admin_headers, monkeypatch):
    from app.modules.baremetal import hardware_discover

    env = _create_env(client, admin_headers, dry_run=False)
    _make_node(env["id"], name="arbitrary-name")
    calls = []

    def discover(host, username, password, **kwargs):
        kwargs["check"]()
        calls.append((host, username))
        assert password == "calvin"
        return {
            "source": "redfish",
            "cpu_threads": 32,
            "memory_gib": 128,
            "drives": [],
            "nics": [],
            "hardware_modified": False,
        }

    monkeypatch.setattr(hardware_discover, "hardware_inventory", discover)
    job = _submit(client, admin_headers, env["id"], "baremetal.hardware.discover")
    assert job["status"] == "success", job.get("error")
    assert len(calls) == 1
    nodes = client.get(f"/api/v1/environments/{env['id']}/baremetal", headers=admin_headers).json()[
        "nodes"
    ]
    assert nodes[0]["hardware"]["cpu_threads"] == 32
    assert nodes[0]["next_boot"] == "disk"
    assert nodes[0]["wiped_at"] is None
    assert "calvin" not in repr(nodes)


def test_discovery_empty_environment_explains_next_step(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(client, admin_headers, env["id"], "baremetal.hardware.discover")
    assert job["status"] == "failed"
    assert "Register" in (job.get("error") or job.get("log_text") or "")


def test_discovery_dry_run_does_not_contact_hardware(client, admin_headers, monkeypatch):
    from app.modules.baremetal import hardware_discover

    env = _create_env(client, admin_headers, dry_run=True)
    _make_node(env["id"])
    monkeypatch.setattr(
        hardware_discover,
        "hardware_inventory",
        lambda *a: (_ for _ in ()).throw(AssertionError("Contacted hardware")),
    )
    job = _submit(client, admin_headers, env["id"], "baremetal.hardware.discover")
    assert job["status"] == "success"
    assert "would read hardware" in job["log_text"]


def test_viewer_cannot_start_discovery(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    response = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=viewer_headers,
        json={"operation": "baremetal.hardware.discover", "params": {}},
    )
    assert response.status_code == 403
