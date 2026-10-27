"""Failure and retry boundaries for the storage gap observed in DFW."""

import json
import runpy
import subprocess
from importlib.resources import files

import pytest

from app.services import managed_cinder

RUNTIME = files("app").joinpath("assets/managed-cinder.py.in")
OWNER = "test-environment"
KEY = "genestack.dev/storage-owner"
DOC = {"storage": {"longhorn_cinder": {"enabled": True, "capacity_gib": 8}}}


def test_storage_is_opt_in_and_rejects_unreviewed_or_conflicting_settings():
    assert managed_cinder.prepare_command({}, OWNER, "/tmp/config") is None
    for value in [
        True,
        {"enabled": "yes"},
        {"enabled": True},
        {"enabled": True, "capacity_gib": True},
        {"enabled": True, "capacity_gib": 7},
    ]:
        with pytest.raises(ValueError):
            managed_cinder.settings({"storage": {"longhorn_cinder": value}})
    for extra in [
        {"helm_overrides": {"cinder": {"storage": "ceph"}}},
        {"storage": dict(DOC["storage"], ceph={"enabled": True})},
    ]:
        with pytest.raises(ValueError):
            managed_cinder.settings(dict(DOC, **extra))


class Cluster:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.foreign_service = False
        self.failed_rollout = False

    def call(self, args, input=None, **kwargs):
        a = args[3:] if args[1:2] == ["--kubeconfig"] else args[1:]
        ns = "default"
        if a[:1] == ["-n"]:
            ns, a = a[1], a[2:]
        if a[0] == "create":
            obj = json.loads(input)
            self.writes.append(obj)
            obj["metadata"]["uid"] = obj["kind"] + "-uid"
            if obj["kind"] == "PersistentVolumeClaim":
                obj["spec"]["volumeName"] = "pvc-owned"
                obj["status"] = {"phase": "Bound"}
            self.objects[
                (obj["metadata"].get("namespace", ns), obj["kind"].lower(), obj["metadata"]["name"])
            ] = obj
            result = obj
        elif a[0] == "rollout":
            return subprocess.CompletedProcess(args, 1 if self.failed_rollout else 0, "", "")
        elif a[0] == "get":
            kind, name = a[1:3]
            if kind == "storageclass":
                result = {"provisioner": "driver.longhorn.io"}
            elif kind == "namespace":
                result = {}
            elif kind == "persistentvolume":
                result = {
                    "spec": {
                        "claimRef": {"uid": "PersistentVolumeClaim-uid"},
                        "csi": {"volumeHandle": "pvc-owned"},
                    }
                }
            elif kind == "volumes.longhorn.io":
                result = {"status": {"shareEndpoint": "nfs://10.0.0.5/pvc-owned"}}
            elif kind == "sharemanagers.longhorn.io":
                result = {"metadata": {"uid": "share-owned"}}
            elif kind == "services":
                result = {
                    "items": [
                        {
                            "metadata": {
                                "name": "pvc-owned",
                                "ownerReferences": [
                                    {
                                        "kind": "ShareManager",
                                        "name": "pvc-owned",
                                        "uid": "foreign" if self.foreign_service else "share-owned",
                                    }
                                ],
                            },
                            "spec": {"clusterIP": "10.0.0.5"},
                        }
                    ]
                }
            else:
                result = self.objects.get((ns, kind.lower(), name))
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(
            args, 0, json.dumps(result) if result is not None else "", ""
        )


def execute(tmp_path, monkeypatch, cluster):
    runtime = runpy.run_path(str(RUNTIME))
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("fixture")
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setattr(subprocess, "run", cluster.call)
    runtime["run"]({"environment_id": OWNER, "capacity_gib": 8, "config_dir": str(tmp_path)})
    return tmp_path / "helm-configs/cinder/zz-console-managed-storage.yaml"


def test_real_runtime_retains_claim_on_retry_and_uses_owned_service_dns(tmp_path, monkeypatch):
    cluster = Cluster()
    output = execute(tmp_path, monkeypatch, cluster)
    values = json.loads(output.read_text())
    assert len(cluster.writes) == 2
    assert (
        values["pod"]["lifecycle"]["upgrades"]["deployments"]["pod_replacement_strategy"]
        == "Recreate"
    )
    assert values["manifests"]["deployment_volume"] is True
    assert values["manifests"]["job_bootstrap"] is True
    execute(tmp_path, monkeypatch, cluster)
    assert len(cluster.writes) == 2
    backend = values["conf"]["backends"]["gsc_longhorn"]
    assert backend["nas_host"] == "pvc-owned.longhorn-system.svc"
    assert backend["nas_share_path"] == "/pvc-owned/cinder"
    assert backend["backend_host"] == "gsc-" + OWNER
    assert "host" not in values["conf"]["cinder"]["DEFAULT"]
    assert (
        values["bootstrap"]["volume_types"]["genestack"]["volume_backend_name"]
        == backend["volume_backend_name"]
    )


@pytest.mark.parametrize("kind", ["PersistentVolumeClaim", "Deployment"])
def test_foreign_objects_are_detected_before_any_writes(tmp_path, monkeypatch, kind):
    cluster = Cluster()
    cluster.objects[("openstack", kind.lower(), "genestack-cinder-data")] = {
        "metadata": {"labels": {KEY: "other"}}
    }
    with pytest.raises(RuntimeError, match="requires review"):
        execute(tmp_path, monkeypatch, cluster)
    assert not cluster.writes


def test_existing_cinder_requires_migration_review_before_any_writes(tmp_path, monkeypatch):
    cluster = Cluster()
    cluster.objects[("openstack", "deployment", "cinder-volume")] = {
        "spec": {"template": {"metadata": {}}}
    }
    with pytest.raises(RuntimeError, match="migration review"):
        execute(tmp_path, monkeypatch, cluster)
    assert not cluster.writes


@pytest.mark.parametrize("failure", ["foreign_service", "failed_rollout"])
def test_failure_retains_new_claim_but_never_publishes_ready_override(
    tmp_path, monkeypatch, failure
):
    cluster = Cluster()
    setattr(cluster, failure, True)
    with pytest.raises(RuntimeError):
        execute(tmp_path, monkeypatch, cluster)
    assert len(cluster.writes) == 2
    assert not (tmp_path / "helm-configs/cinder/zz-console-managed-storage.yaml").exists()


def test_changed_capacity_is_rejected_without_writes(tmp_path, monkeypatch):
    cluster = Cluster()
    execute(tmp_path, monkeypatch, cluster)
    cluster.writes.clear()
    claim = cluster.objects[("openstack", "persistentvolumeclaim", "genestack-cinder-data")]
    claim["spec"]["resources"]["requests"]["storage"] = "16Gi"
    with pytest.raises(RuntimeError, match="replacement is forbidden"):
        execute(tmp_path, monkeypatch, cluster)
    assert not cluster.writes


def test_transient_read_failure_retries_without_replacing_storage(tmp_path, monkeypatch):
    cluster = Cluster()
    original = cluster.call
    failed = False

    def one_failed_read(args, **kwargs):
        nonlocal failed
        if not failed and "get" in args:
            failed = True
            return subprocess.CompletedProcess(args, 1, "", "temporary API error")
        return original(args, **kwargs)

    cluster.call = one_failed_read
    monkeypatch.setattr("time.sleep", lambda _: None)
    execute(tmp_path, monkeypatch, cluster)
    assert failed
    assert len(cluster.writes) == 2


def test_runtime_cannot_fall_back_to_users_default_cluster(tmp_path, monkeypatch):
    runtime = runpy.run_path(str(RUNTIME))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    cluster = Cluster()
    monkeypatch.setattr(subprocess, "run", cluster.call)
    with pytest.raises(RuntimeError, match="explicit environment kubeconfig"):
        runtime["run"]({"environment_id": OWNER, "capacity_gib": 8, "config_dir": str(tmp_path)})
    assert not cluster.writes
