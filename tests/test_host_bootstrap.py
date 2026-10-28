"""Exercise the real managed bootstrap's filesystem and download boundaries."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io

import pytest

from app.paths import package_root

SOURCE = package_root() / "app/assets/deployer-bootstrap.py.in"
loader = importlib.machinery.SourceFileLoader("managed_bootstrap", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
bootstrap = importlib.util.module_from_spec(spec)
loader.exec_module(bootstrap)


def repository(tmp_path):
    root = tmp_path / "source"
    for relative in ["ansible/inventory/genestack/hosts.yaml", "etc/gateway-api/example.yaml", "manifests/example.yaml", "helm-chart-versions.yaml", "base-kustomize/keystone/base/service.yaml", "base-kustomize/keystone/overlay/kustomization.yaml", "base-helm-configs/keystone/values.yaml"]:
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("pristine-source\n")
    return root


def test_cloud_layout_is_isolated_and_retry_preserves_customer_overrides(tmp_path):
    root = repository(tmp_path)
    config = tmp_path / "cloud-one"
    other = tmp_path / "cloud-two"
    bootstrap.initialize_layout(root, config, "talos")
    bootstrap.initialize_layout(root, other, "kubespray")
    override = config / "kustomize/keystone/overlay/kustomization.yaml"
    override.write_text("customer-one\n")
    inventory = config / "inventory/hosts.yaml"
    inventory.write_text("customer-inventory\n")
    bootstrap.initialize_layout(root, config, "talos")
    assert override.read_text() == "customer-one\n"
    assert inventory.read_text() == "customer-inventory\n"
    assert (root / "base-kustomize/keystone/overlay/kustomization.yaml").read_text() == "pristine-source\n"
    assert (other / "kustomize/keystone/overlay/kustomization.yaml").read_text() == "pristine-source\n"
    assert (config / "provider").read_text() == "talos\n"
    assert (other / "provider").read_text() == "kubespray\n"
    assert (config / "helm-configs/keystone").is_dir()
    assert (config / "helm-configs/global_overrides").is_dir()
    assert (config / "kustomize/keystone/base").resolve() == root / "base-kustomize/keystone/base"
    with pytest.raises(RuntimeError, match="Existing provider differs"):
        bootstrap.initialize_layout(root, config, "kubespray")
    assert (config / "provider").read_text() == "talos\n"


def test_configuration_symlink_cannot_modify_another_cloud(tmp_path):
    root = repository(tmp_path)
    target = tmp_path / "other"
    target.mkdir()
    config = tmp_path / "cloud"
    config.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinked"):
        bootstrap.initialize_layout(root, config, "talos")
    assert list(target.iterdir()) == []


def test_systemd_empty_home_and_frozen_loader_do_not_leak_to_system_python(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "")
    monkeypatch.setenv("PYTHONHOME", "/temporary/frozen-python")
    monkeypatch.setenv("PYTHONPATH", "/temporary/frozen-modules")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/temporary/frozen-libraries")
    env = bootstrap.execution_environment(tmp_path / "home", tmp_path / "source", tmp_path / "cloud", "talos")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["GENESTACK_CONFIG"] == str(tmp_path / "cloud")
    assert env["ANSIBLE_INVENTORY"] == str(tmp_path / "cloud/inventory")
    assert all(key not in env for key in ["PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"])


@pytest.mark.parametrize("valid", [False, True])
def test_kubectl_is_only_installed_after_published_checksum_matches(tmp_path, monkeypatch, valid):
    data = b"disposable executable fixture"
    checksum = hashlib.sha256(data).hexdigest() if valid else "0" * 64
    addresses = []
    def fetch(request, timeout):
        addresses.append(request.full_url)
        return io.BytesIO(checksum.encode() if request.full_url.endswith(".sha256") else data)
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    if valid:
        bootstrap.install_kubectl(tmp_path, "v1.35.0")
        assert (tmp_path / "kubectl").read_bytes() == data
        assert (tmp_path / "kubectl").stat().st_mode & 0o777 == 0o755
    else:
        with pytest.raises(RuntimeError, match="checksum"):
            bootstrap.install_kubectl(tmp_path, "v1.35.0")
        assert not (tmp_path / "kubectl").exists()
    assert addresses == ["https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl.sha256", "https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl"]


def test_writable_helm_symlink_cannot_escape_cloud_directory(tmp_path):
    root = repository(tmp_path)
    config = tmp_path / "cloud"
    config.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (config / "helm-configs").symlink_to(other, target_is_directory=True)
    with pytest.raises(RuntimeError, match="configuration symlink"):
        bootstrap.initialize_layout(root, config, "talos")
    assert list(other.iterdir()) == []


def test_immutable_deployer_never_runs_a_package_manager(monkeypatch):
    commands = []
    monkeypatch.setattr(bootstrap, "deployment_python", lambda **kwargs: "/isolated/python3")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda name: object())
    bootstrap.prepare_os(commands.append, immutable=True)
    assert commands == []


def test_incomplete_immutable_image_fails_before_bootstrap_writes(monkeypatch):
    commands = []
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None if name == "jq" else "/usr/bin/" + name)
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="jq, python3-venv"):
        bootstrap.prepare_os(commands.append, immutable=True)
    assert commands == []


def test_mutable_deployer_keeps_normal_os_preparation(monkeypatch):
    commands = []
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    bootstrap.prepare_os(commands.append, immutable=False)
    assert commands[0] == ["apt-get", "update", "-qq"]
    assert commands[1][:3] == ["apt-get", "install", "-y"]


def test_privileged_layout_retains_cloud_account_ownership_without_following_links(tmp_path, monkeypatch):
    root = repository(tmp_path)
    config = tmp_path / "account-cloud"
    config.mkdir()
    owner = config.stat()
    calls = []
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.os, "chown", lambda path, uid, gid, **kwargs: calls.append((path, uid, gid, kwargs)))
    bootstrap.initialize_account_layout(root, config, "talos")
    assert calls
    assert all(path.is_relative_to(config) and uid == owner.st_uid and gid == owner.st_gid
               and options == {"follow_symlinks": False} for path, uid, gid, options in calls)
    assert any(path.is_symlink() for path, *_ in calls)
    assert (root / "base-kustomize/keystone/base/service.yaml").read_text() == "pristine-source\n"


def test_clean_deployer_creates_tool_directory_before_upstream_installers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = repository(tmp_path)
    config = tmp_path / "cloud"
    tools = tmp_path / "usr-local-bin"
    observed = []
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "home")))
    monkeypatch.setattr(bootstrap, "prepare_os", lambda run: None)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *args, **kwargs: "/existing/tool")

    def run(argv, **kwargs):
        if argv[:2] == ["bash", "-ec"]:
            assert tools.is_dir()
            observed.append("installer")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.prepare(root, config, "talos", tools)
    assert observed == ["installer"]


@pytest.mark.parametrize("version,retained", [("3.13.18", True), ("3.14.7", False)])
def test_runtime_change_preserves_existing_environment_and_compatible_runtime(tmp_path, monkeypatch, version, retained):
    venv = tmp_path / ".venvs/genestack"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = " + version + "\n")
    (venv / "bin/python").write_text("existing interpreter")
    (venv / "bin/python").chmod(0o755)
    (venv / "customer-package").write_text("keep this")
    calls = []
    monkeypatch.setattr(bootstrap, "deployment_python", lambda **kwargs: "/isolated/python3")
    assert bootstrap.prepare_venv(tmp_path, calls.append, immutable=True) == venv
    if retained:
        assert calls == []
        assert (venv / "customer-package").read_text() == "keep this"
    else:
        assert calls == [["/isolated/python3", "-m", "venv", str(venv)]]
        backups = list(venv.parent.glob("genestack.previous-*"))
        assert len(backups) == 1 and (backups[0] / "customer-package").read_text() == "keep this"


def test_missing_appliance_runtime_fails_without_using_os_python(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "DEPLOYER_PYTHON", tmp_path / "missing-python")
    with pytest.raises(RuntimeError, match="isolated Python"):
        bootstrap.deployment_python(immutable=True)


def test_mutable_deployer_installs_network_probe_and_completion_dependencies(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/" + name if name == "apt-get" else None)
    commands = []
    bootstrap.prepare_os(commands.append, immutable=False)
    install = next(command for command in commands if command[:2] == ["apt-get", "install"])
    assert "iputils-arping" in install and "bash-completion" in install
