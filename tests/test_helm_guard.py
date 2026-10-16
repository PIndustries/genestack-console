"""Helm stops on an unschedulable pod and installs Gateway CRDs first."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta

import shlex

from app.services.helm_guard import (
    REPO_INDEX_STAMP,
    arm_helm_guard,
    control_plane_pin_pods,
    envoy_gateway_version,
    install_helm_guard,
    install_yq_shim,
    render_helm_guard,
    unschedulable_messages,
)


def _pod(name: str, *, reason: str, age_s: int, message: str = "no nodes") -> dict:
    changed = datetime.now(UTC) - timedelta(seconds=age_s)
    return {
        "metadata": {"name": name, "namespace": "openstack"},
        "status": {
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": reason,
                    "message": message,
                    "lastTransitionTime": changed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ]
        },
    }


def test_unschedulable_message_waits_out_the_grace():
    now = datetime.now(UTC)
    fresh = unschedulable_messages(
        {"items": [_pod("keystone-api", reason="Unschedulable", age_s=10)]},
        now=now,
        grace_s=60,
    )
    held = unschedulable_messages(
        {
            "items": [
                _pod(
                    "keystone-api",
                    reason="Unschedulable",
                    age_s=120,
                    message="untolerated taint",
                )
            ]
        },
        now=now,
        grace_s=60,
    )
    pulling = unschedulable_messages(
        {"items": [_pod("glance-api", reason="ContainerCreating", age_s=600)]},
        now=now,
        grace_s=60,
    )
    assert fresh == []
    assert held == ["openstack/keystone-api: untolerated taint"]
    assert pulling == []


def test_envoy_version_comes_from_the_chart_file():
    assert envoy_gateway_version("envoyproxy-gateway: v1.7.0\n") == "v1.7.0"
    assert envoy_gateway_version("kube-ovn: v1.14.10\n") == "v1.7.0"


def test_control_plane_pin_is_the_label_or_the_taint():
    labeled = _pod("keystone-credential-setup", reason="Unschedulable", age_s=30)
    labeled["spec"] = {"nodeSelector": {"openstack-control-plane": "enabled"}}
    tainted = _pod(
        "glance-api",
        reason="Unschedulable",
        age_s=30,
        message=(
            "0/3 nodes are available: 3 node(s) had untolerated taint "
            "{node-role.kubernetes.io/control-plane: }"
        ),
    )
    busy = _pod("nova-api", reason="Unschedulable", age_s=120, message="insufficient cpu")
    pulling = _pod(
        "keystone-api",
        reason="ContainerCreating",
        age_s=600,
        message="pulling image",
    )
    pulling["spec"] = {"nodeSelector": {"openstack-control-plane": "enabled"}}
    found = control_plane_pin_pods(
        {"items": [labeled, tainted, busy, pulling]}
    )
    assert found == [
        "openstack/keystone-credential-setup",
        "openstack/glance-api",
    ]


def test_frozen_helpers_match_the_checkout():
    import inspect

    from app.services import helm_guard

    for name, fn in (
        ("envoy_gateway_version", helm_guard.envoy_gateway_version),
        ("_parse_rfc3339", helm_guard._parse_rfc3339),
        ("unschedulable_messages", helm_guard.unschedulable_messages),
        ("control_plane_pin_pods", helm_guard.control_plane_pin_pods),
    ):
        assert helm_guard._FROZEN_HELPERS[name] == textwrap.dedent(inspect.getsource(fn))


def test_compiled_binary_still_renders_the_wrapper(monkeypatch):
    from app.services import helm_guard

    def missing(_fn):
        raise OSError("could not get source code")

    monkeypatch.setattr(helm_guard.inspect, "getsource", missing)
    script = render_helm_guard()
    compile(script, "<helm-guard>", "exec")
    assert "def unschedulable_messages" in script
    assert "def control_plane_pin_pods" in script
    assert "def envoy_gateway_version" in script
    assert "could not get source code" not in script


def test_wrapper_stops_a_held_upgrade(tmp_path):
    real = tmp_path / "real-helm"
    real.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ \"$i\" -lt 30 ]; do\n"
        "  i=$((i + 1))\n"
        "  sleep 1\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    real.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    payload = {
        "items": [
            _pod(
                "admission",
                reason="Unschedulable",
                age_s=180,
                message="insufficient cpu",
            )
        ]
    }
    kubectl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' " + json.dumps(json.dumps(payload)) + "\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GENESTACK_HELM_REAL"] = str(real)
    env["GENESTACK_HELM_GUARD_DELAY"] = "0"
    env["GENESTACK_HELM_UNSCHEDULABLE_GRACE"] = "0"
    result = subprocess.run(
        [str(guard), "upgrade", "--install", "keystone", "chart", "--namespace=openstack"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )
    assert result.returncode == 2, result.stderr
    assert "insufficient cpu" in result.stdout
    assert "placing " not in result.stdout
    assert stat.S_IMODE(guard.stat().st_mode) & 0o111


def _credential_pin() -> dict:
    pod = _pod(
        "keystone-credential-setup",
        reason="Unschedulable",
        age_s=120,
        message=(
            "0/3 nodes are available: 3 node(s) had untolerated taint "
            "{node-role.kubernetes.io/control-plane: }"
        ),
    )
    pod["spec"] = {"nodeSelector": {"openstack-control-plane": "enabled"}}
    return pod


def test_wrapper_places_control_planes_before_the_chart(tmp_path):
    calls = tmp_path / "kubectl-calls"
    real = tmp_path / "real-helm"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GENESTACK_HELM_REAL"] = str(real)
    env["GENESTACK_HELM_GUARD_DELAY"] = "0"
    env["GENESTACK_HELM_GUARD_POLL"] = "0"
    result = subprocess.run(
        [str(guard), "upgrade", "--install", "keystone", "chart", "-n", "openstack"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "openstack-control-plane=enabled" in result.stdout
    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert any(
        line.startswith("label node ") and "openstack-control-plane=enabled" in line
        for line in recorded
    )
    assert any(
        "control-plane:NoSchedule-" in line and line.startswith("taint nodes ")
        for line in recorded
    )


def test_wrapper_keeps_helm_after_the_pin_is_placed(tmp_path):
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(json.dumps({"items": [_credential_pin()]}), encoding="utf-8")
    count = tmp_path / "taints"
    real = tmp_path / "real-helm"
    real.write_text("#!/bin/sh\nsleep 2\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import sys

            args = sys.argv[1:]
            count = os.environ["GSC_TAINT_COUNT"]
            if args[:2] == ["taint", "nodes"]:
                seen = 0
                if os.path.exists(count):
                    seen = int(open(count, encoding="utf-8").read() or "0")
                open(count, "w", encoding="utf-8").write(str(seen + 1))
                raise SystemExit(0)
            if "get" in args and "pods" in args:
                seen = 0
                if os.path.exists(count):
                    seen = int(open(count, encoding="utf-8").read() or "0")
                if seen >= 2:
                    print('{"items": []}')
                else:
                    print(open(os.environ["GSC_PIN_JSON"], encoding="utf-8").read())
                raise SystemExit(0)
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GENESTACK_HELM_REAL"] = str(real)
    env["GENESTACK_HELM_GUARD_DELAY"] = "0"
    env["GENESTACK_HELM_GUARD_POLL"] = "0"
    env["GSC_TAINT_COUNT"] = str(count)
    env["GSC_PIN_JSON"] = str(pin_path)
    result = subprocess.run(
        [str(guard), "upgrade", "--install", "keystone", "chart", "-n", "openstack"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "placing openstack/keystone-credential-setup" in result.stdout
    assert "0/3 nodes" not in result.stdout
    assert int(count.read_text(encoding="utf-8")) >= 2


def test_wrapper_stops_when_the_control_plane_pin_stays(tmp_path):
    payload = {"items": [_credential_pin()]}
    real = tmp_path / "real-helm"
    real.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ \"$i\" -lt 30 ]; do\n"
        "  i=$((i + 1))\n"
        "  sleep 1\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    real.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + json.dumps(json.dumps(payload)) + "\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GENESTACK_HELM_REAL"] = str(real)
    env["GENESTACK_HELM_GUARD_DELAY"] = "0"
    env["GENESTACK_HELM_GUARD_POLL"] = "0"
    env["GENESTACK_HELM_UNSCHEDULABLE_GRACE"] = "0"
    env["GENESTACK_HELM_CONTROL_PLANE_REPAIRS"] = "2"
    result = subprocess.run(
        [str(guard), "upgrade", "--install", "keystone", "chart", "--namespace=openstack"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )
    assert result.returncode == 2, result.stderr + result.stdout
    assert "placing openstack/keystone-credential-setup" in result.stdout
    assert "untolerated taint" in result.stdout


def test_wrapper_leaves_helm_show_alone(tmp_path):
    real = tmp_path / "real-helm"
    real.write_text("#!/bin/sh\nprintf 'show:%s\\n' \"$*\"\n", encoding="utf-8")
    real.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["GENESTACK_HELM_REAL"] = str(real)
    result = subprocess.run(
        [str(guard), "show", "crds", "chart"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "show:show crds chart" in result.stdout


def _yq(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_yq_shim_skips_python_yq(tmp_path):
    _yq(
        tmp_path / "python-yq" / "yq",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'yq 3.4.3'; exit 0; fi\n"
        "echo python-yq >&2\n"
        "exit 1\n",
    )
    _yq(
        tmp_path / "mike" / "yq",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'yq (https://github.com/mikefarah/yq/) version v4.47.2'\n"
        "  exit 0\n"
        "fi\n"
        "echo mike \"$*\"\n",
    )
    shim = install_yq_shim(tmp_path / "guard")
    env = dict(os.environ)
    # Unset: this case must use the mikefarah binary already on PATH.
    env.pop("YQ_DOWNLOAD_URL", None)
    env["PATH"] = (
        f"{tmp_path / 'python-yq'}{os.pathsep}{tmp_path / 'mike'}"
        f"{os.pathsep}/bin{os.pathsep}/usr/bin"
    )
    result = subprocess.run(
        [str(shim), "eval", ".networking.ENABLE_SSL // false"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mike ")
    assert "python-yq" not in result.stderr


def test_yq_shim_downloads_mikefarah_when_python_yq_is_first(tmp_path):
    """Python yq first must download YQ_DOWNLOAD_URL, not run a later yq.

    GitHub-hosted Ubuntu installs mikefarah yq at /usr/bin/yq. A decoy later
    on PATH stands in for that binary.
    """
    _yq(tmp_path / "python-yq" / "yq", "#!/bin/sh\necho 'yq 3.4.3'\n")
    _yq(
        tmp_path / "system" / "yq",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'yq (https://github.com/mikefarah/yq/) version v4.54.1'\n"
        "  exit 0\n"
        "fi\n"
        "echo system-yq \"$*\"\n",
    )
    payload = tmp_path / "payload"
    _yq(
        payload,
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'yq (https://github.com/mikefarah/yq/) version v4.47.2'\n"
        "  exit 0\n"
        "fi\n"
        "echo downloaded \"$*\"\n",
    )
    shim = install_yq_shim(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = (
        f"{tmp_path / 'python-yq'}{os.pathsep}{tmp_path / 'system'}"
        f"{os.pathsep}/bin{os.pathsep}/usr/bin"
    )
    env["YQ_DOWNLOAD_URL"] = payload.as_uri()
    result = subprocess.run(
        [str(shim), "eval", ".networking.ENABLE_SSL // false"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("downloaded ")
    assert (tmp_path / "guard" / "yq.bin").is_file()


def test_arm_puts_the_wrapper_first_on_path(tmp_path):
    helm = tmp_path / "bin"
    helm.mkdir()
    binary = helm / "helm"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    env = {"PATH": str(helm)}
    armed = arm_helm_guard(env, tmp_path / "guard")
    assert armed["PATH"].startswith(str(tmp_path / "guard"))
    assert armed["GENESTACK_HELM_REAL"] == str(binary)
    shim = tmp_path / "guard" / "yq"
    assert shim.is_file()
    assert shim.stat().st_mode & stat.S_IXUSR
    script = render_helm_guard()
    assert "gateways.gateway.networking.k8s.io" in script
    assert "allowScheduling" not in script


def test_repo_update_downloads_once_then_reuses_the_index(tmp_path):
    """One real helm repo update per deploy. Later updates do not download."""
    calls = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "helm"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        'if [ "$1" = "repo" ] && [ "$2" = "update" ]; then\n'
        '  if [ "${GENESTACK_TEST_HELM_FAIL:-}" = "1" ]; then\n'
        "    echo downloaded-fail\n"
        "    exit 1\n"
        "  fi\n"
        "  echo downloaded\n"
        "  exit 0\n"
        "fi\n"
        "echo ran\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    base = dict(os.environ)
    base["PATH"] = f"{bindir}{os.pathsep}{base.get('PATH', '')}"
    guard_dir = tmp_path / "guard"
    armed = arm_helm_guard(base, guard_dir)
    assert armed["PATH"].startswith(str(guard_dir))
    assert armed["GENESTACK_HELM_REAL"] == str(fake)
    assert REPO_INDEX_STAMP in render_helm_guard()
    stamp = guard_dir / REPO_INDEX_STAMP
    assert not stamp.exists()

    def run(*args: str, fail: bool = False, env: dict | None = None):
        run_env = dict(env or armed)
        run_env["GENESTACK_HELM_GUARD_DELAY"] = "0"
        if fail:
            run_env["GENESTACK_TEST_HELM_FAIL"] = "1"
        else:
            run_env.pop("GENESTACK_TEST_HELM_FAIL", None)
        return subprocess.run(
            ["helm", *args],
            capture_output=True,
            text=True,
            check=False,
            env=run_env,
            timeout=20,
        )

    failed = run("repo", "update", fail=True)
    assert failed.returncode == 1, failed.stderr
    assert "downloaded-fail" in failed.stdout
    assert not stamp.exists()

    first = run("repo", "update")
    assert first.returncode == 0, first.stderr
    assert "downloaded" in first.stdout
    assert stamp.is_file()

    second = run("repo", "update", "openstack-helm")
    assert second.returncode == 0, second.stderr
    assert "downloaded" not in second.stdout
    assert "reusing the helm repo index" in second.stdout

    listed = run("repo", "list")
    assert listed.returncode == 0, listed.stderr
    assert "ran" in listed.stdout

    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert recorded.count("repo update") == 2
    assert "repo update openstack-helm" not in recorded
    assert "repo list" in recorded

    stamp.write_text("stale\n", encoding="utf-8")
    rearmed = arm_helm_guard(base, guard_dir)
    assert not stamp.exists()
    third = run("repo", "update", env=rearmed)
    assert third.returncode == 0, third.stderr
    assert "downloaded" in third.stdout
    assert calls.read_text(encoding="utf-8").splitlines().count("repo update") == 3


def test_gateway_crds_run_before_cert_manager_and_missing_crds_fail(tmp_path):
    real = tmp_path / "real-helm"
    real.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "show" ]; then
              printf 'crd: missing\\n' >&2
              exit 1
            fi
            printf 'upgraded\\n'
            """
        ),
        encoding="utf-8",
    )
    real.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    kubectl.chmod(0o755)
    guard = install_helm_guard(tmp_path / "guard")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GENESTACK_HELM_REAL"] = str(real)
    result = subprocess.run(
        [
            str(guard),
            "upgrade",
            "--install",
            "cert-manager",
            "oci://quay.io/jetstack/charts/cert-manager",
            "--namespace=cert-manager",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )
    assert result.returncode != 0
    assert "Gateway API CRDs" in result.stderr
    assert "upgraded" not in result.stdout
