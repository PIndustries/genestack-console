"""The Machines stage board follows a deploy that resumes past infrastructure."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "app" / "static" / "js" / "deploy_stages.js"


def _run(body: str) -> None:
    script = MODULE.read_text(encoding="utf-8") + "\n" + body
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_running_platform_extras_marks_earlier_stages_done():
    _run(
        """
        const state = stageStates({
          status: "running",
          params: { from_stage: "platform-extras", until_stage: "platform-extras" },
          log_text: "[deploy] === stage 7/9: platform-extras (Additional OpenStack Services) ===",
        });
        for (const id of ["infrastructure", "operators", "cni", "core", "compute-network"]) {
          if (state[id] !== "done") throw new Error(id + " " + state[id]);
        }
        if (state["platform-extras"] !== "running") throw new Error(state["platform-extras"]);
        """
    )


def test_success_through_compute_network_leaves_extras_unstarted():
    _run(
        """
        const state = stageStates({
          status: "success",
          params: { from_stage: "compute-network", until_stage: "compute-network" },
          log_text: "[deploy] stage compute-network complete",
        });
        for (const id of ["infrastructure", "operators", "cni", "core", "compute-network"]) {
          if (state[id] !== "done") throw new Error(id + " " + state[id]);
        }
        if (state["platform-extras"] !== "not-started") throw new Error(state["platform-extras"]);
        """
    )


def test_fresh_infrastructure_deploy_does_not_mark_later_stages():
    _run(
        """
        const state = stageStates({
          status: "running",
          params: { from_stage: "infrastructure" },
          log_text: "[deploy] === stage 2/9: infrastructure ===",
        });
        if (state.infrastructure !== "running") throw new Error(state.infrastructure);
        if (state.operators !== "not-started") throw new Error(state.operators);
        if (state["platform-extras"] !== "not-started") throw new Error(state["platform-extras"]);
        """
    )


def test_storage_ceph_counts_as_storage():
    _run(
        """
        const servers = [
          { roles: ["storage-ceph"] },
          { roles: ["storage-cinder"] },
          { roles: ["storage"] },
          { roles: ["compute"] },
        ];
        const count = servers.filter((server) => hasRequiredRole(server, "storage")).length;
        if (count !== 3) throw new Error(String(count));
        if (hasRequiredRole({ roles: ["compute"] }, "compute") !== true) throw new Error("compute");
        """
    )
