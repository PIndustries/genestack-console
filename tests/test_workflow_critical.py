"""Test workflow — critical path step state functions.

Focused unit tests for the workflow step builders:
_connect_step, _inventory_step, _config_step.

Tests the service functions directly without API endpoints.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock


from app.models import EnvConfigVersion, Environment, Job, JobStatus
from app.services import workflow as workflow_service


def _make_db_session():
    """Create a fresh in-memory DB session with tables."""
    from app.db import create_db_engine, Base
    from app import models  # noqa: F401 — populate metadata

    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine)
    return Session()


def _make_env(db, **kwargs):
    """Create and persist an Environment."""
    env = Environment(
        name=f"wf-test-{uuid.uuid4().hex[:8]}",
        region=kwargs.get("region") or "lab",
        tier=kwargs.get("tier") or "dev",
        genestack_config_dir=kwargs.get("genestack_config_dir"),
        deployer_ssh_host=kwargs.get("deployer_ssh_host"),
        deployer_ssh_user=kwargs.get("deployer_ssh_user"),
        kubeconfig_path=kwargs.get("kubeconfig_path"),
        kubeconfig_data=kwargs.get("kubeconfig_data"),
        dry_run=kwargs.get("dry_run"),
    )
    db.add(env)
    db.commit()
    return env


class TestConnectStep:
    """Connect step: done when config dir + prepared, attention when no config dir."""

    def test_done_when_config_dir_set(self):
        db = _make_db_session()
        env = _make_env(db, genestack_config_dir="/etc/genestack")
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["state"] == "done"
        assert "deploy target configured" in step["summary"]
        assert step["details"]["genestack_config_dir"] == "/etc/genestack"
        db.close()

    def test_local_hub_when_no_config_dir(self):
        db = _make_db_session()
        env = _make_env(db)
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["state"] == "done"
        assert step["summary"] == "this console is the fleet hub"
        assert step["details"]["local_hub"] is True
        assert step["details"]["genestack_config_dir"] is None
        db.close()

    def test_prepared_none_when_no_job(self):
        db = _make_db_session()
        env = _make_env(db, genestack_config_dir="/etc/genestack")
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["prepared"] is None
        db.close()

    def test_prepared_true_when_job_success(self):
        db = _make_db_session()
        env = _make_env(db, genestack_config_dir="/etc/genestack")
        job = Job(
            environment_id=env.id,
            operation="genestack.host_prepare",
            status=JobStatus.success,
            log_text="",
            created_by="test",
        )
        db.add(job)
        db.commit()
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["prepared"] is True
        assert step["details"]["prepare_job_id"] == job.id
        assert "host prepare not verified" not in step["summary"]
        db.close()

    def test_prepared_false_when_job_failed(self):
        db = _make_db_session()
        env = _make_env(db, genestack_config_dir="/etc/genestack")
        job = Job(
            environment_id=env.id,
            operation="genestack.host_prepare",
            status=JobStatus.failed,
            log_text="",
            created_by="test",
        )
        db.add(job)
        db.commit()
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["prepared"] is False
        assert "host prepare not verified" in step["summary"]
        db.close()

    def test_kubeconfig_source_blob(self):
        db = _make_db_session()
        env = _make_env(
            db, genestack_config_dir="/etc/genestack", kubeconfig_data="blob-data"
        )
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["kubeconfig_source"] == "blob"
        db.close()

    def test_kubeconfig_source_path(self):
        db = _make_db_session()
        env = _make_env(
            db, genestack_config_dir="/etc/genestack", kubeconfig_path="/etc/kc"
        )
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["kubeconfig_source"] == "path"
        db.close()

    def test_kubeconfig_source_default(self):
        db = _make_db_session()
        env = _make_env(db, genestack_config_dir="/etc/genestack")
        settings = MagicMock()
        settings.dry_run = True

        step = workflow_service._connect_step(db, env, settings)
        assert step["details"]["kubeconfig_source"] == "default"
        db.close()


class TestInventoryStep:
    """Inventory step: done when all required roles covered."""

    def test_pending_no_servers(self):
        db = _make_db_session()
        env = _make_env(db)

        step = workflow_service._inventory_step(db, env)
        assert step["state"] == "pending"
        assert "no servers" in step["summary"]
        assert step["details"]["host_count"] == 0
        assert step["details"]["source"] == "none"
        db.close()

    def test_done_all_required_roles(self):
        db = _make_db_session()
        env = _make_env(db)
        doc = {
            "provider": "kubespray",
            "servers": {
                "cp01": {
                    "ip": "10.0.0.1",
                    "roles": ["k8s_control_plane", "etcd", "control"],
                    "source": "static",
                },
            },
        }
        version = EnvConfigVersion(
            environment_id=env.id,
            version=1,
            yaml_text=str(doc),
            created_by="test",
        )
        # Need proper YAML text
        import yaml

        version.yaml_text = yaml.safe_dump(doc)
        db.add(version)
        db.commit()

        step = workflow_service._inventory_step(db, env)
        assert step["state"] == "done"
        assert step["details"]["host_count"] == 1
        assert step["details"]["source"] == "doc"
        db.close()

    def test_attention_missing_required_roles(self):
        db = _make_db_session()
        env = _make_env(db)
        import yaml

        doc = {
            "servers": {
                "cp01": {
                    "ip": "10.0.0.1",
                    "roles": ["k8s_control_plane"],
                    "source": "static",
                },
            },
        }
        version = EnvConfigVersion(
            environment_id=env.id,
            version=1,
            yaml_text=yaml.safe_dump(doc),
            created_by="test",
        )
        db.add(version)
        db.commit()

        step = workflow_service._inventory_step(db, env)
        assert step["state"] == "attention"
        assert "missing required roles" in step["summary"]
        assert "etcd" in step["summary"]
        assert "control" in step["summary"]
        db.close()

    def test_tracked_roles_counted(self):
        db = _make_db_session()
        env = _make_env(db)
        import yaml

        doc = {
            "servers": {
                "cp01": {
                    "ip": "10.0.0.1",
                    "roles": ["k8s_control_plane", "etcd", "control"],
                    "source": "static",
                },
                "comp01": {"ip": "10.0.0.2", "roles": ["compute"], "source": "static"},
                "net01": {"ip": "10.0.0.3", "roles": ["network"], "source": "static"},
                "stor01": {"ip": "10.0.0.4", "roles": ["storage"], "source": "static"},
            },
        }
        version = EnvConfigVersion(
            environment_id=env.id,
            version=1,
            yaml_text=yaml.safe_dump(doc),
            created_by="test",
        )
        db.add(version)
        db.commit()

        step = workflow_service._inventory_step(db, env)
        roles = step["details"]["roles"]
        assert roles["k8s_control_plane"] == 1
        assert roles["etcd"] == 1
        assert roles["control"] == 1
        assert roles["compute"] == 1
        assert roles["network"] == 1
        assert roles["storage"] == 1
        db.close()


class TestConfigStep:
    """Config step: done when version exists."""

    def test_pending_no_config(self):
        db = _make_db_session()
        env = _make_env(db)

        step = workflow_service._config_step(db, env)
        assert step["state"] == "pending"
        assert "no config document" in step["summary"]
        assert step["details"]["version"] is None
        db.close()

    def test_done_when_version_exists(self):
        db = _make_db_session()
        env = _make_env(db)
        import yaml

        doc = {"provider": "kubespray"}
        version = EnvConfigVersion(
            environment_id=env.id,
            version=3,
            yaml_text=yaml.safe_dump(doc),
            created_by="admin",
        )
        db.add(version)
        db.commit()

        step = workflow_service._config_step(db, env)
        assert step["state"] == "done"
        assert step["details"]["version"] == 3
        assert step["details"]["updated_by"] == "admin"
        assert step["details"]["updated_at"] is not None
        db.close()


class TestBuildWorkflow:
    """build_workflow — assembles all steps."""

    def test_returns_all_six_steps(self):
        db = _make_db_session()
        env = _make_env(db)
        settings = MagicMock()
        settings.dry_run = True

        workflow = workflow_service.build_workflow(
            db, env, settings, include_operate_probe=False
        )
        step_ids = [s["id"] for s in workflow["steps"]]
        assert step_ids == [
            "connect",
            "inventory",
            "config",
            "push",
            "deploy",
            "operate",
        ]

        assert workflow["environment"]["id"] == env.id
        assert workflow["environment"]["name"] == env.name
        db.close()

    def test_operate_unchecked_when_disabled(self):
        db = _make_db_session()
        env = _make_env(db)
        settings = MagicMock()
        settings.dry_run = True

        workflow = workflow_service.build_workflow(
            db, env, settings, include_operate_probe=False
        )
        operate = [s for s in workflow["steps"] if s["id"] == "operate"][0]
        assert operate["state"] == "pending"
        assert operate["summary"] == "not checked"
        db.close()
