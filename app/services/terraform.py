"""Terraform bare-metal provision for Hardware accounts (AWS, Azure, GCP, Rackspace).

Credentials stay fernet-encrypted on ``HardwareAccount``. Plan/apply jobs
decrypt them only in the worker, write a temp tfvars/env that is deleted
after the run, and never log secret values. Apply upserts resulting hosts
into the env config ``servers`` section (source ``terraform``) — the same
inventory PXE/OVH/static hosts already use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Environment, HardwareAccount, TerraformState
from app.paths import package_root
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services import envconfig as envconfig_service
from app.services.logredact import redact_secret_line

LogFn = Callable[[str], None]
STATE_FILENAME = "terraform.tfstate"

KINDS = frozenset({"rackspace", "aws", "azure", "gcp"})
MAX_NODE_COUNT = 32
# TEST-NET-1 (RFC 5737) — dry-run placeholders only, never a lab network.
_KIND_OCTET = {"aws": 10, "azure": 20, "gcp": 30, "rackspace": 40}
_HOSTNAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# Credential keys written into tfvars / provider env. Anything else on the
# account JSON is ignored so leftover fields cannot leak onto disk.
_KIND_CRED_KEYS: dict[str, tuple[str, ...]] = {
    "aws": ("access_key", "secret_key"),
    "azure": ("tenant_id", "client_id", "client_secret", "subscription_id"),
    "gcp": ("project_id", "service_account_json"),
    "rackspace": ("username", "api_key", "auth_url", "tenant_name"),
}


def terraform_bin() -> str | None:
    return shutil.which("terraform")


def roots_dir() -> Path:
    return package_root() / "terraform"


def root_for(kind: str) -> Path:
    return roots_dir() / kind


def _as_count(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, MAX_NODE_COUNT))


def _as_roles(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [p.strip().lower() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, list):
        items = [str(p).strip().lower() for p in raw if str(p).strip()]
    else:
        items = []
    return items or ["compute"]


def _name_prefix(env: Environment, kind: str) -> str:
    base = _HOSTNAME_SAFE.sub("-", (env.name or "gs").strip()).strip("-") or "gs"
    return f"tf-{kind}-{base[:16].lower()}"


def fake_hosts(kind: str, count: int, role: str) -> list[dict[str, str]]:
    """Placeholder hosts when terraform is not installed (dry-run apply only)."""
    base = _KIND_OCTET.get(kind, 50)
    out: list[dict[str, str]] = []
    for i in range(1, count + 1):
        out.append(
            {
                "hostname": f"tf-{kind}-{i}",
                "ip": f"192.0.2.{base + i}",
                "role": role,
            }
        )
    return out


def _decrypt_credentials(row: HardwareAccount) -> dict[str, str]:
    raw = decrypt_secret(row.credentials_encrypted) or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = _KIND_CRED_KEYS.get(row.kind, ())
    return {k: str(v) for k, v in data.items() if k in allowed and str(v).strip()}


def _provider_env(kind: str, creds: dict[str, str], region: str) -> dict[str, str]:
    env = dict(os.environ)
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    if kind == "aws":
        if creds.get("access_key"):
            env["AWS_ACCESS_KEY_ID"] = creds["access_key"]
        if creds.get("secret_key"):
            env["AWS_SECRET_ACCESS_KEY"] = creds["secret_key"]
        if region:
            env["AWS_DEFAULT_REGION"] = region
    elif kind == "azure":
        for src, dst in (
            ("tenant_id", "ARM_TENANT_ID"),
            ("client_id", "ARM_CLIENT_ID"),
            ("client_secret", "ARM_CLIENT_SECRET"),
            ("subscription_id", "ARM_SUBSCRIPTION_ID"),
        ):
            if creds.get(src):
                env[dst] = creds[src]
    elif kind == "gcp":
        if creds.get("project_id"):
            env["GOOGLE_PROJECT"] = creds["project_id"]
        if creds.get("service_account_json"):
            env["GOOGLE_CREDENTIALS"] = creds["service_account_json"]
        if region:
            env["GOOGLE_REGION"] = region
    elif kind == "rackspace":
        if creds.get("username"):
            env["OS_USERNAME"] = creds["username"]
        if creds.get("api_key"):
            env["OS_PASSWORD"] = creds["api_key"]
        if region:
            env["OS_REGION_NAME"] = region
        if creds.get("auth_url"):
            env["OS_AUTH_URL"] = creds["auth_url"]
        if creds.get("tenant_name"):
            env["OS_TENANT_NAME"] = creds["tenant_name"]
    return env


def _tfvars(
    kind: str,
    creds: dict[str, str],
    *,
    region: str,
    count: int,
    flavor: str,
    prefix: str,
    role: str,
) -> dict[str, Any]:
    vars_: dict[str, Any] = {
        "node_count": count,
        "name_prefix": prefix,
        "role": role,
    }
    if region:
        vars_["region"] = region
    if flavor:
        vars_["flavor"] = flavor
    vars_.update(creds)
    if kind == "gcp" and region and "zone" not in vars_:
        vars_["zone"] = f"{region}-a"
    return vars_


def _parse_hosts(output_json: str, default_role: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(output_json or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("hosts")
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, list):
        return []
    hosts: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hostname = str(item.get("hostname") or "").strip()
        ip = str(item.get("ip") or "").strip()
        role = str(item.get("role") or default_role).strip() or default_role
        if hostname:
            hosts.append({"hostname": hostname, "ip": ip, "role": role})
    return hosts


def _scrub_terraform_output(output: str) -> str:
    """Scrub sensitive values from terraform stdout/stderr before logging.

    Applies line-by-line redaction to prevent secrets from leaking in job logs.
    Terraform may echo provider credentials, access keys, or other sensitive
    values in plan/apply output.
    """
    if not output:
        return output
    return "\n".join(redact_secret_line(line) for line in output.splitlines())


def _run_tf(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    log: LogFn | None,
) -> tuple[int, str]:
    display = " ".join(
        "<tfvars>" if part.endswith(".tfvars.json") else part for part in argv
    )
    if log:
        log(f"$ {display}")
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 1, f"terraform timed out after {timeout}s"
    except OSError as exc:
        return 1, str(exc)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    scrubbed = _scrub_terraform_output(out)
    return proc.returncode, scrubbed


def _copy_root(kind: str, dest: Path) -> None:
    src = root_for(kind)
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.glob("*.tf"):
        shutil.copy2(path, dest / path.name)


def _state_meta(plain: str) -> tuple[int | None, str | None]:
    try:
        data = json.loads(plain)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    serial = data.get("serial")
    lineage = data.get("lineage")
    try:
        serial_i = int(serial) if serial is not None else None
    except (TypeError, ValueError):
        serial_i = None
    lineage_s = str(lineage).strip() if lineage else None
    return serial_i, (lineage_s or None)


def restore_state_file(
    db: Session,
    *,
    environment_id: str,
    account_id: str,
    work_dir: Path,
    log: LogFn | None = None,
) -> bool:
    """Write the last SQLite snapshot to disk only when the work-dir file is missing."""
    path = work_dir / STATE_FILENAME
    if path.is_file() and path.stat().st_size > 0:
        return False
    row = db.scalar(
        select(TerraformState).where(
            TerraformState.environment_id == environment_id,
            TerraformState.account_id == account_id,
        )
    )
    if row is None:
        return False
    plain = decrypt_secret(row.state_encrypted) or ""
    if not plain.strip():
        return False
    work_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(plain, encoding="utf-8")
    os.chmod(path, 0o600)
    if log:
        log(f"[terraform] restored state from database (serial={row.serial})")
    return True


def snapshot_state_file(
    db: Session,
    *,
    environment_id: str,
    account_id: str,
    work_dir: Path,
    log: LogFn | None = None,
) -> bool:
    """Copy terraform.tfstate from disk into SQLite (encrypted). Disk stays the working copy."""
    path = work_dir / STATE_FILENAME
    if not path.is_file() or path.stat().st_size == 0:
        return False
    plain = path.read_text(encoding="utf-8")
    serial, lineage = _state_meta(plain)
    enc = encrypt_secret(plain) or ""
    row = db.scalar(
        select(TerraformState).where(
            TerraformState.environment_id == environment_id,
            TerraformState.account_id == account_id,
        )
    )
    if row is None:
        row = TerraformState(
            environment_id=environment_id,
            account_id=account_id,
            state_encrypted=enc,
            serial=serial,
            lineage=lineage,
        )
        db.add(row)
    else:
        row.state_encrypted = enc
        row.serial = serial
        row.lineage = lineage
    db.flush()
    if log:
        log(f"[terraform] stored state snapshot in database (serial={serial})")
    return True


def _upsert_hosts(
    db: Session,
    env: Environment,
    actor: str | None,
    hosts: list[dict[str, str]],
    roles: list[str],
    log: LogFn | None,
) -> list[str]:
    imported: list[str] = []
    for host in hosts:
        hostname = host.get("hostname") or ""
        if not hostname:
            continue
        envconfig_service.upsert_static_server(
            db,
            env,
            actor,
            hostname=hostname,
            ip=host.get("ip") or None,
            roles=list(roles),
            source="terraform",
        )
        imported.append(hostname)
        if log:
            log(
                f"[terraform] upserted servers.{hostname} (source=terraform, ip={host.get('ip') or '-'})"
            )
    return imported


def run_terraform_job(
    db: Session,
    env: Environment | None,
    *,
    action: Literal["plan", "apply"],
    params: dict[str, Any],
    dry_run: bool,
    log: LogFn | None = None,
    timeout: int = 1800,
    actor: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Plan or apply a HardwareAccount terraform root.

    Dry-run, or a missing terraform binary, never shells out. Fake hosts are
    imported on apply only when dry-run *and* terraform is not installed.
    Live apply without the binary fails (no placeholder inventory).
    """
    op_id = f"hardware.terraform.{action}"
    if env is None:
        return {
            "ok": False,
            "error": f"{op_id} requires an environment",
            "returncode": 2,
        }
    account_id = str(params.get("account_id") or "").strip()
    if not account_id:
        return {
            "ok": False,
            "error": f"{op_id}: account_id is required",
            "returncode": 2,
        }
    row = db.get(HardwareAccount, account_id)
    if row is None:
        return {
            "ok": False,
            "error": f"{op_id}: hardware account not found",
            "returncode": 2,
        }
    if row.kind not in KINDS:
        return {
            "ok": False,
            "error": f"{op_id}: unsupported account kind {row.kind!r}",
            "returncode": 2,
        }
    count = _as_count(params.get("count") if params.get("count") is not None else 1)
    roles = _as_roles(params.get("roles"))
    invalid = [r for r in roles if r not in envconfig_service.VALID_SERVER_ROLES]
    if invalid:
        valid = ", ".join(sorted(envconfig_service.VALID_SERVER_ROLES))
        return {
            "ok": False,
            "error": f"{op_id}: unknown role(s) {', '.join(invalid)} (valid: {valid})",
            "returncode": 2,
        }
    role = roles[0]
    region = str(params.get("region") or row.region or "").strip()
    flavor = str(params.get("flavor") or "").strip()
    prefix = _name_prefix(env, row.kind)
    tf = terraform_bin()
    if log:
        log(
            f"[terraform] {action} kind={row.kind} account={row.name} "
            f"count={count} region={region or '-'} flavor={flavor or '(default)'} "
            f"dry_run={dry_run} terraform={'yes' if tf else 'no'}"
        )

    if dry_run or not tf:
        if action == "apply" and not dry_run:
            return {
                "ok": False,
                "error": "terraform is not installed on the console host",
                "returncode": 2,
                "dry_run": False,
            }
        hosts: list[dict[str, str]] = []
        imported: list[str] = []
        if action == "apply" and dry_run and not tf:
            hosts = fake_hosts(row.kind, count, role)
            imported = _upsert_hosts(db, env, actor, hosts, roles, log)
        message = (
            f"[dry-run] would terraform {action} {count} {row.kind} node(s) "
            f"for account '{row.name}'"
        )
        if log:
            log(message)
            if hosts:
                log(f"[terraform] dry-run hosts={[h['hostname'] for h in hosts]}")
        return {
            "ok": True,
            "dry_run": True,
            "action": action,
            "kind": row.kind,
            "account_id": row.id,
            "count": count,
            "hosts": hosts,
            "imported": imported,
            "message": message,
        }

    root = root_for(row.kind)
    if not root.is_dir() or not any(root.glob("*.tf")):
        return {
            "ok": False,
            "error": f"{op_id}: terraform root missing for {row.kind}",
            "returncode": 2,
        }
    creds = _decrypt_credentials(row)
    if not creds:
        return {
            "ok": False,
            "error": f"{op_id}: account has no usable credentials",
            "returncode": 2,
        }

    settings = settings or get_settings()
    work_dir = Path(settings.data_dir) / "terraform" / env.id / row.id
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(work_dir, 0o700)
    _copy_root(row.kind, work_dir)
    restore_state_file(
        db,
        environment_id=env.id,
        account_id=row.id,
        work_dir=work_dir,
        log=log,
    )
    tfvars = _tfvars(
        row.kind,
        creds,
        region=region,
        count=count,
        flavor=flavor,
        prefix=prefix,
        role=role,
    )
    tf_env = _provider_env(row.kind, creds, region)

    with tempfile.TemporaryDirectory(prefix="gsc-tfvars-") as tmp:
        tfvars_path = Path(tmp) / "secret.tfvars.json"
        tfvars_path.write_text(json.dumps(tfvars), encoding="utf-8")
        os.chmod(tfvars_path, 0o600)
        var_file = str(tfvars_path)
        init_rc, init_out = _run_tf(
            [tf, "init", "-input=false", "-no-color"],
            cwd=work_dir,
            env=tf_env,
            timeout=min(timeout, 300),
            log=log,
        )
        if init_rc != 0:
            if log and init_out:
                log(init_out[-4000:])
            return {
                "ok": False,
                "error": "terraform init failed",
                "returncode": init_rc,
            }
        if action == "plan":
            rc, out = _run_tf(
                [tf, "plan", "-input=false", "-no-color", f"-var-file={var_file}"],
                cwd=work_dir,
                env=tf_env,
                timeout=timeout,
                log=log,
            )
            if log and out:
                log(out[-4000:])
            snapshot_state_file(
                db,
                environment_id=env.id,
                account_id=row.id,
                work_dir=work_dir,
                log=log,
            )
            return {
                "ok": rc == 0,
                "dry_run": False,
                "action": "plan",
                "kind": row.kind,
                "account_id": row.id,
                "count": count,
                "returncode": rc,
                "message": (
                    "terraform plan completed" if rc == 0 else "terraform plan failed"
                ),
                "error": None if rc == 0 else "terraform plan failed",
            }
        rc, out = _run_tf(
            [
                tf,
                "apply",
                "-input=false",
                "-no-color",
                "-auto-approve",
                f"-var-file={var_file}",
            ],
            cwd=work_dir,
            env=tf_env,
            timeout=timeout,
            log=log,
        )
        if log and out:
            log(out[-4000:])
        snapshot_state_file(
            db,
            environment_id=env.id,
            account_id=row.id,
            work_dir=work_dir,
            log=log,
        )
        if rc != 0:
            return {
                "ok": False,
                "dry_run": False,
                "action": "apply",
                "kind": row.kind,
                "account_id": row.id,
                "count": count,
                "returncode": rc,
                "error": "terraform apply failed",
            }
        out_rc, out_json = _run_tf(
            [tf, "output", "-json", "-no-color"],
            cwd=work_dir,
            env=tf_env,
            timeout=min(timeout, 60),
            log=log,
        )
        hosts = _parse_hosts(out_json if out_rc == 0 else "", role)
        imported = _upsert_hosts(db, env, actor, hosts, roles, log)
        return {
            "ok": True,
            "dry_run": False,
            "action": "apply",
            "kind": row.kind,
            "account_id": row.id,
            "count": len(imported) or count,
            "hosts": hosts,
            "imported": imported,
            "message": f"imported {len(imported)} host(s) from terraform apply",
        }
