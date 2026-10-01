"""Reconcile engine tests: plan diff, apply semantics, guard rails, dispatch."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import yaml

from app.config import get_settings
from app.db import SessionLocal
from app.models import Environment
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services import reconcile
from app.services.catalog import get_operation, mutating_operation_ids


@pytest.fixture(autouse=True)
def _ensure_tables(client):  # noqa: ARG001 — app lifespan creates the DB schema
    yield


def _make_env(db, components=None, dry_run=None, with_doc=True) -> Environment:
    env = Environment(name=f"recon-{uuid.uuid4().hex[:8]}", dry_run=dry_run)
    db.add(env)
    db.flush()
    if with_doc:
        doc = {}
        if components is not None:
            doc["components"] = components
        envconfig_service.put_version(db, env, yaml.safe_dump(doc), "test")
    db.commit()
    return env


def _rel(name: str, namespace: str = "openstack", status: str = "deployed") -> dict:
    return {
        "name": name,
        "namespace": namespace,
        "status": status,
        "chart": f"{name}-1.0.0",
    }


def _ok_result(**over) -> dict:
    result = {
        "ok": True,
        "returncode": 0,
        "dry_run": False,
        "stdout": "",
        "stderr": "",
        "message": "ok",
    }
    result.update(over)
    return result


def _action(plan: dict, component: str) -> dict:
    return next(a for a in plan["actions"] if a["component"] == component)


# ---------------------------------------------------------------------------
# release_for_component mapping
# ---------------------------------------------------------------------------


def test_release_for_component_identity_default():
    assert reconcile.release_for_component("nova") == "nova"
    assert reconcile.release_for_component("mariadb-operator") == "mariadb-operator"


def test_release_for_component_unknown_for_invalid_names():
    # Not a valid helm/service name -> no determinable mapping.
    assert reconcile.release_for_component("heat_api") is None
    assert reconcile.release_for_component("bad name!") is None


def test_release_for_component_overrides_win():
    overrides = {"cinder": "cinder-volume", "custom": None}
    assert (
        reconcile.release_for_component("cinder", overrides=overrides)
        == "cinder-volume"
    )
    # Explicit None marks the component unmappable.
    assert reconcile.release_for_component("custom", overrides=overrides) is None
    # Non-overridden components fall through to identity.
    assert reconcile.release_for_component("glance", overrides=overrides) == "glance"


# ---------------------------------------------------------------------------
# plan_reconcile: pure diff
# ---------------------------------------------------------------------------


def test_plan_enable_when_desired_but_not_deployed():
    db = SessionLocal()
    try:
        env = _make_env(db, {"cinder": True})
        plan = reconcile.plan_reconcile(db, env, get_settings(), releases=[])
    finally:
        db.close()
    assert plan["summary"]["to_enable"] == 1
    action = _action(plan, "cinder")
    assert action["action"] == "enable"
    assert action["desired"] is True
    assert action["deployed"] is False
    assert action["release"] == "cinder"


def test_plan_disable_when_deployed_but_not_desired():
    db = SessionLocal()
    try:
        env = _make_env(db, {"heat": False})
        plan = reconcile.plan_reconcile(
            db, env, get_settings(), releases=[_rel("heat")]
        )
    finally:
        db.close()
    assert plan["summary"]["to_disable"] == 1
    action = _action(plan, "heat")
    assert action["action"] == "disable"
    assert action["deployed"] is True
    assert action["namespace"] == "openstack"


def test_plan_in_sync_both_directions():
    db = SessionLocal()
    try:
        env = _make_env(db, {"keystone": True, "cinder": False})
        plan = reconcile.plan_reconcile(
            db, env, get_settings(), releases=[_rel("keystone")]
        )
    finally:
        db.close()
    assert plan["summary"]["in_sync"] == 2
    assert _action(plan, "keystone")["reason"] == "in sync"
    assert _action(plan, "cinder")["reason"] == "absent as desired"


def test_plan_unknown_mapping():
    db = SessionLocal()
    try:
        env = _make_env(db, {"heat_api": True})
        plan = reconcile.plan_reconcile(db, env, get_settings(), releases=[])
    finally:
        db.close()
    assert plan["summary"]["unknown"] == 1
    action = _action(plan, "heat_api")
    assert action["action"] == "none"
    assert action["release"] is None
    assert "unknown" in action["reason"]


def test_plan_missing_components_block_plans_nothing():
    db = SessionLocal()
    try:
        env_no_block = _make_env(db, components=None)  # doc exists, no components:
        env_no_doc = _make_env(db, with_doc=False)
        settings = get_settings()
        plan_no_block = reconcile.plan_reconcile(
            db, env_no_block, settings, releases=[]
        )
        plan_no_doc = reconcile.plan_reconcile(db, env_no_doc, settings, releases=[])
    finally:
        db.close()
    for plan in (plan_no_block, plan_no_doc):
        assert plan["actions"] == []
        assert plan["note"]
        assert plan["summary"] == {
            "to_enable": 0,
            "to_disable": 0,
            "in_sync": 0,
            "unknown": 0,
        }
    assert "components" in plan_no_block["note"]
    assert "no config document" in plan_no_doc["note"]


def test_plan_release_mapping_override():
    db = SessionLocal()
    try:
        env = _make_env(db, {"cinder": True})
        settings = get_settings()
        # Deployed under a different release name; override resolves it.
        plan = reconcile.plan_reconcile(
            db,
            env,
            settings,
            releases=[_rel("cinder-volume")],
            release_map={"cinder": "cinder-volume"},
        )
        assert _action(plan, "cinder")["action"] == "none"
        assert plan["summary"]["in_sync"] == 1
        # Override to None -> unknown, even though a release exists.
        plan = reconcile.plan_reconcile(
            db, env, settings, releases=[_rel("cinder")], release_map={"cinder": None}
        )
        assert plan["summary"]["unknown"] == 1
    finally:
        db.close()


def test_plan_ignores_uninstalling_releases():
    db = SessionLocal()
    try:
        env = _make_env(db, {"cinder": False})
        plan = reconcile.plan_reconcile(
            db, env, get_settings(), releases=[_rel("cinder", status="uninstalling")]
        )
    finally:
        db.close()
    # An uninstalling release no longer counts as deployed.
    assert _action(plan, "cinder")["deployed"] is False
    assert plan["summary"]["in_sync"] == 1


# ---------------------------------------------------------------------------
# run_reconcile: plan-only vs apply, guard rails
# ---------------------------------------------------------------------------


def test_run_plan_only_executes_nothing():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"cinder": True, "heat": False}, dry_run=False)
        with (
            patch.object(
                reconcile, "_fetch_releases", return_value=([_rel("heat")], None)
            ),
            patch.object(bridge, "enable_service") as enable_spy,
            patch.object(bridge, "run_command") as cmd_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=False, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is True
    assert result["applied"] == 0
    assert result["dry_run"] is True
    assert result["summary"]["to_enable"] == 1
    assert result["summary"]["to_disable"] == 1
    enable_spy.assert_not_called()
    cmd_spy.assert_not_called()
    assert any("DRY PLAN — re-run with apply=true to execute" in line for line in logs)
    assert any("cinder" in line and "enable" in line for line in logs)


def test_run_apply_enables_and_disables():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"cinder": True, "heat": False}, dry_run=False)
        with (
            patch.object(
                reconcile, "_fetch_releases", return_value=([_rel("heat")], None)
            ),
            patch.object(
                bridge, "enable_service", return_value=_ok_result()
            ) as enable_spy,
            patch.object(bridge, "run_command", return_value=_ok_result()) as cmd_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=True, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is True
    assert result["applied"] == 2
    # Enable goes through the exact genestack.service.enable code path.
    assert enable_spy.call_count == 1
    assert enable_spy.call_args.args[0] == "cinder"
    assert enable_spy.call_args.kwargs["dry_run"] is False
    # Disable is helm uninstall of the deployed release in its namespace.
    assert cmd_spy.call_count == 1
    assert cmd_spy.call_args.args[0] == ["helm", "uninstall", "heat", "-n", "openstack"]
    assert cmd_spy.call_args.kwargs["dry_run"] is False


def test_run_apply_refuses_protected_components():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"keystone": False, "heat": False}, dry_run=False)
        with (
            patch.object(
                reconcile,
                "_fetch_releases",
                return_value=([_rel("keystone"), _rel("heat")], None),
            ),
            patch.object(bridge, "enable_service") as enable_spy,
            patch.object(bridge, "run_command", return_value=_ok_result()) as cmd_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=True, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is True
    # keystone refused, heat uninstalled: only one helm call.
    assert cmd_spy.call_count == 1
    assert cmd_spy.call_args.args[0] == ["helm", "uninstall", "heat", "-n", "openstack"]
    enable_spy.assert_not_called()
    assert any("refused: protected component" in line for line in logs)
    refused = [r for r in result["results"] if r.get("refused")]
    assert [r["component"] for r in refused] == ["keystone"]
    assert result["applied"] == 1


def test_run_apply_stops_on_first_failure():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"cinder": True, "glance": True}, dry_run=False)
        with (
            patch.object(reconcile, "_fetch_releases", return_value=([], None)),
            patch.object(
                bridge,
                "enable_service",
                return_value=_ok_result(ok=False, returncode=1, message="boom"),
            ) as enable_spy,
            patch.object(bridge, "run_command") as cmd_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=True, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is False
    assert result["applied"] == 0
    assert "cinder" in result["error"]
    # First enable failed -> glance never attempted.
    assert enable_spy.call_count == 1
    cmd_spy.assert_not_called()
    assert any("stopping" in line for line in logs)


def test_run_dry_run_env_forces_plan_only():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"cinder": True}, dry_run=True)
        with (
            patch.object(reconcile, "_fetch_releases", return_value=([], None)),
            patch.object(bridge, "enable_service") as enable_spy,
            patch.object(bridge, "run_command") as cmd_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=True, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["applied"] == 0
    enable_spy.assert_not_called()
    cmd_spy.assert_not_called()
    assert any("forcing plan-only" in line for line in logs)


def test_run_apply_refuses_when_helm_probe_failed():
    db = SessionLocal()
    logs: list[str] = []
    try:
        env = _make_env(db, {"cinder": True}, dry_run=False)
        with (
            patch.object(
                reconcile,
                "_fetch_releases",
                return_value=([], "helm not found on PATH"),
            ),
            patch.object(bridge, "enable_service") as enable_spy,
        ):
            result = reconcile.run_reconcile(
                db, env, get_settings(), apply=True, log=logs.append
            )
    finally:
        db.close()
    assert result["ok"] is False
    assert "helm list failed" in result["error"]
    enable_spy.assert_not_called()


def test_run_requires_environment():
    result = reconcile.run_reconcile(None, None, get_settings(), apply=False)
    assert result["ok"] is False
    assert result["returncode"] == 2


def test_summary_math_mixed_plan():
    db = SessionLocal()
    try:
        env = _make_env(
            db,
            {"cinder": True, "heat": False, "keystone": True, "heat_api": True},
        )
        plan = reconcile.plan_reconcile(
            db, env, get_settings(), releases=[_rel("heat"), _rel("keystone")]
        )
    finally:
        db.close()
    assert plan["summary"] == {
        "to_enable": 1,
        "to_disable": 1,
        "in_sync": 1,
        "unknown": 1,
    }


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


def test_catalog_registers_reconcile_op():
    op = get_operation("genestack.components.reconcile")
    assert op is not None
    assert op.mutating is True
    assert op.required_role == "operator"
    assert op.backend == "genestack"
    assert op.timeout_seconds == 1800
    assert op.handler == "genestack_components_reconcile"
    assert "genestack.components.reconcile" in mutating_operation_ids()
    apply_param = next(p for p in op.params if p.name == "apply")
    assert apply_param.required is False
    assert apply_param.type == "boolean"
    assert "plan-only" in op.description


# ---------------------------------------------------------------------------
# Dispatch through the job machinery (global dry_run => plan-only)
# ---------------------------------------------------------------------------


def _submit_reconcile_job(client, headers, env_id, params):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.components.reconcile",
            "params": params,
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_job_reconcile_plan_only_dispatches(client, operator_headers):
    db = SessionLocal()
    try:
        env = _make_env(db, {"cinder": True, "heat": False})
        env_id = env.id
    finally:
        db.close()
    with (
        patch.object(reconcile, "_fetch_releases", return_value=([_rel("heat")], None)),
        patch.object(bridge, "enable_service") as enable_spy,
        patch.object(bridge, "run_command") as cmd_spy,
    ):
        job = _submit_reconcile_job(client, operator_headers, env_id, {})
    assert job["status"] == "success", job
    assert "genestack_components_reconcile" in job["log_text"]
    assert "cinder" in job["log_text"]
    assert "heat" in job["log_text"]
    assert "DRY PLAN" in job["log_text"]
    enable_spy.assert_not_called()
    cmd_spy.assert_not_called()


def test_job_reconcile_apply_is_plan_only_under_global_dry_run(
    client, operator_headers
):
    # Test config sets global dry_run=true: apply=true still executes nothing.
    db = SessionLocal()
    try:
        env = _make_env(db, {"cinder": True})
        env_id = env.id
    finally:
        db.close()
    with (
        patch.object(reconcile, "_fetch_releases", return_value=([], None)),
        patch.object(bridge, "enable_service") as enable_spy,
    ):
        job = _submit_reconcile_job(client, operator_headers, env_id, {"apply": True})
    assert job["status"] == "success", job
    assert "forcing plan-only" in job["log_text"]
    enable_spy.assert_not_called()
