"""Helm stops on an unschedulable pod and installs Gateway CRDs first."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta

from app.services.helm_guard import (
    arm_helm_guard,
    envoy_gateway_version,
    install_helm_guard,
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
                message="untolerated taint node-role.kubernetes.io/control-plane",
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
    assert "untolerated taint" in result.stdout
    assert stat.S_IMODE(guard.stat().st_mode) & 0o111


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
    script = render_helm_guard()
    assert "gateways.gateway.networking.k8s.io" in script
    assert "allowScheduling" not in script


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
