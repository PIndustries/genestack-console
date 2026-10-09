"""Names and notes stored in the console vault.

Values stay encrypted. This module does not talk to an outside password
manager.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BaremetalNode, Environment, HardwareAccount, VaultItem
from app.services.crypto import encrypt_secret


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
