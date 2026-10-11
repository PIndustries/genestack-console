"""Intake plans: operations, params, refuses, and the hardware-not-run report."""

from __future__ import annotations

import json

import pytest

from app.services.build_matrix import ROWS, plan, report_rows, write_report


def test_outside_iso():
    result = plan("outside-iso")
    assert [job["operation"] for job in result["jobs"]] == [
        "record-machine",
        "genestack.talos.bootstrap",
    ]
    assert "params" not in result["jobs"][0]
    assert result["jobs"][1]["params"] == {}
    assert result["refuses"] == ["power", "pxe-restart"]
    assert result["note"] == (
        "The ISO is fetched from the given URL. The console does not serve it "
        "and does not restart DHCP."
    )


def test_console_pxe():
    result = plan("console-pxe")
    assert [job["operation"] for job in result["jobs"]] == ["baremetal.node.next_boot"]
    assert result["jobs"][0]["params"] == {"next_boot": "talos", "boot_now": True}
    assert result["refuses"] == ["virtual-cd"]
    assert result["note"] == "Boot files come from this console."


def test_virtual_cd():
    result = plan("virtual-cd")
    assert [job["operation"] for job in result["jobs"]] == ["baremetal.node.iso_boot"]
    assert result["jobs"][0]["params"] == {
        "image_url": "http://image.example/metal-amd64.iso"
    }
    assert result["refuses"] == ["vendor-path"]
    assert result["note"] == (
        "The job mounts that ISO and power-cycles the machine when it runs. "
        "iLO and iDRAC use the same generic InsertMedia path. "
        "This test does not call a BMC."
    )


def test_pxe_no_bmc():
    result = plan("pxe-no-bmc")
    assert [job["operation"] for job in result["jobs"]] == ["genestack.talos.bootstrap"]
    assert result["jobs"][0]["params"] == {}
    assert result["jobs"][0]["user_step"] == "boot-installer"
    assert result["refuses"] == ["power"]
    assert result["note"] == "No management port. The job names the one step and waits."


def test_user_ubuntu():
    result = plan("user-ubuntu")
    assert [job["operation"] for job in result["jobs"]] == [
        "record-ubuntu",
        "ssh-check",
        "genestack.deploy",
    ]
    assert "params" not in result["jobs"][0]
    assert "params" not in result["jobs"][1]
    assert result["jobs"][2]["params"] == {"parallelism": 2}
    assert result["refuses"] == ["reboot"]
    assert result["note"] == (
        "Already have an OS records the row. Deploy over SSH runs after SSH answers."
    )


def test_existing_talos():
    result = plan("existing-talos")
    assert [job["operation"] for job in result["jobs"]] == ["vault.put"]
    assert result["jobs"][0]["params"] == {"name": "talosconfig"}
    assert result["refuses"] == ["replace", "reboot"]
    assert result["note"] == "A pasted talosconfig matches the nodes. Replace stays off."


def test_existing_k8s():
    result = plan("existing-k8s")
    assert [job["operation"] for job in result["jobs"]] == ["hosts.kubespray.adopt"]
    assert result["jobs"][0]["params"] == {"kubeconfig": True}
    assert result["refuses"] == ["genestack.deploy", "ansible"]
    assert result["note"] == "The kubeconfig is stored. Deploy stays off until asked."


def test_flip_talos_ubuntu():
    result = plan("flip-talos-ubuntu")
    assert [job["operation"] for job in result["jobs"]] == [
        "genestack.greenfield",
        "hosts.ubuntu.bringup",
        "genestack.deploy",
    ]
    assert result["jobs"][0]["params"] == {"stop_after": "talos", "parallelism": 2}
    assert result["jobs"][1]["params"] == {"leave_hostnames": ["node-1"]}
    assert result["jobs"][2]["params"] == {"parallelism": 2}
    assert all("dry_run" not in job for job in result["jobs"])
    assert result["refuses"] == ["unnamed-replace"]
    assert result["note"] == (
        "Greenfield stops after Talos. Ubuntu names the hosts that leave Talos. "
        "Deploy then runs host setup."
    )


def test_look_around():
    result = plan("look-around")
    flip = plan("flip-talos-ubuntu")
    assert [job["operation"] for job in result["jobs"]] == [
        job["operation"] for job in flip["jobs"]
    ]
    for job, base in zip(result["jobs"], flip["jobs"], strict=True):
        assert job["params"] == base["params"]
        assert job["dry_run"] is True
    assert result["refuses"] == ["power", "ssh"]
    assert result["note"] == "Look around prints the list and does not power or SSH."


def test_report_marks_hardware_not_run():
    rows = report_rows()
    assert [row["id"] for row in rows] == ROWS
    assert len(ROWS) == 9
    for row in rows:
        assert row["status"] == "pass"
        assert row["hardware"] == "not-run"
        planned = plan(row["id"])
        assert row["jobs"] == planned["jobs"]
        assert row["refuses"] == planned["refuses"]
        assert row["note"] == planned["note"]
    rehearsal = report_rows(dry_run=True)
    assert all(row["hardware"] == "not-run" for row in rehearsal)
    assert all(row["status"] == "pass" for row in rehearsal)
    assert all(job["dry_run"] is True for row in rehearsal for job in row["jobs"])


def test_write_report_honors_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BUILD_REPORT_PATH", raising=False)
    assert write_report() is None

    dest = tmp_path / "build-report.json"
    monkeypatch.setenv("BUILD_REPORT_PATH", str(dest))
    assert write_report() == str(dest)
    text = dest.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert json.loads(text) == {"rows": report_rows()}


def test_unknown_intake_raises():
    with pytest.raises(ValueError):
        plan("no-such-intake")
