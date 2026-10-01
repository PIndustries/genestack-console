"""Refuse dangerous environment create/PATCH fields."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas import EnvironmentCreate, EnvironmentUpdate
from app.services.env_field_guards import (
    refuse_dangerous_path,
    refuse_dangerous_ssh_host,
    refuse_dangerous_ssh_user,
    refuse_kubeconfig_exec_plugins,
)

_EXEC_KUBECONFIG = """
apiVersion: v1
kind: Config
users:
  - name: evil
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1
        command: /usr/bin/evil
        args: []
clusters: []
contexts: []
""".strip()

_SAFE_KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters: []\nusers: []\ncontexts: []\n"


def test_refuse_ssh_user_proxycommand():
    with pytest.raises(ValueError, match="ProxyCommand"):
        refuse_dangerous_ssh_user("root -oProxyCommand=/bin/evil")
    with pytest.raises(ValueError, match="ProxyCommand"):
        refuse_dangerous_ssh_user("-oProxyCommand=nc")
    with pytest.raises(ValueError):
        refuse_dangerous_ssh_user("alice@evil")
    assert refuse_dangerous_ssh_user("ubuntu") == "ubuntu"
    assert refuse_dangerous_ssh_user(None) is None


def test_refuse_ssh_host_options():
    with pytest.raises(ValueError, match="ProxyCommand"):
        refuse_dangerous_ssh_host("host -oProxyCommand=evil")
    assert refuse_dangerous_ssh_host("deployer.example.com") == "deployer.example.com"
    assert refuse_dangerous_ssh_host("10.0.0.5") == "10.0.0.5"


def test_refuse_dangerous_paths():
    with pytest.raises(ValueError, match="\\.\\."):
        refuse_dangerous_path("/opt/genestack/../etc/shadow", field="genestack_path")
    with pytest.raises(ValueError, match="shell-meta"):
        refuse_dangerous_path("/tmp/x; id", field="kubeconfig_path")
    assert (
        refuse_dangerous_path("/etc/genestack-lab", field="genestack_config_dir")
        == "/etc/genestack-lab"
    )


def test_refuse_kubeconfig_exec_plugins():
    with pytest.raises(ValueError, match="exec plugins"):
        refuse_kubeconfig_exec_plugins(_EXEC_KUBECONFIG)
    assert refuse_kubeconfig_exec_plugins(_SAFE_KUBECONFIG) == _SAFE_KUBECONFIG
    assert refuse_kubeconfig_exec_plugins("***") == "***"


def test_environment_create_schema_rejects_exec_kubeconfig():
    with pytest.raises(ValidationError, match="exec plugins"):
        EnvironmentCreate(name="x", kubeconfig_data=_EXEC_KUBECONFIG)


def test_environment_update_schema_rejects_proxycommand_user():
    with pytest.raises(ValidationError, match="ProxyCommand"):
        EnvironmentUpdate(deployer_ssh_user="root -oProxyCommand=/bin/evil")


def test_environment_update_schema_rejects_path_traversal():
    with pytest.raises(ValidationError, match="\\.\\."):
        EnvironmentUpdate(kubeconfig_path="/var/lib/kube/../../etc/passwd")


def test_patch_api_refuses_exec_kubeconfig(client, admin_headers):
    name = f"env-p0-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name},
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    bad = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"kubeconfig_data": _EXEC_KUBECONFIG},
    )
    assert bad.status_code == 422, bad.text
    assert "exec" in bad.text.lower()

    bad_user = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"deployer_ssh_user": "root -oProxyCommand=/bin/evil"},
    )
    assert bad_user.status_code == 422, bad_user.text

    ok = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={
            "deployer_ssh_user": "ubuntu",
            "deployer_ssh_host": "deployer.example.com",
            "kubeconfig_data": _SAFE_KUBECONFIG,
        },
    )
    assert ok.status_code == 200, ok.text
