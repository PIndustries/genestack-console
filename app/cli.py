"""Simple Genestack Console CLI.

Usage:
    python -m app.cli list-ops
    python -m app.cli create-env --name lab --description "local lab"
    python -m app.cli create-tenant --name acme
    python -m app.cli create-user --username alice --password secret [--platform-admin]
    python -m app.cli add-member --username alice --tenant acme --role operator
    python -m app.cli health
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Genestack Console operator CLI (local / dry-run friendly)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-ops", help="Print the operation catalog")

    create_env = sub.add_parser(
        "create-env", help="Create an environment in the local DB"
    )
    create_env.add_argument("--name", required=True, help="Unique environment name")
    create_env.add_argument("--description", default="", help="Optional description")
    create_env.add_argument("--region", default=None)
    create_env.add_argument("--tier", default=None)

    create_tenant = sub.add_parser(
        "create-tenant", help="Create a tenant in the local DB"
    )
    create_tenant.add_argument("--name", required=True, help="Unique tenant name")
    create_tenant.add_argument("--description", default="", help="Optional description")

    create_user = sub.add_parser("create-user", help="Create a local user account")
    create_user.add_argument("--username", required=True, help="Unique username")
    create_user.add_argument("--password", required=True, help="Login password")
    create_user.add_argument(
        "--platform-admin",
        action="store_true",
        help="Grant platform admin (bypasses tenancy; break-glass)",
    )

    add_member = sub.add_parser("add-member", help="Add a user to a tenant with a role")
    add_member.add_argument("--username", required=True, help="Existing username")
    add_member.add_argument("--tenant", required=True, help="Tenant name")
    add_member.add_argument(
        "--role",
        default="viewer",
        choices=["viewer", "operator", "admin"],
        help="Role within the tenant",
    )

    sub.add_parser(
        "health", help="Print local health settings (no HTTP server required)"
    )

    sub.add_parser(
        "make-config",
        help="Print a ready-to-run config.yaml (rotated secret_key + API keys) to stdout",
    )

    sub.add_parser(
        "seed-demo",
        help="Create or fill the walkthrough sample tenant/env (idempotent)",
    )

    return parser


def cmd_list_ops() -> int:
    from app.services.catalog import get_operation_catalog

    catalog = get_operation_catalog()
    rows: list[dict[str, Any]] = []
    for op in catalog:
        rows.append(
            {
                "id": op.id,
                "name": op.name,
                "required_role": op.required_role,
                "backend": op.backend,
                "description": op.description,
            }
        )
    print(json.dumps(rows, indent=2))
    print(f"# {len(rows)} operations", file=sys.stderr)
    return 0


def cmd_create_env(args: argparse.Namespace) -> int:
    from app.db import SessionLocal, init_db
    from app.models import Environment
    from sqlalchemy import select

    init_db()
    db = SessionLocal()
    try:
        existing = db.scalar(select(Environment).where(Environment.name == args.name))
        if existing:
            print(
                json.dumps(
                    {
                        "error": "already exists",
                        "id": existing.id,
                        "name": existing.name,
                    }
                )
            )
            return 1
        env = Environment(
            name=args.name,
            description=args.description or None,
            region=args.region,
            tier=args.tier,
            metadata_json={},
        )
        db.add(env)
        db.commit()
        db.refresh(env)
        print(
            json.dumps(
                {
                    "id": env.id,
                    "name": env.name,
                    "description": env.description,
                    "region": env.region,
                    "tier": env.tier,
                },
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


def cmd_create_tenant(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import Tenant

    init_db()
    db = SessionLocal()
    try:
        existing = db.scalar(select(Tenant).where(Tenant.name == args.name))
        if existing:
            print(
                json.dumps(
                    {
                        "error": "already exists",
                        "id": existing.id,
                        "name": existing.name,
                    }
                )
            )
            return 1
        tenant = Tenant(name=args.name, description=args.description or None)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(
            json.dumps(
                {
                    "id": tenant.id,
                    "name": tenant.name,
                    "description": tenant.description,
                },
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


def cmd_create_user(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import User
    from app.services import accounts

    init_db()
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.username == args.username))
        if existing:
            print(
                json.dumps(
                    {
                        "error": "already exists",
                        "id": existing.id,
                        "username": existing.username,
                    }
                )
            )
            return 1
        user = accounts.create_user(
            db, args.username, args.password, platform_admin=args.platform_admin
        )
        db.commit()
        db.refresh(user)
        print(
            json.dumps(
                {
                    "id": user.id,
                    "username": user.username,
                    "platform_admin": user.platform_admin,
                },
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


def cmd_add_member(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import Membership, Tenant, User, UserRole

    init_db()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(json.dumps({"error": "user not found", "username": args.username}))
            return 1
        tenant = db.scalar(select(Tenant).where(Tenant.name == args.tenant))
        if tenant is None:
            print(json.dumps({"error": "tenant not found", "tenant": args.tenant}))
            return 1
        existing = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.tenant_id == tenant.id
            )
        )
        if existing:
            print(
                json.dumps(
                    {
                        "error": "already a member",
                        "username": user.username,
                        "tenant": tenant.name,
                        "role": existing.role.value,
                    }
                )
            )
            return 1
        membership = Membership(
            user_id=user.id, tenant_id=tenant.id, role=UserRole(args.role)
        )
        db.add(membership)
        db.commit()
        print(
            json.dumps(
                {"username": user.username, "tenant": tenant.name, "role": args.role},
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


def cmd_health() -> int:
    from app import __version__
    from app.config import get_settings

    settings = get_settings()
    print(
        json.dumps(
            {
                "status": "ok",
                "version": __version__,
                "dry_run": settings.dry_run,
                "genestack_root": settings.genestack_root,
                "ansible_root": settings.ansible_root,
                "database_url": settings.database_url,
            },
            indent=2,
        )
    )
    return 0


def cmd_seed_demo() -> int:
    """Create or refill the labeled walkthrough sample. Ignores seed_demo flag."""
    from app.db import SessionLocal, init_db
    from app.services.demo import seed_demo

    init_db()
    db = SessionLocal()
    try:
        result = seed_demo(db)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        db.close()


def cmd_make_config(args: argparse.Namespace) -> int:
    """Emit a ready-to-run config.yaml (rotated secrets) on stdout.

    The body is config.yaml.example with every REPLACE_ME placeholder filled:
    a fresh Fernet secret_key and three random API keys. Loopback-only by
    design (server.host 127.0.0.1) — expose via VPN or a reverse proxy.
    """
    import base64
    import secrets
    from datetime import datetime, timezone
    from pathlib import Path

    example_path = Path(__file__).resolve().parents[1] / "config.yaml.example"
    text = example_path.read_text(encoding="utf-8")
    text = text.replace(
        "secret_key: REPLACE_ME-generated-per-install",
        f"secret_key: {base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}",
    )
    roles = ("admin", "operator", "viewer")
    for role in roles:
        text = text.replace(
            f"gsc-{role}-REPLACE_ME", f"gsc-{role}-{secrets.token_urlsafe(24)}"
        )
    if "REPLACE_ME" in text:
        print(
            "ERROR: config.yaml.example still carries a REPLACE_ME placeholder — "
            "run `make-config` from a clean checkout.",
            file=sys.stderr,
        )
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(
        f"# Generated by `python -m app.cli make-config` on {stamp}.\n{text.lstrip()}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-ops":
        return cmd_list_ops()
    if args.command == "create-env":
        return cmd_create_env(args)
    if args.command == "create-tenant":
        return cmd_create_tenant(args)
    if args.command == "create-user":
        return cmd_create_user(args)
    if args.command == "add-member":
        return cmd_add_member(args)
    if args.command == "health":
        return cmd_health()
    if args.command == "make-config":
        return cmd_make_config(args)
    if args.command == "seed-demo":
        return cmd_seed_demo()
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
