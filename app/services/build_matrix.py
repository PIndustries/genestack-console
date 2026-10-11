"""Jobs one intake will run, and the actions that intake refuses.

Planning names jobs only. It does not power hardware or open a network connection.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

ROWS = [
    "outside-iso",
    "console-pxe",
    "virtual-cd",
    "pxe-no-bmc",
    "user-ubuntu",
    "existing-talos",
    "existing-k8s",
    "flip-talos-ubuntu",
    "look-around",
]

_FLIP_JOBS: list[dict[str, Any]] = [
    {
        "operation": "genestack.greenfield",
        "params": {"stop_after": "talos", "parallelism": 2},
    },
    {
        "operation": "hosts.ubuntu.bringup",
        "params": {"leave_hostnames": ["node-1"]},
    },
    {"operation": "genestack.deploy", "params": {"parallelism": 2}},
]

_INTAKES: dict[str, dict[str, Any]] = {
    "outside-iso": {
        "jobs": [
            {"operation": "record-machine"},
            {"operation": "genestack.talos.bootstrap", "params": {}},
        ],
        "refuses": ["power", "pxe-restart"],
        "note": (
            "The ISO is fetched from the given URL. The console does not serve it "
            "and does not restart DHCP."
        ),
    },
    "console-pxe": {
        "jobs": [
            {
                "operation": "baremetal.node.next_boot",
                "params": {"next_boot": "talos", "boot_now": True},
            }
        ],
        "refuses": ["virtual-cd"],
        "note": "Boot files come from this console.",
    },
    "virtual-cd": {
        "jobs": [
            {
                "operation": "baremetal.node.iso_boot",
                "params": {"image_url": "http://image.example/metal-amd64.iso"},
            }
        ],
        "refuses": ["vendor-path"],
        "note": (
            "The job mounts that ISO and power-cycles the machine when it runs. "
            "iLO and iDRAC use the same generic InsertMedia path. "
            "This test does not call a BMC."
        ),
    },
    "pxe-no-bmc": {
        "jobs": [
            {
                "operation": "genestack.talos.bootstrap",
                "params": {},
                "user_step": "boot-installer",
            }
        ],
        "refuses": ["power"],
        "note": "No management port. The job names the one step and waits.",
    },
    "user-ubuntu": {
        "jobs": [
            {"operation": "record-ubuntu"},
            {"operation": "ssh-check"},
            {"operation": "genestack.deploy", "params": {"parallelism": 2}},
        ],
        "refuses": ["reboot"],
        "note": (
            "Already have an OS records the row. Deploy over SSH runs after SSH answers."
        ),
    },
    "existing-talos": {
        "jobs": [{"operation": "vault.put", "params": {"name": "talosconfig"}}],
        "refuses": ["replace", "reboot"],
        "note": "A pasted talosconfig matches the nodes. Replace stays off.",
    },
    "existing-k8s": {
        "jobs": [
            {"operation": "hosts.kubespray.adopt", "params": {"kubeconfig": True}}
        ],
        "refuses": ["genestack.deploy", "ansible"],
        "note": "The kubeconfig is stored. Deploy stays off until asked.",
    },
    "flip-talos-ubuntu": {
        "jobs": _FLIP_JOBS,
        "refuses": ["unnamed-replace"],
        "note": (
            "Greenfield stops after Talos. Ubuntu names the hosts that leave Talos. "
            "Deploy then runs host setup."
        ),
    },
    "look-around": {
        "jobs": _FLIP_JOBS,
        "refuses": ["power", "ssh"],
        "note": "Look around prints the list and does not power or SSH.",
        "force_dry_run": True,
    },
}


def plan(intake: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Return jobs, refuses, and the note for one intake.

    ``dry_run`` marks every job as a rehearsal. Look-around always does that.
    Unknown intakes raise ``ValueError``.
    """
    spec = _INTAKES.get(intake)
    if spec is None:
        raise ValueError(f"unknown intake: {intake}")
    jobs = deepcopy(spec["jobs"])
    if dry_run or spec.get("force_dry_run"):
        for job in jobs:
            job["dry_run"] = True
    return {
        "jobs": jobs,
        "refuses": list(spec["refuses"]),
        "note": spec["note"],
    }


def report_rows(dry_run: bool = False) -> list[dict[str, Any]]:
    """One pass row per intake. Hardware is never run."""
    rows: list[dict[str, Any]] = []
    for intake_id in ROWS:
        planned = plan(intake_id, dry_run=dry_run)
        rows.append(
            {
                "id": intake_id,
                "status": "pass",
                "hardware": "not-run",
                "jobs": planned["jobs"],
                "refuses": planned["refuses"],
                "note": planned["note"],
            }
        )
    return rows


def write_report() -> str | None:
    """Write ``{"rows": report_rows()}`` when ``BUILD_REPORT_PATH`` is set.

    The file is UTF-8 with a trailing newline. Unset or empty means write nothing.
    """
    path = os.environ.get("BUILD_REPORT_PATH")
    if not path:
        return None
    payload = json.dumps({"rows": report_rows()}) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path
