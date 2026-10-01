#!/usr/bin/env python3
"""Rotate the console's at-rest secrets: Fernet key + static API keys.

Usage:
    python scripts/rotate_secrets.py [--config ./config.yaml] [--db PATH]
                                     [--old-secret-key KEY]
                                     [--old-admin KEY] [--old-operator KEY]
                                     [--old-viewer KEY]
                                     [--legacy-secret-key KEY]...
                                     [--dry-run]

What it does (real run):
  * generates a fresh Fernet ``secret_key`` and fresh gsc-admin/operator/viewer
    API keys (``gsc-<role>-`` + urlsafe token, same style as the current ones);
  * re-encrypts every ``fernet:``-prefixed value stored in the SQLite DB
    (decrypt with the old key, encrypt with the new one) — any value that does
    not decrypt with the old key aborts the run with a nonzero exit;
  * rewrites config.yaml in place via exact string replacement (all other
    lines and comments are preserved);
  * prints the new keys to stdout exactly once.

Idempotency safety: the script refuses to run unless the expected old keys
are all present in config.yaml, so it cannot be pointed at an already-rotated
(or the wrong) config by accident.

--legacy-secret-key (repeatable): accepts additional Fernet source keys for
tokens that were stored under a PREVIOUS secret_key — i.e. databases whose
config key was rotated in the past without re-encrypting stored secrets.
Such tokens are decrypted with the legacy key and re-encrypted under the new
one, which closes the exposure (legacy keys are often publicly known). A
token that decrypts under no provided key still aborts the run.

--dry-run reports what would change and touches nothing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import secrets
import sqlite3
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

CONSOLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE_DIR))

FERNET_TOKEN_RE = re.compile(r"fernet:[A-Za-z0-9_-]+={0,2}")
ROLES = ("admin", "operator", "viewer")


def make_fernet(secret_key: str) -> Fernet:
    """Mirror of app.services.crypto._fernet (key = sha256 of secret_key)."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


def new_api_key(role: str) -> str:
    return f"gsc-{role}-" + secrets.token_urlsafe(32)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="./config.yaml", help="path to config.yaml")
    parser.add_argument("--db", default=None, help="SQLite path (default: <data_dir>/console.db from config)")
    parser.add_argument("--old-secret-key", default=None, help="override expected current secret_key")
    for role in ROLES:
        parser.add_argument(f"--old-{role}", default=None, help=f"override expected current gsc-{role} key")
    parser.add_argument(
        "--legacy-secret-key",
        action="append",
        default=[],
        metavar="KEY",
        help="extra pre-rotation Fernet key for tokens encrypted under an older "
        "secret_key (repeatable); decrypts with the key that works, re-encrypts with the new one",
    )
    parser.add_argument("--dry-run", action="store_true", help="report changes, touch nothing")
    return parser.parse_args()


def load_config(config_path: Path) -> tuple[dict, str]:
    text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        die(f"config root must be a mapping: {config_path}")
    return data, text


def resolve_db_path(args: argparse.Namespace, config: dict) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    data_dir = config.get("data_dir", "./data")
    db_path = Path(str(data_dir)).expanduser()
    if not db_path.is_absolute():
        db_path = (CONSOLE_DIR / db_path).resolve()
    return db_path / "console.db"


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        die(f"config not found: {config_path}")
    config, text = load_config(config_path)

    old_secret = args.old_secret_key or config.get("secret_key", "")
    auth = config.get("auth") or {}
    api_keys = auth.get("api_keys") or {}
    if not isinstance(api_keys, dict):
        die("auth.api_keys in config is not a mapping")
    old_api = {role: args.__dict__.get(f"old_{role}") or "" for role in ROLES}

    for role in ROLES:
        matches = [k for k in api_keys if k.startswith(f"gsc-{role}-")]
        if len(matches) != 1:
            die(f"expected exactly one gsc-{role}-* key in config, found {matches}")
        old_api[role] = old_api[role] or matches[0]

    # Idempotency guard: every expected old key must be present verbatim.
    missing = []
    if old_secret not in text:
        missing.append("secret_key")
    for role in ROLES:
        if old_api[role] not in text:
            missing.append(f"gsc-{role} key")
    if missing:
        die("refusing to run: old key(s) not found in " + str(config_path) + ": " + ", ".join(missing)
            + ". Is it already rotated, or do you need the --old-* overrides?")

    db_path = resolve_db_path(args, config)
    if not db_path.is_file():
        die(f"SQLite DB not found: {db_path}")

    old_f = make_fernet(old_secret)
    # Legacy source keys (tokens encrypted under a previous secret_key).
    legacy_fs: list[tuple[str, Fernet]] = []
    for legacy_key in args.legacy_secret_key:
        if legacy_key and legacy_key != old_secret:
            legacy_fs.append((legacy_key, make_fernet(legacy_key)))

    new_secret = Fernet.generate_key().decode()
    new_f = make_fernet(new_secret)
    new_api = {role: new_api_key(role) for role in ROLES}

    # ---- scan the DB for fernet: tokens (every table/column) ----
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=5000")
    plan: list[tuple[str, str, int, str]] = []  # (table, column, rowid, row value)
    total_tokens = 0
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table.startswith("sqlite_"):
            continue
        for (column,) in [(r[1],) for r in con.execute(f"PRAGMA table_info({table})")]:
            for rowid, value in con.execute(
                f"SELECT rowid, {column} FROM {table} "
                f"WHERE typeof({column})='text' AND instr({column},'fernet:')>0"
            ):
                plan.append((table, column, rowid, value))
                total_tokens += len(FERNET_TOKEN_RE.findall(value))
    con.close()

    # ---- dry run: report only ----
    if args.dry_run:
        print("DRY RUN — nothing will be changed.")
        print(f"config: {config_path}")
        print(f"db:     {db_path}")
        if legacy_fs:
            print("legacy source keys (for pre-rotation tokens): "
                  + ", ".join(lbl[:8] + "..." for lbl, _ in legacy_fs))
        print(f"fernet: tokens to re-encrypt: {total_tokens} across {len(plan)} column value(s):")
        for table, column, rowid, value in plan:
            print(f"  {table}[rowid={rowid}].{column}: {len(FERNET_TOKEN_RE.findall(value))}")
        print("config.yaml would change:")
        print(f"  secret_key: {old_secret[:8]}... -> <new {len(new_secret)}-char Fernet key>")
        for role in ROLES:
            print(f"  gsc-{role} key: {old_api[role]} -> <new {len(new_api[role])}-char key>")
        if total_tokens == 0:
            print("  (no fernet: values in DB — only config.yaml keys would rotate)")
        return

    # ---- verify every token decrypts (old key first, then legacy keys) ----
    # Fail fast, no writes. A token decryptable by no provided key aborts.
    decrypted: dict[str, str] = {}
    for token in FERNET_TOKEN_RE.findall("\n".join(v for *_, v in plan)):
        body = token[len("fernet:"):].encode()
        try:
            decrypted[token] = old_f.decrypt(body).decode()
            continue
        except Exception:  # noqa: BLE001
            pass
        matched = False
        for label, legacy_f in legacy_fs:
            try:
                decrypted[token] = legacy_f.decrypt(body).decode()
                print(f"  [legacy] token under {label[:8]}... re-encrypted under new key")
                matched = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not matched:
            die(
                "a fernet: token in the DB decrypts under neither the current "
                "secret_key nor any --legacy-secret-key; aborting, nothing written"
            )

    # ---- re-encrypt + write DB ----
    con = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    reencrypted = 0
    updates = 0
    try:
        with con:
            for table, column, rowid, value in plan:
                new_value = FERNET_TOKEN_RE.sub(
                    lambda m: "fernet:" + new_f.encrypt(
                        decrypted[m.group(0)].encode()
                    ).decode(),
                    value,
                )
                if new_value != value:
                    con.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (new_value, rowid))
                    updates += 1
                    reencrypted += len(FERNET_TOKEN_RE.findall(value))
    finally:
        con.close()

    if reencrypted != total_tokens:
        die(f"internal error: re-encrypted {reencrypted} of {total_tokens} tokens")

    # ---- rewrite config.yaml in place (exact string replacement) ----
    new_text = text.replace(old_secret, new_secret, 1)
    for role in ROLES:
        old_key, new_key = old_api[role], new_api[role]
        if new_text.count(old_key) != 1:
            die(f"old {role} key not found exactly once in config text; aborting config rewrite")
        new_text = new_text.replace(old_key, new_key, 1)
    config_path.write_text(new_text, encoding="utf-8")

    print(f"Re-encrypted {reencrypted} fernet value(s) in {db_path} ({updates} row(s) updated).")
    print(f"Updated {config_path} (secret_key + 3 API keys).")
    print()
    print("WARNING: the new keys below are printed ONCE — store them securely now.")
    print(f"  secret_key: {new_secret}")
    for role in ROLES:
        print(f"  gsc-{role}: {new_api[role]}")
    print("Old keys are revoked. Restart the console services to pick up the new config.")


if __name__ == "__main__":
    main()
