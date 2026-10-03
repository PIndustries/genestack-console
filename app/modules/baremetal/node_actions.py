"""What: Shared steps: Bare-Metal Node Power (baremetal.node.power); PXE Boot Bare-Metal
Node (baremetal.node.pxe_boot); Set Bare-Metal Next Boot (baremetal.node.next_boot); ISO
Boot Bare-Metal Node (baremetal.node.iso_boot); Provision Bare-Metal Node (zero-touch)
(baremetal.node.provision).
Where: app/modules/baremetal/node_actions.py. BaremetalModule lists this file.
Why: These handlers share one body, so they stay in one file instead of growing the job
runner.
"""

from __future__ import annotations

from typing import Any

from app.modules.params import p as _p
from app.services.redfish import RESET_TYPES

HANDLERS = (
    "baremetal_node_power",
    "baremetal_node_pxe_boot",
    "baremetal_node_next_boot",
    "baremetal_node_iso_boot",
    "baremetal_node_provision",
)

OPERATIONS = (
    {
        "id": "baremetal.node.power",
        "name": "Bare-Metal Node Power",
        "description": (
            "Power a registered bare-metal node on/off/restart via Redfish "
            "(ComputerSystem.Reset: On/ForceOff/ForceRestart)."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("node_id", True, "BaremetalNode row UUID"),
            _p("action", True, "on, off, or restart", enum=sorted(RESET_TYPES)),
        ],
        "handler": "baremetal_node_power",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "baremetal.node.pxe_boot",
        "name": "PXE Boot Bare-Metal Node",
        "description": (
            "Set one-shot PXE, read BootSourceOverrideTarget back, then "
            "ForceRestart. State moves to booting. The image is this "
            "machine's current next boot: commission, Talos, or an exit to "
            "the local disk. This does not change that choice. A machine "
            "that was not asked to provision stays on disk, so this PXE "
            "does not wipe it."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("node_id", True, "BaremetalNode row UUID"),
        ],
        "handler": "baremetal_node_pxe_boot",
        "mutating": True,
        "timeout_seconds": 300,
    },
    {
        "id": "baremetal.node.next_boot",
        "name": "Set Bare-Metal Next Boot",
        "description": (
            "Choose the next PXE image for one machine: commission (RAM-disk "
            "probe and fixed-disk wipe), talos (only after a wipe report), "
            "ubuntu (autoinstall of that machine, whole disk, no Talos), "
            "or disk (iPXE exits to the local disk). boot_now power-cycles "
            "into that image. The default for a machine that was not asked "
            "to provision stays disk, so a stray PXE does not wipe."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("node_id", True, "BaremetalNode row UUID"),
            _p(
                "next_boot",
                True,
                "commission, talos, ubuntu, or disk",
                enum=["commission", "talos", "ubuntu", "disk"],
            ),
            _p(
                "boot_now",
                False,
                "Power-cycle into the chosen image now",
                "boolean",
            ),
        ],
        "handler": "baremetal_node_next_boot",
        "mutating": True,
        "timeout_seconds": 300,
    },
    {
        "id": "baremetal.node.iso_boot",
        "name": "ISO Boot Bare-Metal Node",
        "description": (
            "Insert the console-hosted Talos ISO into iLO virtual CD, "
            "ForceOff, wait Off, then On (not ACPI restart). State moves to "
            "booting. Use this when NIC PXE is blocked. image_url is optional "
            "and defaults to the Console ISO on a BMC-reachable address "
            "(http://<iso_host>:<api-port>/pxe-media/metal-amd64.iso), not "
            "the PXE next_server — iLO is not on the provision L2."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("node_id", True, "BaremetalNode row UUID"),
            _p("image_url", False, "HTTP(S) ISO URL (default: console PXE HTTP ISO)"),
        ],
        "handler": "baremetal_node_iso_boot",
        "mutating": True,
        "timeout_seconds": 420,
    },
    {
        "id": "baremetal.node.provision",
        "name": "Provision Bare-Metal Node (zero-touch)",
        "description": (
            "Commission, then Talos. The first PXE is a RAM disk that wipes "
            "fixed disks and posts a hardware report. After that report, a "
            "second one-shot PXE serves Talos. Success is fresh maintenance: "
            "the Talos API is up, the node is not Kubernetes Ready, and Talos "
            "was served after this wipe. The node is then marked talos-ready "
            "and upserted into the env config doc servers section "
            "(source=baremetal). stop_after=commission returns after the wipe "
            "report and does not serve Talos. stop_after=talos returns after "
            "fresh maintenance and does not update inventory. The node needs "
            "a PXE MAC and an expected_ip first."
        ),
        "required_role": "admin",
        "backend": "baremetal",
        "params": [
            _p("node_id", True, "BaremetalNode row UUID"),
            _p(
                "roles",
                False,
                "Genestack roles for the doc servers entry: "
                "k8s_control_plane/etcd/control/compute/network/storage",
                "array",
            ),
            _p(
                "stop_after",
                False,
                "Optional hold: commission (after the wipe report, no Talos) "
                "or talos (after fresh maintenance, no inventory update). "
                "Omit to commission, install Talos, and update inventory.",
            ),
        ],
        "handler": "baremetal_node_provision",
        "mutating": True,
        "timeout_seconds": 1800,
    },
)


def run(
    self,
    handler,
    op,
    job,
    env,
    log,
    ctx,
    params,
    deadline,
    check_cancel,
    dry,
    timeout,
    gs_root,
    ans_root,
    extra_env,
    ssh_target,
    remote_env,
    executor,
    agent_env_id,
):
    if env is None:
        return {
            "ok": False,
            "error": f"{op.id} requires an environment",
            "returncode": 2,
        }
    from app.services import baremetal as baremetal_service

    node = baremetal_service.get_node(self.db, env, params.get("node_id"))
    if node is None:
        msg = f"{op.id}: unknown node id {params.get('node_id')!r} for this environment"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    audit_action = ""
    details: dict[str, Any] = {
        "node_id": node.id,
        "node": node.name,
        "dry_run": dry,
    }
    if handler == "baremetal_node_power":
        action = str(params.get("action") or "").strip().lower()
        result = baremetal_service.power_action(
            self.db, node, action, dry_run=dry, log=log, settings=self.settings
        )
        audit_action = "env.baremetal.power"
        details["action"] = action
    elif handler == "baremetal_node_pxe_boot":
        result = baremetal_service.pxe_boot(
            self.db, node, dry_run=dry, log=log, settings=self.settings
        )
        audit_action = "env.baremetal.pxe_boot"
    elif handler == "baremetal_node_next_boot":
        target = str(params.get("next_boot") or "").strip().lower()
        raw_now = params.get("boot_now")
        if isinstance(raw_now, str):
            boot_now = raw_now.strip().lower() in ("1", "true", "yes", "on")
        else:
            boot_now = bool(raw_now)
        result = baremetal_service.set_next_boot(
            self.db,
            env,
            node,
            target,
            boot_now=boot_now,
            dry_run=dry,
            log=log,
            settings=self.settings,
        )
        audit_action = "env.baremetal.next_boot"
        details["next_boot"] = target
        details["boot_now"] = boot_now
    elif handler == "baremetal_node_iso_boot":
        result = baremetal_service.iso_boot(
            self.db,
            env,
            node,
            dry_run=dry,
            log=log,
            settings=self.settings,
            image_url=str(params.get("image_url") or "").strip() or None,
        )
        audit_action = "env.baremetal.iso_boot"
    else:
        raw_roles = params.get("roles") or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split(",")
        roles = [str(r).strip().lower() for r in raw_roles if str(r).strip()]
        from app.services import envconfig as envconfig_service

        invalid = [
            r for r in roles if r not in envconfig_service.VALID_SERVER_ROLES
        ]
        if invalid:
            valid = ", ".join(sorted(envconfig_service.VALID_SERVER_ROLES))
            return {
                "ok": False,
                "error": (
                    f"baremetal.node.provision: unknown role(s) "
                    f"{', '.join(invalid)} (valid: {valid})"
                ),
                "returncode": 2,
            }
        stop_after = str(params.get("stop_after") or "").strip().lower()
        result = baremetal_service.provision(
            self.db,
            env,
            node,
            roles=roles,
            actor=job.created_by or "system",
            dry_run=dry,
            log=log,
            settings=self.settings,
            timeout_seconds=timeout,
            stop_after=stop_after,
        )
        audit_action = "env.baremetal.provision"
        details["roles"] = roles
        details["stop_after"] = stop_after
    self.write_audit(
        actor=job.created_by or "system",
        action=audit_action,
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details=details,
        success=bool(result.get("ok")),
    )
    return result
