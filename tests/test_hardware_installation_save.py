"""Saving a reviewed proposal is distinct from executing an installation."""

from datetime import UTC, datetime

import pytest
import yaml

from app.db import SessionLocal
from app.models import BaremetalNode
from tests.test_hardware_installation import fixture


def stored_machine(client, headers):
    from tests.test_baremetal import _create_env

    node, doc = fixture()
    doc["pxe"].update(
        interface="fixture0", range_start="10.77.27.191", range_end="10.77.27.254", http_port=8088
    )
    env = _create_env(client, headers, dry_run=False)
    base = f"/api/v1/environments/{env['id']}"
    response = client.put(
        base + "/config",
        headers=headers,
        json={"yaml_text": yaml.safe_dump(doc), "expected_version": 0},
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        row = BaremetalNode(
            environment_id=env["id"],
            name=node["name"],
            bmc_host="https://fixture.invalid",
            bmc_username="fixture",
            bmc_password="not-a-real-credential",
            expected_ip=node["ip"],
            pxe_mac=node["mac"],
            hardware_report=node["hardware"],
            os_inventory_report=node["os_inventory"],
            next_boot="disk",
            boot_stage="inspected",
        )
        db.add(row)
        db.commit()
        identity = row.id
    plan = client.get(base + "/baremetal/installation-plan", headers=headers).json()
    assert plan["blockers"] == [], plan
    return env, base, identity, plan


def test_saving_and_retrying_review_preserves_config_and_never_starts_hardware(
    client, admin_headers
):
    env, base, identity, plan = stored_machine(client, admin_headers)
    before = yaml.safe_load(client.get(base + "/config", headers=admin_headers).json()["yaml"])
    body = {"expected_version": plan["config_version"], "fingerprint": plan["fingerprint"]}
    for _ in range(2):
        response = client.put(base + "/config/installation-plan", headers=admin_headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "version": 2,
            "plan_saved": True,
            "installation_started": False,
            "hardware_modified": False,
        }
    current = client.get(base + "/config", headers=admin_headers).json()
    assert current["version"] == 2
    saved = yaml.safe_load(current["yaml"])
    record = saved.pop("hardware_installation")
    assert saved == before
    assert record["execution_authorized"] is False
    assert record["reviewed_fingerprint"] == plan["fingerprint"]
    reopened = client.get(base + "/baremetal/installation-plan", headers=admin_headers).json()
    assert reopened["saved_plan_matches"] is True
    assert (
        client.get(
            "/api/v1/jobs", params={"environment_id": env["id"]}, headers=admin_headers
        ).json()
        == []
    )
    with SessionLocal() as db:
        node = db.get(BaremetalNode, identity)
        assert node.next_boot == "disk" and node.boot_stage == "inspected"
        assert (
            node.wiped_at is None
            and not node.commission_wipe_wwids
            and node.commission_token is None
        )


def test_hardware_change_invalidates_old_review_and_retry(client, admin_headers):
    _, base, identity, plan = stored_machine(client, admin_headers)
    body = {"expected_version": plan["config_version"], "fingerprint": plan["fingerprint"]}
    assert (
        client.put(base + "/config/installation-plan", headers=admin_headers, json=body).status_code
        == 200
    )
    with SessionLocal() as db:
        node = db.get(BaremetalNode, identity)
        node.hardware_report = {
            **node.hardware_report,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        db.commit()
    assert (
        client.get(base + "/baremetal/installation-plan", headers=admin_headers).json()[
            "saved_plan_matches"
        ]
        is False
    )
    assert (
        client.put(base + "/config/installation-plan", headers=admin_headers, json=body).status_code
        == 409
    )
    assert client.get(base + "/config", headers=admin_headers).json()["version"] == 2


def test_viewer_cannot_save_review(client, admin_headers, viewer_headers):
    _, base, _, plan = stored_machine(client, admin_headers)
    response = client.put(
        base + "/config/installation-plan",
        headers=viewer_headers,
        json={"expected_version": plan["config_version"], "fingerprint": plan["fingerprint"]},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"fingerprint": "a" * 64},
        {"fingerprint": "bad", "expected_version": 1},
        {"fingerprint": "a" * 64, "expected_version": -1},
    ],
)
def test_review_save_requires_an_explicit_valid_snapshot(client, admin_headers, body):
    _, base, _, _ = stored_machine(client, admin_headers)
    assert (
        client.put(base + "/config/installation-plan", headers=admin_headers, json=body).status_code
        == 422
    )
    assert client.get(base + "/config", headers=admin_headers).json()["version"] == 1


def test_later_config_edit_invalidates_a_saved_review(client, admin_headers):
    _, base, _, plan = stored_machine(client, admin_headers)
    body = {"expected_version": plan["config_version"], "fingerprint": plan["fingerprint"]}
    assert (
        client.put(base + "/config/installation-plan", headers=admin_headers, json=body).status_code
        == 200
    )
    doc = yaml.safe_load(client.get(base + "/config", headers=admin_headers).json()["yaml"])
    doc["components"] = {"keystone": True}
    assert (
        client.put(
            base + "/config",
            headers=admin_headers,
            json={"yaml_text": yaml.safe_dump(doc), "expected_version": 2},
        ).status_code
        == 201
    )
    assert (
        client.get(base + "/baremetal/installation-plan", headers=admin_headers).json()[
            "saved_plan_matches"
        ]
        is False
    )
    assert (
        client.put(base + "/config/installation-plan", headers=admin_headers, json=body).status_code
        == 409
    )
    assert client.get(base + "/config", headers=admin_headers).json()["version"] == 3
