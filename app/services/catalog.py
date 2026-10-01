"""Operation catalog — authoritative list of console operations."""

from __future__ import annotations

from typing import Any

from app.schemas import OperationSpec
from app.services.agents import AGENT_COMMAND_ALLOWLIST
from app.services.repo_scripts import SAFE_REPO_SCRIPTS
from app.services.redfish import RESET_TYPES

# Backward-compat export only (imported by older tests/consumers). Enforcement
# for genestack.service.enable now uses discovered bin/install-*.sh scripts —
# see service_registry.discover_deployable_services.
SERVICE_ENABLE_ALLOWLIST = frozenset({"placement", "keystone", "glance", "skyline"})

# Ansible playbooks allowed via ansible.playbook.run
PLAYBOOK_ALLOWLIST = frozenset(
    {
        # Console-owned playbooks (genestack-console/ansible/playbooks)
        "host_preflight.yml",
        "basic_ops.yml",
        "provision_bridge.yml",
        # Existing Genestack core playbooks (resolved under GENESTACK_ROOT/ansible/playbooks)
        "host-setup.yml",
    }
)


def _p(
    name: str,
    required: bool = False,
    description: str = "",
    type_: str = "string",
    *,
    default: Any = None,
    enum: list[Any] | None = None,
) -> dict:
    return {
        "name": name,
        "required": required,
        "description": description,
        "type": type_,
        "default": default,
        "enum": enum,
    }


OPERATION_CATALOG_RAW: list[dict[str, Any]] = [
    {
        "id": "internal.health",
        "name": "Internal Health Check",
        "description": "Return console health and path configuration (no side effects).",
        "required_role": "viewer",
        "backend": "internal",
        "params": [],
        "handler": "internal_health",
    },
    {
        "id": "console.release",
        "name": "Compile and publish Console binary",
        "description": (
            "On this hub: compile the Console to a Linux binary under dist/. "
            "Published installs download the GitHub Release asset. "
            "Runs locally on the hub (not on an environment)."
        ),
        "required_role": "admin",
        "backend": "internal",
        "params": [],
        "handler": "console_release",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "console.backup",
        "name": "Backup console database + config",
        "description": (
            "Run scripts/backup-console.sh on this hub: online "
            "SQLite backup (or pg_dump) plus config.yaml into "
            "<backup_dir>/<UTC-stamp>/ with --keep retention (default 7). "
            "Env-less (console host only). Dry-run prints destination and "
            "retention only. This operation does not schedule itself."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p(
                "backup_dir",
                False,
                "Destination root for stamp dirs (default: <host_prefix>/backups/console)",
            ),
            _p(
                "keep",
                False,
                "Retention: newest N stamp directories to keep (default 7)",
                "integer",
                default=7,
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of global dry-run pin",
                "boolean",
            ),
        ],
        "handler": "console_backup",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "console.vacuum",
        "name": "Vacuum console SQLite database",
        "description": (
            "Run scripts/vacuum-console.sh on this hub: "
            "PRAGMA wal_checkpoint(TRUNCATE) then VACUUM (SQLite). Dry-run "
            "reports page_count/freelist stats only — no mutate. Admin-only. "
            "Prefer running retention sweep before vacuum; take console.backup "
            "after. This operation does not schedule itself."
        ),
        "required_role": "admin",
        "backend": "internal",
        "params": [
            _p(
                "dry_run",
                False,
                "Force stats-only rehearsal regardless of global dry-run pin",
                "boolean",
            ),
        ],
        "handler": "console_vacuum",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machines.list",
        "name": "List MAAS Machines",
        "description": "List machines from the environment MAAS (or default console MAAS).",
        "required_role": "viewer",
        "backend": "maas",
        "params": [
            _p("environment_id", False, "Override environment for MAAS credentials"),
        ],
        "handler": "maas_machines_list",
    },
    {
        "id": "maas.machine.power_status",
        "name": "MAAS Machine Power Status",
        "description": "Query power status for a MAAS machine system_id.",
        "required_role": "viewer",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
            _p("environment_id", False, "Environment providing MAAS credentials"),
        ],
        "handler": "maas_machine_power_status",
    },
    {
        "id": "maas.machine.commission",
        "name": "Commission MAAS Machine",
        "description": "Commission a MAAS machine (op=commission) for the environment.",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
        ],
        "handler": "maas_machine_commission",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machine.deploy",
        "name": "Deploy MAAS Machine",
        "description": (
            "Deploy a MAAS machine (op=deploy) with optional hostname and Genestack "
            "roles; renders cloud-init user-data and upserts the env config doc "
            "servers section so deploy -> inventory is one action. With image set "
            "(an uploaded custom boot-resource such as a Talos factory image), "
            "deploys osystem=custom and skips cloud-init user-data."
        ),
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
            _p("hostname", False, "Hostname to set on deploy"),
            _p(
                "roles",
                False,
                "Genestack roles: k8s_control_plane/etcd/control/compute/network/storage",
                "array",
            ),
            _p(
                "image",
                False,
                "Uploaded custom image name (e.g. talos-genestack); skips cloud-init",
            ),
        ],
        "handler": "maas_machine_deploy",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.talos.image_upload",
        "name": "Upload Talos Factory Image to MAAS",
        "description": (
            "Zero-touch talos provisioning, step 1: download the Talos Image "
            "Factory image over HTTPS (bounded size, sha256 logged) and upload "
            "it to the environment's MAAS as a custom boot-resource. Then "
            "deploy machines with maas.machine.deploy image=<name>. The image "
            "URL defaults to the env config doc talos.image_url when the param "
            "is omitted."
        ),
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p(
                "image_url",
                False,
                "Talos factory image URL (default: env config doc talos.image_url)",
            ),
        ],
        "handler": "maas_talos_image_upload",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machine.release",
        "name": "Release MAAS Machine",
        "description": "Release a MAAS machine back to the pool (op=release).",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
        ],
        "handler": "maas_machine_release",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "host.preflight",
        "name": "Host Preflight",
        "description": "Run ansible playbook host_preflight.yml against inventory hosts.",
        "required_role": "operator",
        "backend": "ansible",
        "params": [
            _p("limit", False, "Ansible --limit pattern"),
            _p("extra_vars", False, "Extra vars as JSON object", "object"),
        ],
        "handler": "host_preflight",
    },
    {
        "id": "host.basic_ops",
        "name": "Host Basic Ops",
        "description": "Run ansible basic_ops.yml with an action parameter.",
        "required_role": "operator",
        "backend": "ansible",
        "params": [
            _p(
                "action",
                True,
                "Action passed to basic_ops (ping, facts, disk_check, all)",
                enum=["ping", "facts", "disk_check", "all"],
            ),
            _p("limit", False, "Ansible --limit pattern"),
            _p("extra_vars", False, "Extra vars as JSON object", "object"),
        ],
        "handler": "host_basic_ops",
        "mutating": True,
    },
    {
        "id": "genestack.components.desired",
        "name": "Desired Components",
        "description": "Read openstack-components.yaml from GENESTACK_ROOT or env genestack_path.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_components_desired",
    },
    {
        "id": "genestack.service.enable",
        "name": "Enable Genestack Service",
        "description": (
            "Enable/install an OpenStack service via bin/install-<service>.sh. "
            "The allowlist is now all discovered install scripts under "
            "GENESTACK_ROOT/bin (excluding service-template)."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p(
                "service",
                True,
                "Service name (must have a discovered bin/install-<service>.sh)",
            ),
        ],
        "handler": "genestack_service_enable",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.scripts.list",
        "name": "List Genestack Install Scripts",
        "description": "List bin/install-*.sh scripts under GENESTACK_ROOT.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_scripts_list",
    },
    {
        "id": "genestack.repo_scripts.list",
        "name": "List Genestack Repo Scripts",
        "description": (
            "Inventory genestack's utility/maintenance tooling under "
            "GENESTACK_ROOT: scripts/*.sh utilities (with first-comment "
            "description), maintenances/*.txt runbooks (with title), and "
            "ops-tools/** helpers. Read-only."
        ),
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_repo_scripts_list",
    },
    {
        "id": "genestack.repo_script.run",
        "name": "Run Genestack Repo Script (curated)",
        "description": (
            "Run a curated genestack utility script as 'bash scripts/<name> "
            "[args]' from GENESTACK_ROOT. The script must exist under scripts/ "
            "and be in SAFE_REPO_SCRIPTS (app/services/repo_scripts.py — the "
            f"extension point). Allowlist: {', '.join(sorted(SAFE_REPO_SCRIPTS))}."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("script", True, "Script basename under scripts/ (must be allowlisted)"),
            _p("args", False, "Extra arguments appended verbatim (shlex-split)"),
        ],
        "handler": "genestack_repo_script_run",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.smoke",
        "name": "Genestack Smoke Checks",
        "description": "Lightweight smoke validation of genestack paths and key artifacts.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_smoke",
    },
    {
        "id": "genestack.components.list",
        "name": "List Components (alias)",
        "description": "Alias for genestack.components.desired.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_components_desired",
    },
    {
        "id": "genestack.components.reconcile",
        "name": "Reconcile Components",
        "description": (
            "Diff the env config doc's components: block against deployed helm "
            "releases and converge the cloud to the desired state. Default "
            "(apply=false) is plan-only: the plan is logged and nothing is "
            "executed. apply=true enables missing components via the "
            "genestack.service.enable path and helm-uninstalls undesired "
            "releases (protected core components are never uninstalled). "
            "Env/global dry_run forces plan-only even with apply=true."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p(
                "apply", False, "Execute the plan (default false: plan-only)", "boolean"
            ),
        ],
        "handler": "genestack_components_reconcile",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "genestack.services.list",
        "name": "Genestack Service Registry",
        "description": (
            "Build the service registry from bin/install-*.sh headers, chart "
            "versions, and desired state; returns counts per category."
        ),
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_services_list",
    },
    {
        "id": "genestack.cluster.status",
        "name": "Cluster Status",
        "description": "Probe cluster reachability, nodes, and namespaces via kubectl.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_cluster_status",
    },
    {
        "id": "genestack.pipeline.run",
        "name": "Run Provisioning Pipeline Stage",
        "description": (
            "Run one curated provisioning stage sequentially (install scripts). "
            "Stage ids: see GET /api/v1/genestack/pipeline."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("stage", True, "Pipeline stage id (e.g. hosts, core, observability)"),
        ],
        "handler": "genestack_pipeline_run",
        "mutating": True,
        "timeout_seconds": 14400,
    },
    {
        "id": "ansible.playbook.run",
        "name": "Run Allowlisted Playbook",
        "description": f"Run an allowlisted ansible playbook. Allowlist: {', '.join(sorted(PLAYBOOK_ALLOWLIST))}.",
        "required_role": "operator",
        "backend": "ansible",
        "params": [
            _p(
                "playbook",
                True,
                "Playbook filename (must be allowlisted)",
                enum=sorted(PLAYBOOK_ALLOWLIST),
            ),
            _p("limit", False, "Ansible --limit pattern"),
            _p("extra_vars", False, "Extra vars as JSON object", "object"),
            _p("tags", False, "Comma-separated ansible tags"),
        ],
        "handler": "ansible_playbook_run",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.config.push",
        "name": "Push Env Config Document",
        "description": (
            "Render the environment's portal-managed config document and write "
            "the files to its config dir (over the ssh executor when a deploy "
            "host is set), backing up pre-existing files first."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_config_push",
        "mutating": True,
    },
    {
        "id": "genestack.state.export",
        "name": "Export State to Repo",
        "description": (
            "Render the environment's current portal-managed config document "
            "and write the files under state/<env>/ in the repo checkout "
            "configured by state_repo_path, committing (and pushing to "
            "state_repo_remote when set) the result. When the environment "
            "resolves to a deploy host (connected agent, else ssh), the git "
            "work happens there instead of on the console host. Secret files "
            "(kubesecrets.yaml, .ssh keys) are intentionally excluded — the "
            "repo holds non-secret config; secrets remain portal-managed in "
            "the DB."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_state_export",
        "mutating": True,
    },
    {
        "id": "genestack.verify",
        "name": "Verify environment (genestack test suite)",
        "description": (
            "Run genestack's own scripts/tests/run-all-tests.sh as the official "
            "'did it work' check. Levels: quick, standard (default), full "
            "(full provisions real test resources, then cleans up)."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("level", False, "Test level: quick, standard (default), or full"),
        ],
        "handler": "genestack_verify",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.tempest",
        "name": "Tempest conformance suite",
        "description": (
            "Run genestack's official OpenStack conformance suite (tempest). "
            "action=install deploys the openstack-helm tempest chart (release "
            "tempest, namespace openstack) via bin/install-tempest.sh without "
            "running tests; action=run re-installs with "
            "manifests.job_run_tests=true so Helm waits on the real "
            "tempest-run-tests job; install-run (default) does both. suite "
            "narrows the test scope by passing two chart values "
            "to the install phase (install-tempest.sh forwards extra args to "
            "helm): conf.whitelist[0] (the chart renders it into the "
            "tempest-etc secret, mounted at /etc/tempest/test-whitelist) and "
            "a rewritten conf.script ('tempest run --include-list "
            "/etc/tempest/test-whitelist --exclude-list ... -w 4') — the "
            "deployed script only consumes the blacklist plus --smoke, so "
            "without the rewrite the whitelist file would be mounted but "
            "ignored. suite=full (or omitting suite) keeps the chart default: "
            "the test-blacklist plus --smoke. suite requires action=install "
            "or install-run (a bare helm test re-runs the deployed values)."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p(
                "action",
                False,
                "install, run, or install-run (default)",
                default="install-run",
                enum=["install", "run", "install-run"],
            ),
            _p(
                "suite",
                False,
                "Test suite scope: a tempest test regex (e.g. "
                "tempest\\.scenario\\.test_server_basic_ops). Applied via "
                "conf.whitelist[0] + a conf.script rewrite to "
                "--include-list during the install phase; 'full' (or "
                "omitted) keeps the chart default (test-blacklist + "
                "--smoke). Not usable with action=run.",
            ),
        ],
        "handler": "genestack_tempest",
        "mutating": True,
        "timeout_seconds": 7200,
    },
    {
        "id": "genestack.talos.bootstrap",
        "name": "Talos Bootstrap (provider=talos)",
        "description": (
            "Bring up Kubernetes the talos way for a provider=talos env, per "
            "docs/k8s-talos.md: talosctl gen config, apply-config to each "
            "control-plane and worker node, set endpoints, bootstrap etcd "
            "(once), then fetch the kubeconfig — all from <config_dir>/talos "
            "on the deploy host. Control-plane nodes are doc servers with role "
            "k8s_control_plane; every other server is a worker. Cluster name "
            "and install disk come from the doc's talos: section (defaults: "
            "env name, /dev/sda). Note: pin kube-ovn to v1.14.10 for talos and "
            "boot nodes from a Talos Image Factory image with iscsi-tools + "
            "util-linux-tools extensions. Stops at the first failing phase."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_talos_bootstrap",
        "mutating": True,
        "timeout_seconds": 7200,
    },
    {
        "id": "genestack.k8s_upgrade",
        "name": "Kubernetes Upgrade (kubespray)",
        "description": (
            "Run kubespray's upgrade-cluster.yml from the genestack kubespray "
            "submodule against the environment inventory, per "
            "docs/k8s-kubespray-upgrade.md. One major version jump per run; "
            "2+ hours is normal. The target version comes from the inventory "
            "group_vars k8s_cluster.kube_version file — when kube_version is "
            "given, the console rewrites that line in "
            "<config_dir>/inventory/group_vars/k8s_cluster/k8s-cluster.yml "
            "(backing up the original as k8s-cluster.yml.bak-<timestamp> "
            "next to it) before running the playbook; on dry-run it only "
            "logs the intended change."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p(
                "kube_version",
                False,
                "Target Kubernetes version (e.g. 1.31.4) — written to "
                "k8s_cluster.kube_version in the env inventory group_vars "
                "k8s-cluster.yml before the upgrade (original backed up as "
                "k8s-cluster.yml.bak-<timestamp>); when omitted the "
                "inventory's current value is used as-is",
            ),
        ],
        "handler": "genestack_k8s_upgrade",
        "mutating": True,
        "timeout_seconds": 21600,
    },
    {
        "id": "genestack.hyperconverged_lab",
        "name": "Deploy Hyperconverged Lab",
        "description": (
            "Run genestack's DESTRUCTIVE lab deployer scripts/hyperconverged-lab.sh "
            "with a chosen platform: kubespray (Ubuntu VMs + kubespray/ansible) "
            "or talos (Talos Linux). Deploys a full hyperconverged OpenStack-on-"
            "Kubernetes lab on OpenStack infrastructure — it provisions VMs, "
            "k8s, and OpenStack, so only point it at a disposable lab cloud. "
            "include restricts which OpenStack services get installed (-i flag); "
            "extra_args is an advanced pass-through of additional script flags "
            "(they are wordsplit, quoted where needed). dry_run (or the env/"
            "global dry-run pin) logs the exact command without executing it."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("platform", True, "kubespray or talos", enum=["kubespray", "talos"]),
            _p(
                "include",
                False,
                "Comma-separated OpenStack services to include (script -i flag)",
            ),
            _p(
                "extra_args",
                False,
                "Advanced: extra script flags, e.g. '-x --envoy-gateway-config' "
                "(space-separated, wordsplit by the console)",
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "genestack_hyperconverged_lab",
        "mutating": True,
        "timeout_seconds": 14400,
    },
    {
        "id": "genestack.backup_mariadb",
        "name": "Backup MariaDB",
        "description": (
            "Run genestack's scripts/backup-mariadb.sh: dumps every database in "
            "the openstack namespace mariadb cluster (except performance_schema "
            "and information_schema) to $HOME/backup/mariadb/<timestamp> on the "
            "host it runs on."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_backup_mariadb",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.deploy",
        "name": "Deploy environment (push config + full pipeline)",
        "description": (
            "Helm restack: push config, then run pipeline stages. Required "
            "stages (hosts through compute-network) stop the job on failure. "
            "Optional extras/observability warn and continue. Tempest is not "
            "in this job — run genestack.tempest. from_stage / until_stage "
            "are the control points."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
            _p(
                "skip_push",
                False,
                "Run the pipeline without pushing config first",
                "boolean",
            ),
            _p(
                "from_stage",
                False,
                "Start the pipeline at this stage id (e.g. core) instead of hosts",
                "string",
            ),
            _p(
                "until_stage",
                False,
                "Stop after this stage id (run one control point)",
                "string",
            ),
            _p(
                "include_testing",
                False,
                "Also run Tempest as the last stage (default: skip; use genestack.tempest)",
                "boolean",
            ),
            _p(
                "parallelism",
                False,
                "Run up to N hosts in parallel (1..16, default 1 = sequential); "
                "keystone is always installed first, independently",
                "integer",
            ),
        ],
        "handler": "genestack_deploy",
        "mutating": True,
        "timeout_seconds": 21600,
    },
    {
        "id": "genestack.greenfield",
        "name": "Greenfield redeploy (PXE + wipe + Talos + OpenStack)",
        "description": (
            "DESTRUCTIVE. PXE-boot every inventory server; if DHCP/TFTP never "
            "show, iLO-mount the Talos ISO and continue (or OVH BYOI). A box "
            "that comes back k8s Ready is the old OS — not maintenance — and "
            "is remounted via iLO virtual CD. Wait for real Talos maintenance "
            "(:50000 up and not Ready), then run deploy from hosts. "
            "apply-config formats the Talos install disk. Requires a BMC row "
            "per server (or an OVH-bound env). Workloads are destroyed."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("dry_run", False, "Force a dry-run rehearsal", "boolean"),
            _p("skip_push", False, "Do not re-push the config document", "boolean"),
            _p(
                "boot",
                False,
                "auto (PXE; if DHCP/TFTP never show, iLO virtual CD), pxe, or iso",
                "string",
            ),
            _p(
                "parallelism",
                False,
                "OpenStack services parallelism after metal is up (1..16)",
                "integer",
            ),
        ],
        "handler": "genestack_greenfield",
        "mutating": True,
        "timeout_seconds": 21600,
    },
    {
        "id": "registry.mirror",
        "name": "Warm cluster image cache",
        "description": (
            "Start the Console pull-through registries and pull every image "
            "the live cluster already runs through them. Next greenfield "
            "boots from this cache instead of the internet. Does not skip "
            "observability or testing."
        ),
        "required_role": "admin",
        "backend": "internal",
        "params": [
            _p("dry_run", False, "List images without fetching", "boolean"),
        ],
        "handler": "registry_mirror",
        "mutating": True,
        "timeout_seconds": 21600,
    },
    {
        "id": "genestack.host_prepare",
        "name": "Prepare Deploy Host",
        "description": (
            "Take a fresh deploy host to 'ready for push + deploy': preflight "
            "tool checks, clone the genestack repo (fetch-only when already "
            "present unless update=true), then run bootstrap.sh to build the "
            "/etc/genestack skeleton. Mirrors docs/genestack-getting-started.md."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p(
                "repo_url",
                False,
                "Git repo URL (default env metadata repo_url, else rackerlabs/genestack)",
            ),
            _p(
                "repo_ref",
                False,
                "Branch/tag checked out on a fresh clone, or when update=true (default main)",
            ),
            _p(
                "genestack_path",
                False,
                "Remote clone path on the deploy host (default /opt/genestack)",
            ),
            _p(
                "config_dir",
                False,
                "Config dir (default env genestack_config_dir, else /etc/genestack)",
            ),
            _p(
                "update",
                False,
                "If true, reset an existing checkout to repo_url@repo_ref (fetch, checkout, hard reset)",
                "boolean",
            ),
        ],
        "handler": "genestack_host_prepare",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "genestack.host_setup",
        "name": "Genestack Host Setup",
        "description": (
            "Run core Genestack ansible/playbooks/host-setup.yml "
            "(host_setup role — same path operators use via setup-hosts.sh)."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("limit", False, "Ansible --limit pattern"),
            _p("check", False, "If true, ansible --check mode", "boolean"),
        ],
        "handler": "genestack_host_setup",
        "mutating": True,
        "timeout_seconds": 3600,
    },
    {
        "id": "openstack.servers.list",
        "name": "List OpenStack Servers (VMs)",
        "description": (
            "List Nova servers (VMs) running in the environment's cloud via "
            "OpenStack REST (native API), with kubectl exec CLI fallback."
        ),
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "openstack_servers_list",
    },
    {
        "id": "openstack.server.start",
        "name": "Start OpenStack Server",
        "description": "Start a stopped Nova server (openstack server start <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_start",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.stop",
        "name": "Stop OpenStack Server",
        "description": "Stop a running Nova server (openstack server stop <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_stop",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.reboot",
        "name": "Reboot OpenStack Server",
        "description": "Reboot a Nova server (openstack server reboot <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_reboot",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.delete",
        "name": "Delete OpenStack Server",
        "description": (
            "Delete a Nova server irreversibly (openstack server delete <server_id>)."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_delete",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "baremetal.node.register",
        "name": "Register Bare-Metal Node",
        "description": (
            "Register a bare-metal node (BMC address + Redfish credentials) "
            "for console-managed provisioning — no MAAS. The BMC password is "
            "fernet-encrypted at rest. When pxe_mac is omitted the node's "
            "ethernet MACs are probed over Redfish to autofill it (the probe "
            "is skipped on dry-run; a probe failure registers without a MAC)."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("name", True, "Node name (hostname-safe; becomes the inventory key)"),
            _p("bmc_host", True, "BMC address (host/IP or https:// URL)"),
            _p("bmc_username", True, "BMC (Redfish) username"),
            _p("bmc_password", True, "BMC (Redfish) password — encrypted at rest"),
            _p("pxe_mac", False, "PXE boot MAC (default: probed via Redfish)"),
        ],
        "secret_params": ("bmc_password",),
        "handler": "baremetal_node_register",
        "mutating": True,
    },
    {
        "id": "baremetal.nodes.list",
        "name": "List Bare-Metal Nodes",
        "description": (
            "List the environment's registered bare-metal nodes and their "
            "states (registered|booting|talos-ready|failed). The UI also "
            "reads these via GET /api/v1/environments/{id}/baremetal."
        ),
        "required_role": "viewer",
        "backend": "baremetal",
        "params": [],
        "handler": "baremetal_nodes_list",
    },
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
            "ForceOff, wait until the chassis is Off, set one-shot PXE, then "
            "On (not ACPI restart — hung kernels ignore ForceRestart). State "
            "moves to booting. Console PXE DHCP/HTTP then assigns the "
            "reserved IP and serves Talos boot assets."
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
            "Full zero-touch per node: prepare in-process PXE, one-shot PXE "
            "via Redfish, then iLO virtual CD if DHCP/TFTP never show. Wait "
            "for the talos maintenance API at the node's expected_ip (port "
            "50000, insecure), then mark the node talos-ready and upsert it "
            "into the env config doc servers section (source=baremetal) so "
            "the talos bootstrap flow sees it. The node needs an expected_ip "
            "(assigned from the PXE pool) first."
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
        ],
        "handler": "baremetal_node_provision",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "baremetal.bmc_scan",
        "name": "Scan Subnet for BMCs",
        "description": (
            "Route a scan_bmc command to the environment's connected agent: "
            "it sweeps the given subnet (CIDR) for Redfish BMC endpoints and "
            "reports each find back as a bmc_found event into the discovery "
            "inbox (GET /api/v1/environments/{id}/discovery). The agent's "
            "found count lands in the job result. Fails cleanly when no "
            "agent is connected."
        ),
        "required_role": "operator",
        "backend": "baremetal",
        "params": [
            _p("subnet", True, "CIDR to sweep for BMCs, e.g. 10.0.0.0/24"),
        ],
        "handler": "baremetal_bmc_scan",
        "mutating": True,
        "timeout_seconds": 900,
    },
    {
        "id": "hardware.terraform.plan",
        "name": "Terraform Plan (bare metal)",
        "description": (
            "Plan Terraform for a saved Hardware account (AWS, Azure, GCP, "
            "or Rackspace). Decrypts credentials only in the job worker. "
            "Does not change inventory. Dry-run, or a missing terraform "
            "binary, returns a rehearsal payload without shelling out."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("account_id", True, "HardwareAccount id"),
            _p("environment_id", False, "Environment receiving the plan (job scope)"),
            _p("count", False, "Node count (default 1)", "integer"),
            _p("flavor", False, "Instance/flavor override"),
            _p("region", False, "Region override (default: the account region)"),
            _p("roles", False, "Genestack roles for planned nodes", "array"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "hardware_terraform_plan",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "hardware.terraform.apply",
        "name": "Terraform Apply (bare metal)",
        "description": (
            "Apply Terraform for a saved Hardware account (AWS, Azure, GCP, "
            "or Rackspace) and upsert resulting hosts into the environment "
            "config servers section (source=terraform), the same inventory "
            "PXE/OVH/static hosts already use. Dry-run without a terraform "
            "binary imports placeholder hosts; live apply without the binary "
            "fails. Credentials are never returned or logged."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("account_id", True, "HardwareAccount id"),
            _p(
                "environment_id",
                False,
                "Environment receiving imported hosts (job scope)",
            ),
            _p("count", False, "Node count (default 1)", "integer"),
            _p("flavor", False, "Instance/flavor override"),
            _p("region", False, "Region override (default: the account region)"),
            _p("roles", False, "Genestack roles for imported hosts", "array"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "hardware_terraform_apply",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "hostvm.list",
        "name": "List Host VMs (QEMU)",
        "description": (
            "Discover and list the QEMU virtual machines running on the "
            "console host (genestack lab/dev nodes) with live pid/cpu/rss "
            "stats merged onto the HostVM registry."
        ),
        "required_role": "viewer",
        "backend": "internal",
        "params": [],
        "handler": "hostvm_list",
    },
    {
        "id": "hostvm.start",
        "name": "Start Host VM",
        "description": (
            "Start a stopped host QEMU VM by replaying the argv captured at "
            "discovery (cwd=VM workdir, detached). Workdir must be under a "
            "configured hypervisor root."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_start",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "hostvm.stop",
        "name": "Stop Host VM",
        "description": (
            "Stop a running host QEMU VM: SIGTERM the live pid, escalating "
            "to SIGKILL after 10s."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_stop",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "hostvm.restart",
        "name": "Restart Host VM",
        "description": "Restart a host QEMU VM (stop, then start from the stored cmdline).",
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_restart",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "agent.status",
        "name": "Environment Agent Status",
        "description": (
            "Show the enrollment and live connection status of the "
            "environment's console agent (connected, last_seen, hostname, "
            "version). Read-only."
        ),
        "required_role": "viewer",
        "backend": "agent",
        "params": [],
        "handler": "agent_status",
    },
    {
        "id": "agent.command",
        "name": "Run Command on Environment Agent",
        "description": (
            "Run an allowlisted proof command on the environment's connected "
            "console agent over the agent channel; output streams into the "
            "job log and the agent's return code decides the job result. "
            f"Allowlist: {', '.join(sorted(AGENT_COMMAND_ALLOWLIST))}. "
            "Fails cleanly when no agent is connected."
        ),
        "required_role": "admin",
        "backend": "agent",
        "params": [
            _p(
                "command",
                True,
                "Allowlisted command, e.g. 'uptime' or 'kubectl get nodes'",
            ),
        ],
        "handler": "agent_command",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "agent.install",
        "name": "Push-Install Agent on Host",
        "description": (
            "Install the console agent on a host inside the environment over "
            "ssh: issues a fresh enrollment credential (replacing any "
            "existing one), then runs the curl-pipe installer on the target "
            "(curl <advertise>/agent | bash -s -- --hub ... --token ...). "
            "When the target cannot pull the script from the hub, the "
            "packaged agent/install.sh is streamed over the ssh stdin "
            "instead. Requires hub.advertise_url in config.yaml — the "
            "address agents can reach. The agent dials OUT to the hub, so "
            "this is how agents get onto networks the console cannot reach "
            "inbound. The raw token is masked (gsca_***) in the job log."
        ),
        "required_role": "admin",
        "backend": "agent",
        "params": [
            _p("host", True, "Target host (IP or name) the console can ssh to"),
            _p("ssh_user", False, "SSH user (default root)"),
            _p("ssh_port", False, "SSH port (default 22)", "integer"),
            _p("name", False, "Agent/container name (default: the host)"),
        ],
        "handler": "agent_install",
        "mutating": True,
        "timeout_seconds": 900,
    },
    {
        "id": "ovh.byoi.reinstall",
        "name": "OVH BYOI Reinstall",
        "description": (
            "Reinstall the environment's OVH dedicated servers via BYOI. "
            "For provider=talos this uses talos.image_url (or params.image_url) "
            "as customizations.imageURL with operatingSystem=byoi_64, then "
            "polls OVH install/status and the Talos maintenance API on :50000 "
            "until each node is ready. Catalog OS templates still work for "
            "non-Talos reinstalls. Requires an OVH-bound environment and a "
            "consumer key that includes POST /dedicated/server/*/reinstall "
            "(re-run Connect if the key is older than that rule)."
        ),
        "required_role": "admin",
        "backend": "internal",
        "params": [
            _p(
                "operating_system",
                False,
                "OVH OS template (default byoi_64 when a Talos image URL is set)",
            ),
            _p(
                "server_hostnames",
                False,
                "Only reinstall these config server hostnames (default: all OVH-owned servers)",
                "array",
            ),
            _p("image_url", False, "Override talos.image_url for this reinstall"),
            _p(
                "wait",
                False,
                "Wait for OVH install + Talos :50000 (default true)",
                "boolean",
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "ovh_byoi_reinstall",
        "mutating": True,
        "timeout_seconds": 10800,
    },
    {
        "id": "ovh.vrack.attach",
        "name": "OVH vRack Attach",
        "description": (
            "Attach this environment's OVH dedicated servers to a vRack so "
            "the private NICs share one L2 fabric. Rise boxes use the VNI "
            "(dedicatedServerInterface); older SKUs attach the whole server. "
            "Does not set the 802.1q tag — that is applied at Talos provision "
            "from ovh.vlan_id. Requires a consumer key with POST /vrack/*."
        ),
        "required_role": "admin",
        "backend": "internal",
        "params": [
            _p(
                "vrack", False, "vRack service name (default: ovh.vrack on the env doc)"
            ),
            _p(
                "server_hostnames",
                False,
                "Only attach these inventory hostnames (default: all OVH-owned servers)",
                "array",
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "ovh_vrack_attach",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "platform.talos.reboot",
        "name": "Talos Node Reboot",
        "description": (
            "Queue ``talosctl reboot --wait=false`` for one inventory node. "
            "Honors env/global dry_run (rehearsal only). Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Inventory / node hostname"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_reboot",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "platform.talos.shutdown",
        "name": "Talos Node Shutdown",
        "description": (
            "Queue ``talosctl shutdown --wait=false`` for one inventory node. "
            "Honors env/global dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Inventory / node hostname"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_shutdown",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "platform.talos.reset",
        "name": "Talos Node Reset",
        "description": (
            "Queue ``talosctl reset --wait=false`` (graceful/wipe/reboot flags). "
            "Wipes system disk by default. Honors dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Inventory / node hostname"),
            _p("graceful", False, "Leave etcd if possible (default true)", "boolean", default=True),
            _p("reboot", False, "Reboot after reset instead of halt (default false)", "boolean", default=False),
            _p("wipe", False, "Wipe system disk (default true)", "boolean", default=True),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_reset",
        "mutating": True,
        "timeout_seconds": 300,
    },
    {
        "id": "platform.talos.upgrade",
        "name": "Talos Node Upgrade",
        "description": (
            "Queue ``talosctl upgrade`` for one node (installer image from "
            "param or env talos.install_image). Honors dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Inventory / node hostname"),
            _p("image", False, "Installer image override (registry-style)"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_upgrade",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "platform.talos.upgrade_many",
        "name": "Talos Bulk Upgrade",
        "description": (
            "Queue Talos upgrades across nodes. mode=parallel (lab), sequential, "
            "or rolling (one-at-a-time; same as sequential in this slice). "
            "Honors dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("image", False, "Installer image override"),
            _p(
                "mode",
                False,
                "parallel | sequential | rolling (default rolling)",
                default="rolling",
                enum=["parallel", "sequential", "rolling"],
            ),
            _p(
                "names",
                False,
                "Node hostnames (default: all inventory servers)",
                "array",
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_upgrade_many",
        "mutating": True,
        "timeout_seconds": 7200,
    },
    {
        "id": "platform.talos.apply_config",
        "name": "Talos Apply Machine Config",
        "description": (
            "Queue ``talosctl apply-config --file … --mode <mode>`` for one node. "
            "Honors dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Inventory / node hostname"),
            _p("yaml", True, "Machineconfig YAML"),
            _p(
                "mode",
                False,
                "auto | staged | no-reboot | reboot (default auto)",
                default="auto",
                enum=["auto", "staged", "no-reboot", "reboot"],
            ),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "platform_talos_apply_config",
        "mutating": True,
        "timeout_seconds": 300,
    },
    {
        "id": "k8s.node.drain",
        "name": "Drain Kubernetes Node",
        "description": (
            "Queue a Kubernetes node drain (cordon + evict). Honors env/global "
            "dry_run. Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("name", True, "Kubernetes node name"),
            _p("ignore_daemonsets", False, "Skip DaemonSet pods (default true)", "boolean", default=True),
            _p("delete_emptydir", False, "Delete emptyDir pods (default false)", "boolean", default=False),
            _p("grace_period", False, "Pod termination grace period seconds", "integer", default=30),
            _p("timeout", False, "Drain timeout seconds", "integer", default=90),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "k8s_node_drain",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "k8s.apply",
        "name": "Apply Kubernetes YAML",
        "description": (
            "Queue server-side apply of YAML manifests. Honors env/global dry_run. "
            "Mutating; per-env lock."
        ),
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("yaml", True, "Kubernetes manifest YAML"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "k8s_apply",
        "mutating": True,
        "timeout_seconds": 300,
    },
    {
        "id": "app.deploy",
        "name": "Deploy Git-backed App",
        "description": (
            "Clone a linked GitHub repository and apply it to Kubernetes "
            "(Helm/Kustomize/manifests) or OpenStack (Heat/Terraform/Ansible)."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("app_id", True, "App id"),
            _p("force", False, "Redeploy even if SHA is unchanged", "boolean"),
        ],
        "handler": "app_deploy",
        "mutating": True,
        "timeout_seconds": 900,
    },
]


def get_operation_catalog() -> list[OperationSpec]:
    """Return typed operation catalog."""
    return [OperationSpec.model_validate(item) for item in OPERATION_CATALOG_RAW]


def get_operation(operation_id: str) -> OperationSpec | None:
    for item in OPERATION_CATALOG_RAW:
        if item["id"] == operation_id:
            return OperationSpec.model_validate(item)
    return None


def get_handler_key(operation_id: str) -> str | None:
    op = get_operation(operation_id)
    return op.handler if op else None


def mutating_operation_ids() -> frozenset[str]:
    """Ids of catalog operations that mutate the target environment."""
    return frozenset(
        item["id"] for item in OPERATION_CATALOG_RAW if item.get("mutating")
    )


def secret_param_names(operation_id: str) -> frozenset[str]:
    """Param names of ``operation_id`` that hold secrets (scrubbed at rest).

    Only baremetal.node.register takes a secret param today (bmc_password).
    MAAS ops read credentials from the env/settings, agent.install generates
    its enrollment token internally (masked in the job log), and the
    discovery bmc-creds path never goes through jobs.
    """
    op = get_operation(operation_id)
    return frozenset(op.secret_params) if op else frozenset()


def all_secret_param_names() -> frozenset[str]:
    """Union of every operation's secret param names (historical-row scrub)."""
    names: set[str] = set()
    for item in OPERATION_CATALOG_RAW:
        names.update(item.get("secret_params") or ())
    return frozenset(names)


def validate_params(operation_id: str, params: dict[str, Any] | None) -> list[str]:
    """Return list of validation error messages (empty if ok)."""
    op = get_operation(operation_id)
    if op is None:
        return [f"Unknown operation: {operation_id}"]
    params = params or {}
    errors: list[str] = []
    for p in op.params:
        if p.required and (p.name not in params or params[p.name] in (None, "")):
            errors.append(f"Missing required parameter: {p.name}")
    return errors
