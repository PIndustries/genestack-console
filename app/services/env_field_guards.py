"""Fail-closed guards for environment create/PATCH fields.

Blocks kubeconfig exec plugins, SSH option injection via deployer_ssh_user /
deployer_ssh_host, and shell-meta / path-traversal values in path fields.
Called from Pydantic validators on EnvironmentCreate / EnvironmentUpdate so
dangerous values never reach the DB.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Imported lazily-safe constant; schemas also defines it — keep a local copy of
# the mask string so this module does not import app.schemas (cycle risk).
_MASKED_KUBECONFIG = "***"

_SSH_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
# Hostnames, IPv4, IPv6 (with optional brackets), and dotted names — no spaces
# or SSH option flags.
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]%-]+$")
# Control chars + classic shell metacharacters that turn a path into a command.
_PATH_SHELL_META = re.compile(r"[\x00-\x1f;|&$`<>(){}!\n\r]")


def refuse_dangerous_ssh_user(value: str | None) -> str | None:
    """Accept a plain POSIX username only — never SSH options / ProxyCommand."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    lowered = text.lower()
    if (
        "proxycommand" in lowered
        or "proxyjump" in lowered
        or "-o" in text
        or " " in text
        or "\t" in text
        or "@" in text
        or text.startswith("-")
    ):
        raise ValueError(
            "deployer_ssh_user must be a plain username "
            "(SSH options such as ProxyCommand are refused)"
        )
    if not _SSH_USER_RE.fullmatch(text) or len(text) > 64:
        raise ValueError(
            "deployer_ssh_user must be a plain username (letters, digits, underscore, dot, hyphen)"
        )
    return text


def refuse_dangerous_ssh_host(value: str | None) -> str | None:
    """Accept a hostname / IP only — never SSH options."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    lowered = text.lower()
    if (
        "proxycommand" in lowered
        or "proxyjump" in lowered
        or "-o" in text
        or " " in text
        or "\t" in text
        or "@" in text
        or text.startswith("-")
    ):
        raise ValueError(
            "deployer_ssh_host must be a hostname or IP "
            "(SSH options such as ProxyCommand are refused)"
        )
    if not _SSH_HOST_RE.fullmatch(text) or len(text) > 253:
        raise ValueError("deployer_ssh_host must be a hostname or IP address")
    return text


def refuse_dangerous_path(value: str | None, *, field: str) -> str | None:
    """Refuse path traversal and shell metacharacters in filesystem path fields."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    if _PATH_SHELL_META.search(text):
        raise ValueError(f"{field} contains forbidden control or shell-meta characters")
    for part in re.split(r"[/\\]", text):
        if part == "..":
            raise ValueError(f"{field} must not contain '..' path segments")
    if len(text) > 1024:
        raise ValueError(f"{field} exceeds maximum length")
    return text


def _user_entry_has_exec(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    user = entry.get("user")
    return bool(isinstance(user, dict) and "exec" in user)


def refuse_kubeconfig_exec_plugins(value: str | None) -> str | None:
    """Refuse kubeconfig blobs that enable client-go exec credential plugins."""
    if value is None:
        return None
    text = str(value)
    if text == _MASKED_KUBECONFIG:
        return text
    # Already-at-rest ciphertext is not re-validated as YAML.
    if text.startswith("fernet:"):
        return text
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"kubeconfig_data must be valid YAML: {exc}") from exc
    if data is None:
        return text
    if not isinstance(data, dict):
        raise TypeError("kubeconfig_data must be a kubeconfig mapping")
    users = data.get("users")
    if isinstance(users, list) and any(_user_entry_has_exec(u) for u in users):
        raise ValueError(
            "kubeconfig_data must not enable exec plugins (users[].user.exec is refused)"
        )
    return text


def apply_environment_field_guards(
    *,
    deployer_ssh_user: str | None = None,
    deployer_ssh_host: str | None = None,
    kubeconfig_data: str | None = None,
    kubeconfig_path: str | None = None,
    genestack_path: str | None = None,
    genestack_config_dir: str | None = None,
    state_repo_path: str | None = None,
) -> dict[str, str | None]:
    """Validate the security-sensitive environment fields; return cleaned values."""
    return {
        "deployer_ssh_user": refuse_dangerous_ssh_user(deployer_ssh_user),
        "deployer_ssh_host": refuse_dangerous_ssh_host(deployer_ssh_host),
        "kubeconfig_data": refuse_kubeconfig_exec_plugins(kubeconfig_data),
        "kubeconfig_path": refuse_dangerous_path(kubeconfig_path, field="kubeconfig_path"),
        "genestack_path": refuse_dangerous_path(genestack_path, field="genestack_path"),
        "genestack_config_dir": refuse_dangerous_path(
            genestack_config_dir, field="genestack_config_dir"
        ),
        "state_repo_path": refuse_dangerous_path(state_repo_path, field="state_repo_path"),
    }
