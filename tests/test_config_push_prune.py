"""B2: config-push pruning — dropped files and entries must be removed.

Complements test_config_push.py: the first push of an environment writes a
.genestack-manifest.yaml recording what it produced; a later push that
renders fewer files deletes the dropped ones (backed up first) and prunes
the Secret / chart entries the document stopped declaring.
"""

from __future__ import annotations

import yaml

from app.models import JobStatus
from app.services import envconfig as envconfig_service
from tests.test_config_push import (
    CHARTS_DOC,
    DOC,
    SECRETS_DOC,
    _create_env,
    _manifests_by_name,
    _put_doc,
    _push_job,
    _suffix,
)


def _push_doc(client, admin_headers, env_id: str, doc: str):
    """PUT a new config doc and push it synchronously; return the job."""
    _put_doc(client, admin_headers, env_id, doc)
    resp = _push_job(client, admin_headers, env_id)
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    return job


def test_first_push_writes_manifest(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _push_doc(client, admin_headers, env["id"], DOC)

    manifest = yaml.safe_load(
        (config_dir / envconfig_service.MANIFEST_FILENAME).read_text()
    )
    assert set(manifest["pushed_files"]) == {
        "provider",
        "openstack-components.yaml",
        "helm-configs/keystone/console-rendered.yaml",
    }
    assert manifest["pinned_secrets"] == []
    assert manifest["pinned_charts"] == []


def test_dropped_helm_override_file_deleted(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )

    # First push renders a keystone override; second push drops it.
    _push_doc(client, admin_headers, env["id"], DOC)
    override = config_dir / "helm-configs" / "keystone" / "console-rendered.yaml"
    assert override.is_file()

    _push_doc(client, admin_headers, env["id"], "provider: kubespray\n")

    assert not override.exists()
    # The emptied helm-configs/keystone tree is cleaned up too
    assert not (config_dir / "helm-configs").exists()
    # The pre-deletion content was backed up
    backups = list(
        (config_dir / ".console-backup").glob(
            "*/helm-configs/keystone/console-rendered.yaml"
        )
    )
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text()) == {"replicas": 3}


def test_dropped_secret_pruned_generated_survives(client, admin_headers, tmp_path):
    """A secret the doc stops declaring is pruned; generated ones survive."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    # Externally generated entry (create-secrets.sh shape) — never pruned
    (config_dir / "kubesecrets.yaml").write_text(
        "---\n"
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: mariadb\n"
        "  namespace: openstack\n"
        "type: Opaque\n"
        "data:\n"
        f"  password: {envconfig_service.base64.b64encode(b'gen').decode()}\n"
    )
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _push_doc(client, admin_headers, env["id"], SECRETS_DOC)
    merged = _manifests_by_name((config_dir / "kubesecrets.yaml").read_text())
    assert set(merged) == {
        "mariadb",
        "netapp-cinder-backend",
        "keystone-rabbitmq-password",
    }

    # New doc keeps one secret, drops the other
    second = SECRETS_DOC.replace(
        "  netapp-cinder-backend:\n    data:\n      username: admin\n      password: s3cret\n",
        "",
    )
    _push_doc(client, admin_headers, env["id"], second)
    merged = _manifests_by_name((config_dir / "kubesecrets.yaml").read_text())
    assert set(merged) == {"mariadb", "keystone-rabbitmq-password"}


def test_dropped_chart_pruned_bootstrap_survives(client, admin_headers, tmp_path):
    """Chart pins the doc drops are pruned; bootstrap's set survives."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "helm-chart-versions.yaml").write_text(
        "charts:\n  mariadb-operator: 0.38.1\n  glance: 2026.1.9+7cce5ac45\n"
    )
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _push_doc(client, admin_headers, env["id"], CHARTS_DOC)
    charts = yaml.safe_load((config_dir / "helm-chart-versions.yaml").read_text())[
        "charts"
    ]
    assert set(charts) == {"mariadb-operator", "glance", "keystone", "cinder"}

    second = "chart_versions:\n  keystone: 2026.1.9+abcdef123\n"
    _push_doc(client, admin_headers, env["id"], second)
    charts = yaml.safe_load((config_dir / "helm-chart-versions.yaml").read_text())[
        "charts"
    ]
    assert charts == {
        "mariadb-operator": "0.38.1",
        "glance": "2026.1.9+7cce5ac45",
        "keystone": "2026.1.9+abcdef123",
    }


def test_dry_run_push_writes_nothing_and_plans_nothing(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], DOC)
    # Global dry_run=True: the plan is logged, nothing is written — no
    # rendered files and no .genestack-manifest.yaml.
    resp = _push_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not (config_dir / envconfig_service.MANIFEST_FILENAME).exists()
    assert list(config_dir.rglob("*")) == []


def test_first_push_without_previous_manifest_no_deletions(
    client, admin_headers, tmp_path
):
    """No previous manifest -> nothing is deletable (fresh config dir)."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    # A file present but never in a manifest must survive the first push
    (config_dir / "operator-owned.yml").write_text("x: 1\n")
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _push_doc(client, admin_headers, env["id"], DOC)
    assert (config_dir / "operator-owned.yml").is_file()


def test_ssh_keys_never_pruned_by_config_push(tmp_path):
    """The env's .ssh key pair is env-owned; a config push never deletes it."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / ".ssh").mkdir(parents=True)
    key = config_dir / ".ssh" / "id_ed25519"
    key.write_text("private\n")
    (key.parent / "id_ed25519.pub").write_text("public\n")
    # A previous manifest that (hypothetically) listed the keys
    (config_dir / envconfig_service.MANIFEST_FILENAME).write_text(
        envconfig_service._dump(
            {
                "pushed_files": ["provider", ".ssh/id_ed25519", ".ssh/id_ed25519.pub"],
                "pinned_secrets": [],
                "pinned_charts": [],
            }
        )
    )

    from app.db import SessionLocal
    from app.models import Environment
    from app.services.job_runner import JobRunner

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-prune-keys-{_suffix()}",
            genestack_config_dir=str(config_dir),
            dry_run=False,
        )
        db.add(env)
        db.commit()
        envconfig_service.put_version(db, env, "provider: kubespray\n", "t")
        db.commit()
        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.config.push",
            params={},
            environment_id=env.id,
            created_by="t",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.success, job.error
    finally:
        db.close()

    assert key.is_file()
    assert (key.parent / "id_ed25519.pub").is_file()
    manifest = yaml.safe_load(
        (config_dir / envconfig_service.MANIFEST_FILENAME).read_text()
    )
    assert ".ssh/id_ed25519" not in manifest["pushed_files"]
