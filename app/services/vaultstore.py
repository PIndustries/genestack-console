"""Names and records stored in the console vault.

Values stay encrypted. This module does not talk to an outside password
manager. A list returns names only. Reading one record returns its value
to the caller, which must not log it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BaremetalNode, Environment, HardwareAccount, VaultItem
from app.services.crypto import decrypt_secret, encrypt_secret


class VaultError(Exception):
    """A vault failure whose message is safe to show. It never includes a secret."""


def note_key(name: str) -> str:
    text = name.strip().strip("/")
    if text.startswith("secret/"):
        return text
    return f"secret/{text}"


def _name_row(
    name: str,
    kind: str,
    updated: datetime | None,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "environment_id": environment_id,
        "environment_name": environment_name,
        "updated_at": updated.isoformat() if updated else None,
    }


def list_names(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    """Names stored for one tenant. Values stay encrypted and are not returned."""
    rows: list[dict[str, Any]] = []
    envs = list(
        db.scalars(
            select(Environment)
            .where(Environment.tenant_id == tenant_id)
            .order_by(Environment.name)
        ).all()
    )
    env_names = {env.id: env.name for env in envs}
    for env in envs:
        if (env.ssh_private_key_encrypted or "").strip():
            rows.append(_name_row("ssh", "ssh", env.updated_at, env.id, env.name))
        nodes = db.scalars(
            select(BaremetalNode).where(BaremetalNode.environment_id == env.id)
        ).all()
        for node in nodes:
            if (node.bmc_password or "").strip():
                rows.append(
                    _name_row(
                        f"bmc/{node.name}",
                        "bmc",
                        node.updated_at,
                        env.id,
                        env.name,
                    )
                )
    accounts = db.scalars(
        select(HardwareAccount).where(HardwareAccount.tenant_id == tenant_id)
    ).all()
    for account in accounts:
        if (account.credentials_encrypted or "").strip():
            rows.append(
                _name_row(
                    f"hardware/{account.kind}/{account.name}",
                    "hardware",
                    account.updated_at,
                )
            )
    notes = db.scalars(select(VaultItem).where(VaultItem.tenant_id == tenant_id)).all()
    for note in notes:
        if (note.value_encrypted or "").strip():
            rows.append(
                _name_row(
                    note.name,
                    note.kind or "note",
                    note.updated_at,
                    note.environment_id,
                    env_names.get(note.environment_id or ""),
                )
            )
    rows.sort(
        key=lambda row: (
            1 if row["environment_name"] else 0,
            row["environment_name"] or "",
            row["name"],
        )
    )
    return rows


def save_note(
    db: Session,
    tenant_id: str,
    environment_id: str | None,
    name: str,
    value: str,
) -> VaultItem:
    """Store a note on the tenant, or on one of that tenant's environments."""
    scope: str | None = None
    if environment_id:
        env = db.get(Environment, environment_id)
        if env is None or env.tenant_id != tenant_id:
            raise VaultError("Choose an environment in this tenant.")
        scope = env.id
    key = note_key(name)
    if not value:
        raise VaultError("A secret needs a value.")
    stored = encrypt_secret(value)
    if not stored:
        raise VaultError("A secret needs a value.")
    row = db.scalar(
        select(VaultItem).where(
            VaultItem.tenant_id == tenant_id,
            VaultItem.environment_id == scope,
            VaultItem.name == key,
        )
    )
    if row is None:
        row = VaultItem(
            tenant_id=tenant_id,
            environment_id=scope,
            name=key,
            kind="note",
            value_encrypted=stored,
        )
        db.add(row)
    else:
        row.value_encrypted = stored
    db.commit()
    db.refresh(row)
    return row


def _environment(db: Session, tenant_id: str, environment_id: str) -> Environment:
    env = db.get(Environment, environment_id)
    if env is None or env.tenant_id != tenant_id:
        raise VaultError("Choose an environment in this tenant.")
    return env


def _classify(name: str) -> tuple[str, str]:
    """Return ``(kind, stored name)`` for one vault record name."""
    if name == "ssh":
        return "ssh", name
    if name.startswith("bmc/"):
        return "bmc", name
    if name in {"kubeconfig", "talosconfig"}:
        return name, name
    return "note", note_key(name)


def _node(db: Session, environment_id: str, name: str) -> BaremetalNode:
    node_name = name[len("bmc/") :]
    if not node_name:
        raise VaultError("That record is not in this vault.")
    node = db.scalar(
        select(BaremetalNode).where(
            BaremetalNode.environment_id == environment_id,
            BaremetalNode.name == node_name,
        )
    )
    if node is None:
        raise VaultError("That machine is not in this environment.")
    return node


def _item(
    db: Session, tenant_id: str, environment_id: str, name: str
) -> VaultItem | None:
    return db.scalar(
        select(VaultItem).where(
            VaultItem.tenant_id == tenant_id,
            VaultItem.environment_id == environment_id,
            VaultItem.name == name,
        )
    )


def _note_row(
    db: Session, tenant_id: str, environment_id: str, name: str
) -> VaultItem | None:
    exact = _item(db, tenant_id, environment_id, name)
    if exact is not None:
        return exact
    stored = note_key(name)
    if stored == name:
        return None
    return _item(db, tenant_id, environment_id, stored)


def _plain(stored: str | None) -> str:
    if not (stored or "").strip():
        raise VaultError("That record is not in this vault.")
    try:
        text = decrypt_secret(stored) or ""
    except (InvalidToken, ValueError, UnicodeError):
        raise VaultError("That record could not be read.") from None
    if not str(text).strip():
        raise VaultError("That record is not in this vault.")
    return str(text)


def read_record(
    db: Session, tenant_id: str, environment_id: str, name: str
) -> dict[str, str]:
    """Decrypt one record. The caller must not log the value."""
    env = _environment(db, tenant_id, environment_id)
    key = (name or "").strip()
    if not key:
        raise VaultError("That record is not in this vault.")
    kind, stored_name = _classify(key)
    if kind == "ssh":
        return {
            "name": "ssh",
            "kind": "ssh",
            "value": _plain(env.ssh_private_key_encrypted),
        }
    if kind == "bmc":
        node = _node(db, env.id, stored_name)
        return {
            "name": stored_name,
            "kind": "bmc",
            "value": _plain(node.bmc_password),
        }
    if kind in {"kubeconfig", "talosconfig"}:
        row = _item(db, tenant_id, env.id, stored_name)
        if row is None:
            raise VaultError("That record is not in this vault.")
        return {
            "name": stored_name,
            "kind": kind,
            "value": _plain(row.value_encrypted),
        }
    row = _note_row(db, tenant_id, env.id, key)
    if row is None:
        raise VaultError("That record is not in this vault.")
    return {
        "name": row.name,
        "kind": row.kind or "note",
        "value": _plain(row.value_encrypted),
    }


def write_record(
    db: Session, tenant_id: str, environment_id: str, name: str, value: str
) -> dict[str, str]:
    """Replace one record. The returned dict has no value."""
    env = _environment(db, tenant_id, environment_id)
    key = (name or "").strip()
    if not key:
        raise VaultError("A secret needs a name.")
    if not (value or "").strip():
        raise VaultError("A secret needs a value.")
    kind, stored_name = _classify(key)
    if kind == "ssh":
        from app.services.ssh_keys import apply_private_key

        try:
            apply_private_key(env, value)
        except ValueError as exc:
            raise VaultError(str(exc)) from None
        db.commit()
        return {"name": "ssh", "kind": "ssh"}
    if kind == "bmc":
        node = _node(db, env.id, stored_name)
        stored = encrypt_secret(value)
        if not stored:
            raise VaultError("A secret needs a value.")
        node.bmc_password = stored
        db.commit()
        return {"name": stored_name, "kind": "bmc"}
    if kind in {"kubeconfig", "talosconfig"}:
        from app.services.clientconfig import _acceptable, remember_client_config

        if not _acceptable(kind, value):
            raise VaultError(f"That value is not a {kind}.")
        if not remember_client_config(db, env, kind, value, commit=False):
            raise VaultError(f"That value is not a {kind}.")
        db.commit()
        return {"name": kind, "kind": kind}
    existing = _note_row(db, tenant_id, env.id, key)
    if existing is not None and existing.name != note_key(key):
        stored = encrypt_secret(value)
        if not stored:
            raise VaultError("A secret needs a value.")
        existing.value_encrypted = stored
        db.commit()
        return {"name": existing.name, "kind": existing.kind or "note"}
    row = save_note(db, tenant_id, env.id, key, value)
    return {"name": row.name, "kind": row.kind or "note"}


def delete_record(
    db: Session, tenant_id: str, environment_id: str, name: str
) -> dict[str, str]:
    """Remove one record. Client-certificate files on disk stay in place."""
    env = _environment(db, tenant_id, environment_id)
    key = (name or "").strip()
    if not key:
        raise VaultError("That record is not in this vault.")
    kind, stored_name = _classify(key)
    if kind == "ssh":
        if not (env.ssh_private_key_encrypted or "").strip():
            raise VaultError("That record is not in this vault.")
        env.ssh_private_key_encrypted = None
        env.ssh_public_key = None
        db.commit()
        return {"name": "ssh", "kind": "ssh"}
    if kind == "bmc":
        node = _node(db, env.id, stored_name)
        if not (node.bmc_password or "").strip():
            raise VaultError("That record is not in this vault.")
        node.bmc_password = ""
        db.commit()
        return {"name": stored_name, "kind": "bmc"}
    if kind in {"kubeconfig", "talosconfig"}:
        row = _item(db, tenant_id, env.id, stored_name)
        if row is None or not (row.value_encrypted or "").strip():
            raise VaultError("That record is not in this vault.")
        db.delete(row)
        db.commit()
        return {"name": kind, "kind": kind}
    row = _note_row(db, tenant_id, env.id, key)
    if row is None or not (row.value_encrypted or "").strip():
        raise VaultError("That record is not in this vault.")
    result = {"name": row.name, "kind": row.kind or "note"}
    db.delete(row)
    db.commit()
    return result
