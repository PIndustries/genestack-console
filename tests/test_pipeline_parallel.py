"""genestack.deploy parallel OpenStack services stage tests.

``parallelism`` (job param, default 1, clamped 1..16) fans the items of the
stage that installs keystone out across a thread pool — keystone first.
All other stages stay sequential; dry runs and parallelism=1 keep the
historical sequential loop byte-for-byte.
"""

from __future__ import annotations

import uuid

from app.services import deploy as deploy_service
from app.services.service_registry import PIPELINE_STAGES
from tests.test_deploy import (
    _capture_commands,
    _create_env,
    _deploy_job,
    _job_log,
    _put_doc,
)

# The default doc in test_deploy only enables keystone; fan-out needs the
# stage's other services on, so these tests pin the components explicitly.
DOC = """\
provider: kubespray
components:
  keystone: true
  placement: true
  glance: true
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _expected_sequential_commands() -> list[list[str]]:
    """Every pipeline item in strict stage/item order (kubespray provider)."""
    expected: list[list[str]] = []
    for stage in PIPELINE_STAGES:
        # Testing stage (tempest) is not part of regular deploy
        if stage["id"] == "testing":
            continue
        for item in stage["items"]:
            expected.append(["bash", item["script"]])
    return expected


# A non-dry-run deploy ends with the phase-3 credentials setup script.
CREDENTIALS_SCRIPT = ["bash", "bin/setup-openstack-rc.sh"]


def _full_expected() -> list[list[str]]:
    return _expected_sequential_commands() + [CREDENTIALS_SCRIPT]


CORE_SCRIPTS = [
    ["bash", "bin/install-keystone.sh"],
    ["bash", "bin/install-placement.sh"],
    ["bash", "bin/install-glance.sh"],
]


def _scripts_index(captured: list[list[str]]) -> dict[str, int]:
    return {argv[1]: i for i, argv in enumerate(captured) if len(argv) > 1}


def test_effective_parallelism_clamps():
    assert deploy_service._effective_parallelism(None) == 1
    assert deploy_service._effective_parallelism(1) == 1
    assert deploy_service._effective_parallelism(0) == 1
    assert deploy_service._effective_parallelism(-3) == 1
    assert deploy_service._effective_parallelism(4) == 4
    assert deploy_service._effective_parallelism(16) == 16
    assert deploy_service._effective_parallelism(99) == 16
    assert deploy_service._effective_parallelism("8") == 8
    assert deploy_service._effective_parallelism("junk") == 1


def test_default_deploy_is_sequential(client, admin_headers, tmp_path, monkeypatch):
    """No parallelism param: commands run in strict pipeline order."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")

    assert captured == _full_expected()
    log_text = _job_log(client, admin_headers, job["id"])
    assert "parallelism=" not in log_text
    assert "keystone first" not in log_text


def test_parallelism_one_is_sequential(client, admin_headers, tmp_path, monkeypatch):
    """parallelism=1: identical command order to the default run."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 1})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")

    assert captured == _full_expected()
    log_text = _job_log(client, admin_headers, job["id"])
    assert "keystone first" not in log_text


def test_parallel_keystone_first_then_services(
    client, admin_headers, tmp_path, monkeypatch
):
    """parallelism>1: keystone runs before the other services in its stage;
    the deploy still succeeds and covers every item exactly once."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 4})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")

    idx = _scripts_index(captured)
    assert idx["bin/install-keystone.sh"] < idx["bin/install-placement.sh"]
    assert idx["bin/install-keystone.sh"] < idx["bin/install-glance.sh"]
    # Every item ran exactly once.
    assert sorted(captured) == sorted(_full_expected())
    # Stages outside the services stage kept their exact order.
    pre = [a for a in captured if a not in CORE_SCRIPTS]
    expected_pre = [c for c in _full_expected() if c not in CORE_SCRIPTS]
    assert pre == expected_pre

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] parallelism=4 for the OpenStack services stage" in log_text
    assert "keystone first, then 2 item(s) across 4 worker(s)" in log_text


def test_parallel_failure_stops_pipeline(client, admin_headers, tmp_path, monkeypatch):
    """A failing item in the services stage fails the deploy with the same
    stage/item shape the sequential path uses; later stages never run."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch, rc_for={"bin/install-placement.sh": 1})

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 4})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "deploy failed at stage 'core' item 'placement' (rc=1)" in (
        job.get("error") or ""
    ), job

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] FAILED at core/placement rc=1" in log_text
    # Stages after the services stage did not start.
    assert ["bash", "bin/install-nova.sh"] not in captured
    assert ["bash", "bin/setup-openstack-rc.sh"] not in captured


def test_sequential_failure_same_semantics(
    client, admin_headers, tmp_path, monkeypatch
):
    """parallelism=1 with the same failing item: identical failure surface."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch, rc_for={"bin/install-placement.sh": 1})

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 1})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "deploy failed at stage 'core' item 'placement' (rc=1)" in (
        job.get("error") or ""
    ), job

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] FAILED at core/placement rc=1 — stopping pipeline" in log_text
    assert ["bash", "bin/install-nova.sh"] not in captured
    assert ["bash", "bin/setup-openstack-rc.sh"] not in captured


def test_dry_run_deploy_stays_sequential_with_parallelism(
    client, admin_headers, tmp_path, monkeypatch
):
    """A dry run never takes the pool path: strict order, no fan-out logs."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 8})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")

    assert captured == _expected_sequential_commands()
    log_text = _job_log(client, admin_headers, job["id"])
    assert "keystone first" not in log_text
    # Credentials phase is dry: logged, not captured.
    assert ["bash", "bin/setup-openstack-rc.sh"] not in captured


def test_out_of_range_parallelism_is_clamped_not_rejected(
    client, admin_headers, tmp_path, monkeypatch
):
    """parallelism=99 clamps to 16 and runs successfully."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC)
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"], params={"parallelism": 99})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] parallelism=16 for the OpenStack services stage" in log_text
    idx = _scripts_index(captured)
    assert idx["bin/install-keystone.sh"] < idx["bin/install-placement.sh"]
