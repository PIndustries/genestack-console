"""Operation catalog.

The operation definitions live next to the code that runs them, in
``app/modules``. This module answers which operations exist and keeps
the allowlists other code imports from here.
"""

from __future__ import annotations

from typing import Any

from app.schemas import OperationSpec

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


def _items() -> list[dict[str, Any]]:
    """Load operation dicts from modules.

    Importing this module does not load ``app.modules``. The first caller does.
    Built-in operations keep their original order. An extra module's operations
    come after those.
    """
    from app.modules import iter_operations
    from app.modules.order import sort_key

    return sorted(iter_operations(), key=lambda item: sort_key(str(item["id"])))


def get_operation_catalog() -> list[OperationSpec]:
    """Return typed operation catalog."""
    return [OperationSpec.model_validate(item) for item in _items()]


def get_operation(operation_id: str) -> OperationSpec | None:
    for item in _items():
        if item["id"] == operation_id:
            return OperationSpec.model_validate(item)
    return None


def get_handler_key(operation_id: str) -> str | None:
    op = get_operation(operation_id)
    return op.handler if op else None


def mutating_operation_ids() -> frozenset[str]:
    """Ids of catalog operations that mutate the target environment."""
    return frozenset(
        item["id"] for item in _items() if item.get("mutating")
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
    for item in _items():
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
