"""What: Push config, run the install scripts, and check the cloud.
Where: app/modules/genestack/__init__.py. This class lists the function files below, in
order.
Why: Each install step is a file, so the job runner does not grow when a step is added.
"""

from __future__ import annotations

from app.modules.base import Module


class GenestackModule(Module):
    name = "genestack"
    functions = (
        "components_desired",
        "components_reconcile",
        "service_enable",
        "scripts_list",
        "repo_scripts_list",
        "repo_script_run",
        "smoke",
        "host_prepare",
        "host_setup",
        "services_list",
        "cluster_status",
        "pipeline_run",
        "verify",
        "tempest",
        "k8s_upgrade",
        "backup_mariadb",
        "hyperconverged_lab",
        "deploy",
        "greenfield",
        "registry_mirror",
        "talos_bootstrap",
        "config_push",
        "state_export",
    )
