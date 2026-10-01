"""Git-backed Apps: repo validation, webhook HMAC, clone, detect, apply."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.models import App, Environment
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.overlays import git_safe_argv

log = logging.getLogger(__name__)

GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}(?:\.git)?/?$"
)
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
WEBHOOK_ID_RE = re.compile(r"^apk_[A-Za-z0-9_-]{8,80}$")
FORBIDDEN_NS = frozenset({"kube-system", "openstack", "kube-public", "kube-node-lease"})
K8S_BUILDS = frozenset({"auto", "manifests", "kustomize", "helm", "dockerfile"})
OS_BUILDS = frozenset({"auto", "heat", "terraform", "ansible"})
MANIFEST_DIRS = ("deploy", "k8s", "manifests", "kubernetes")
CLONE_TIMEOUT = 120
APPLY_TIMEOUT = 180

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ALLOW_PROTOCOL": "https",
}


class AppError(ValueError):
    """Invalid app field or deploy input."""


def validate_name(name: str) -> str:
    text = str(name or "").strip().lower()
    if not NAME_RE.match(text):
        raise AppError("name must be a lowercase DNS label (e.g. my-app)")
    return text


def validate_repo_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if text.endswith(".git"):
        pass
    if not GITHUB_REPO_RE.match(text):
        raise AppError("repo_url must be https://github.com/<owner>/<repo>")
    if not text.endswith(".git"):
        text = text + ".git"
    return text


def validate_branch(branch: str) -> str:
    text = str(branch or "").strip()
    if (
        not text
        or text.startswith("-")
        or ".." in text.split("/")
        or not BRANCH_RE.match(text)
    ):
        raise AppError("invalid branch")
    return text


def validate_root_path(path: str | None) -> str | None:
    if path is None or str(path).strip() in ("", "."):
        return None
    text = str(path).strip().lstrip("/")
    if ".." in text.split("/") or text.startswith("-"):
        raise AppError("invalid root_path")
    return text


def validate_target_build(target: str, build: str) -> tuple[str, str]:
    target = str(target or "").strip().lower()
    build = str(build or "auto").strip().lower() or "auto"
    if target not in ("kubernetes", "openstack"):
        raise AppError("target must be kubernetes or openstack")
    allowed = K8S_BUILDS if target == "kubernetes" else OS_BUILDS
    if build not in allowed:
        raise AppError(f"build {build!r} is not valid for target {target}")
    return target, build


def validate_namespace(name: str | None, app_name: str) -> str:
    text = str(name or "").strip() or f"app-{app_name}"
    if not NAME_RE.match(text) or text in FORBIDDEN_NS:
        raise AppError("invalid namespace")
    return text


def mint_webhook() -> tuple[str, str]:
    webhook_id = "apk_" + secrets.token_urlsafe(24)
    secret = secrets.token_urlsafe(32)
    return webhook_id, secret


def webhook_url(base: str, webhook_id: str) -> str:
    root = str(base or "").rstrip("/")
    return f"{root}/api/v1/hooks/apps/{webhook_id}"


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    token = str(header).strip()
    if not token.lower().startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    return hmac.compare_digest(expected, token)


def detect_kubernetes(root: Path) -> str | None:
    if (root / "Chart.yaml").is_file():
        return "helm"
    if (root / "kustomization.yaml").is_file() or (
        root / "kustomization.yml"
    ).is_file():
        return "kustomize"
    for dirname in MANIFEST_DIRS:
        folder = root / dirname
        if folder.is_dir() and any(folder.glob("*.y*ml")):
            return "manifests"
    yamls = [p for p in root.glob("*.y*ml") if p.is_file()]
    if yamls:
        return "manifests"
    if (root / "Dockerfile").is_file():
        return "dockerfile"
    return None


def detect_openstack(root: Path) -> str | None:
    if list(root.glob("*.tf")) or (root / "terraform").is_dir():
        return "terraform"
    if (
        (root / "playbook.yml").is_file()
        or (root / "playbook.yaml").is_file()
        or (root / "site.yml").is_file()
        or (root / "site.yaml").is_file()
    ):
        return "ansible"
    if (
        (root / "hot").is_dir()
        or list(root.glob("*.heat.y*ml"))
        or (root / "heat.yaml").is_file()
    ):
        return "heat"
    return None


def detect_build(target: str, root: Path, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    found = (
        detect_kubernetes(root) if target == "kubernetes" else detect_openstack(root)
    )
    if not found:
        raise AppError(
            "could not detect how to deploy this repository "
            "(add k8s manifests / Chart.yaml, or heat/terraform/ansible)"
        )
    return found


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(_GIT_ENV)
    return env


def _run_git(
    args: list[str], cwd: Path | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    argv = git_safe_argv(args)
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_git_env(),
    )


def clone_repo(
    repo_url: str,
    branch: str,
    dest: Path,
    token: str | None = None,
) -> str:
    """Shallow-clone ``repo_url`` at ``branch``. Returns HEAD sha."""
    extra: list[str] = []
    if token:
        import base64

        basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode(
            "ascii"
        )
        extra = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    argv = [
        "git",
        *extra,
        "clone",
        "--depth",
        "1",
        "--branch",
        branch,
        "--",
        repo_url,
        str(dest),
    ]
    try:
        proc = _run_git(argv, timeout=CLONE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise AppError("git clone timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git clone failed").strip().splitlines()
        raise AppError(err[-1][:200] if err else "git clone failed")
    head = _run_git(["git", "rev-parse", "HEAD"], cwd=dest)
    if head.returncode != 0:
        raise AppError("git rev-parse failed")
    return (head.stdout or "").strip()


def _kubectl_env(kubeconfig: str) -> dict[str, str]:
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    return env


def _run(
    argv: list[str], *, cwd: Path | None, env: dict[str, str] | None, timeout: int
) -> tuple[int, str]:
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)[:200]
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, out[-4000:]


def apply_kubernetes(
    *,
    kubeconfig: str,
    root: Path,
    build: str,
    namespace: str,
    name: str,
    log_fn: Any = None,
) -> dict[str, Any]:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return {"ok": False, "error": "kubectl not found"}
    env = _kubectl_env(kubeconfig)
    subprocess.run(
        [kubectl, "create", "namespace", namespace],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if log_fn:
        log_fn(f"[app] kubernetes namespace {namespace}")

    if build == "dockerfile":
        return {
            "ok": False,
            "error": "Dockerfile detected; image build is not in this console version — add k8s manifests that reference a prebuilt image",
        }
    if build == "helm":
        helm = shutil.which("helm")
        if not helm:
            return {"ok": False, "error": "helm not found"}
        argv = [
            helm,
            "upgrade",
            "--install",
            name,
            str(root),
            "--namespace",
            namespace,
            "--create-namespace",
            "--wait=false",
        ]
        code, out = _run(argv, cwd=root, env=env, timeout=APPLY_TIMEOUT)
        return {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "helm",
            "output": out,
        }
    if build == "kustomize":
        argv = [kubectl, "apply", "-k", str(root), "-n", namespace]
        code, out = _run(argv, cwd=root, env=env, timeout=APPLY_TIMEOUT)
        return {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "kustomize",
            "output": out,
        }

    targets: list[str] = []
    for dirname in MANIFEST_DIRS:
        folder = root / dirname
        if folder.is_dir():
            targets.append(str(folder))
    if not targets:
        targets = [str(root)]
    last = {"ok": False, "error": "no manifests", "method": "manifests"}
    for target in targets:
        argv = [kubectl, "apply", "-f", target, "-n", namespace]
        code, out = _run(argv, cwd=root, env=env, timeout=APPLY_TIMEOUT)
        last = {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "manifests",
            "output": out,
        }
        if code != 0:
            return last
    return last


def apply_openstack(
    *,
    root: Path,
    build: str,
    stack_name: str,
    log_fn: Any = None,
) -> dict[str, Any]:
    if build == "terraform":
        tf = shutil.which("terraform")
        if not tf:
            return {"ok": False, "error": "terraform not found on the console host"}
        init_code, init_out = _run(
            [tf, "init", "-input=false"],
            cwd=root,
            env=os.environ.copy(),
            timeout=APPLY_TIMEOUT,
        )
        if init_code != 0:
            return {"ok": False, "error": init_out, "method": "terraform"}
        code, out = _run(
            [tf, "apply", "-auto-approve", "-input=false"],
            cwd=root,
            env=os.environ.copy(),
            timeout=APPLY_TIMEOUT,
        )
        return {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "terraform",
            "output": out,
        }
    if build == "ansible":
        play = None
        for name in ("site.yml", "site.yaml", "playbook.yml", "playbook.yaml"):
            if (root / name).is_file():
                play = root / name
                break
        if play is None:
            return {"ok": False, "error": "no site.yml / playbook.yml in repo"}
        ansible = shutil.which("ansible-playbook")
        if not ansible:
            return {"ok": False, "error": "ansible-playbook not found"}
        code, out = _run(
            [ansible, str(play)], cwd=root, env=os.environ.copy(), timeout=APPLY_TIMEOUT
        )
        return {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "ansible",
            "output": out,
        }
    if build == "heat":
        heat = None
        for name in ("heat.yaml", "heat.yml", "stack.yaml", "stack.yml"):
            if (root / name).is_file():
                heat = root / name
                break
        if heat is None:
            yamls = sorted(root.glob("*.heat.y*ml"))
            heat = yamls[0] if yamls else None
        if heat is None:
            return {"ok": False, "error": "no Heat template found"}
        osc = shutil.which("openstack")
        if not osc:
            return {
                "ok": False,
                "error": "openstack CLI not on the console host — ship a Heat template plus credentials, or use the Cloud tab",
            }
        code, out = _run(
            [osc, "stack", "create", "-t", str(heat), stack_name, "--wait"],
            cwd=root,
            env=os.environ.copy(),
            timeout=APPLY_TIMEOUT,
        )
        if code != 0 and "AlreadyExists" in (out or ""):
            code, out = _run(
                [osc, "stack", "update", "-t", str(heat), stack_name, "--wait"],
                cwd=root,
                env=os.environ.copy(),
                timeout=APPLY_TIMEOUT,
            )
        return {
            "ok": code == 0,
            "error": None if code == 0 else out,
            "method": "heat",
            "output": out,
        }
    return {"ok": False, "error": f"unsupported openstack build {build}"}


def deploy_app(
    app: App,
    env: Environment,
    *,
    kubeconfig: str | None,
    force: bool = False,
    log_fn: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Clone and apply. Never raises for git/apply failures — returns ok/error."""
    token = decrypt_secret(app.deploy_token_encrypted)
    tmp = Path(tempfile.mkdtemp(prefix="gsc-app-"))
    try:
        if log_fn:
            log_fn(f"[app] clone {app.repo_url} branch={app.branch}")
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "message": f"would clone {app.repo_url}@{app.branch} and deploy as {app.target}",
                "sha": app.last_sha,
            }
        sha = clone_repo(app.repo_url, app.branch, tmp, token=token)
        if app.last_sha and sha == app.last_sha and not force:
            if log_fn:
                log_fn(f"[app] sha {sha[:12]} unchanged — skip")
            return {
                "ok": True,
                "skipped": True,
                "sha": sha,
                "message": "already deployed",
            }
        root = tmp
        if app.root_path:
            root = tmp / app.root_path
            if not root.is_dir():
                return {
                    "ok": False,
                    "error": f"root_path {app.root_path!r} missing in repo",
                    "sha": sha,
                }
        build = detect_build(app.target, root, app.build)
        if log_fn:
            log_fn(f"[app] detected build={build} target={app.target} sha={sha[:12]}")
        if app.target == "kubernetes":
            if not kubeconfig:
                return {
                    "ok": False,
                    "error": "environment has no kubeconfig",
                    "sha": sha,
                }
            ns = validate_namespace(app.namespace, app.name)
            result = apply_kubernetes(
                kubeconfig=kubeconfig,
                root=root,
                build=build,
                namespace=ns,
                name=app.name,
                log_fn=log_fn,
            )
        else:
            stack = app.stack_name or app.name
            result = apply_openstack(
                root=root, build=build, stack_name=stack, log_fn=log_fn
            )
        result["sha"] = sha
        result["build"] = build
        return result
    except AppError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.info("app deploy failed: %s", type(exc).__name__)
        return {"ok": False, "error": "deploy failed"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def to_read(app: App, base_url: str) -> dict[str, Any]:
    url = webhook_url(base_url, app.webhook_id)
    return {
        "id": app.id,
        "environment_id": app.environment_id,
        "name": app.name,
        "repo_url": app.repo_url,
        "branch": app.branch,
        "root_path": app.root_path,
        "target": app.target,
        "build": app.build,
        "namespace": app.namespace,
        "stack_name": app.stack_name,
        "webhook_id": app.webhook_id,
        "webhook_url": url,
        "has_deploy_token": bool(app.deploy_token_encrypted),
        "last_sha": app.last_sha,
        "last_job_id": app.last_job_id,
        "last_status": app.last_status,
        "last_error": app.last_error,
        "poll_seconds": app.poll_seconds,
        "created_by": app.created_by,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
    }


def encrypt_token(token: str | None) -> str | None:
    text = str(token or "").strip()
    if not text:
        return None
    return encrypt_secret(text)


def encrypt_webhook_secret(secret: str) -> str:
    return encrypt_secret(secret) or secret
