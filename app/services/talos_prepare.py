"""Make a Talos cluster ready for Longhorn without a one-off node tweak.

Longhorn's pre-upgrade check runs ``iscsiadm`` on the host. The factory
schematic names those extensions, and writing ``install.image`` into the
machine config does not install them. ``talosctl upgrade`` does. That image
also renames ``eth0`` to a predictable name, so a kube-ovn DaemonSet that
still says ``--iface=eth0`` crash-loops the CNI on the upgraded node.

This step runs after kube-ovn's helm install and before Longhorn:

* Drop ``ovn.kubernetes.io/tunnel_interface`` only when the DaemonSet iface
  is empty. An empty iface selects the NIC that owns the node address, which
  works for both ``eth0`` and ``ens160``. Deleting the annotation while
  ``--iface=eth0`` is still set crash-loops every renamed node.
* Upgrade a control plane or storage node whose ``ext-iscsid`` is not
  Running. Control planes are first, and the next node waits until
  ``ext-iscsid`` is Running. A node that already has the service is left
  alone. A timeout from ``talosctl upgrade`` is not success and not proof
  the node stayed on the old image; the service check decides.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.platform import UPGRADE_TIMEOUT
from app.services.talos import (
    image_has_longhorn_extensions,
    install_image_from_doc,
    node_cluster_ip,
    talosctl_command,
)

LogFn = Callable[[str], None]
RunFn = Callable[[list[str], int], subprocess.CompletedProcess[str]]

TUNNEL_ANNOTATION = "ovn.kubernetes.io/tunnel_interface"
ISCSI_SERVICE = "ext-iscsid"
# Matches platform.UPGRADE_TIMEOUT. A cold image pull exceeds 60s.
UPGRADE_REQUEST_TIMEOUT = UPGRADE_TIMEOUT
ISCSI_WAIT_S = 600
SERVICE_POLL_S = 10
SERVICE_TIMEOUT = 20

_HEADER_TOKENS = frozenset({"node", "id", "service"})


def parse_services(text: str) -> list[dict[str, str]]:
    """Parse ``talosctl services`` rows. A leading node address is ignored."""
    rows: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or parts[0].lower() in _HEADER_TOKENS:
            continue
        if "." in parts[0] or ":" in parts[0]:
            parts = parts[1:]
        if len(parts) < 3:
            continue
        rows.append({"id": parts[0], "state": parts[1], "health": parts[2]})
    return rows


def iscsi_running(services: list[dict[str, str]]) -> bool:
    return any(
        row.get("id") == ISCSI_SERVICE and row.get("state") == "Running"
        for row in services
    )


def iface_from_container_args(args: list[str]) -> str | None:
    """Value of kube-ovn ``--iface``. None when the flag is absent.

    ``--iface=`` and a bare ``--iface`` are the empty value. That is the
    chart default: each node uses the NIC that owns its address.
    """
    for index, arg in enumerate(args):
        if arg == "--iface":
            if index + 1 >= len(args) or str(args[index + 1]).startswith("--"):
                return ""
            return str(args[index + 1])
        if arg.startswith("--iface="):
            return arg.split("=", 1)[1]
    return None


def daemonset_iface(manifest: dict[str, Any]) -> str | None:
    """Iface the kube-ovn CNI will use. A non-empty pin wins over an empty one."""
    try:
        containers = manifest["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        return None
    if not isinstance(containers, list):
        return None
    saw_empty = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        args = container.get("args")
        if not isinstance(args, list):
            continue
        iface = iface_from_container_args([str(item) for item in args])
        if iface is None:
            continue
        if iface != "":
            return iface
        saw_empty = True
    if saw_empty:
        return ""
    return None


def should_clear_tunnel_annotation(iface: str | None) -> bool:
    """True only when kube-ovn will auto-detect the NIC."""
    return iface == ""


def nodes_with_tunnel_annotation(manifest: dict[str, Any]) -> list[str]:
    items = manifest.get("items")
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            continue
        annotations = meta.get("annotations")
        if not isinstance(annotations, dict):
            continue
        value = str(annotations.get(TUNNEL_ANNOTATION) or "").strip()
        name = str(meta.get("name") or "").strip()
        if value and name:
            names.append(name)
    return names


def _role_tokens(roles: Any) -> set[str]:
    if isinstance(roles, str):
        parts = roles.replace(",", " ").split()
    elif isinstance(roles, (list, tuple, set)):
        parts = [str(role) for role in roles]
    else:
        return set()
    return {part.strip().lower() for part in parts if part.strip()}


def longhorn_targets(doc: dict[str, Any]) -> list[dict[str, str]]:
    """Control planes first, then Ceph storage nodes. Workers are omitted.

    Longhorn's manager runs on control planes and storage nodes. A worker
    does not need ``iscsiadm`` for that check.
    """
    servers = doc.get("servers")
    if not isinstance(servers, dict):
        return []
    control: list[dict[str, str]] = []
    storage: list[dict[str, str]] = []
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        roles = _role_tokens(entry.get("roles"))
        is_control = "k8s_control_plane" in roles
        is_storage = "storage-ceph" in roles
        if not is_control and not is_storage:
            continue
        ip = node_cluster_ip(entry, str(hostname)).strip()
        name = str(hostname).strip()
        if not name or not ip or "=" in name or "=" in ip:
            continue
        row = {"name": name, "ip": ip, "kind": "control" if is_control else "storage"}
        if is_control:
            control.append(row)
        else:
            storage.append(row)
    control.sort(key=lambda row: row["name"])
    storage.sort(key=lambda row: row["name"])
    return control + storage


def _prepare_launch() -> list[str]:
    """Re-enter this step from the infrastructure script.

    The published console is one binary. A source checkout uses the same
    interpreter that is already running the worker.
    """
    exe = Path(sys.executable)
    if exe.name.startswith("genestack-console"):
        return [str(exe), "talos-prepare"]
    return [sys.executable, "-m", "app.services.talos_prepare"]


def render_prepare_script(config_dir: Path, doc: dict[str, Any]) -> str | None:
    """Bash that runs this step. None when the image has no Longhorn schematic."""
    image = install_image_from_doc(doc if isinstance(doc, dict) else {})
    if not image_has_longhorn_extensions(image):
        return None
    targets = longhorn_targets(doc if isinstance(doc, dict) else {})
    argv = [
        *_prepare_launch(),
        "--talosconfig",
        str(config_dir / "talos" / "talosconfig"),
        "--image",
        image,
    ]
    for target in targets:
        argv.extend(["--node", f"{target['name']}={target['ip']}"])
    return "#!/bin/bash\nset -euo pipefail\nexec " + shlex.join(argv) + "\n"


def install_prepare_script(config_dir: Path, doc: dict[str, Any]) -> Path | None:
    text = render_prepare_script(config_dir, doc)
    if text is None:
        return None
    path = config_dir / "talos-before-longhorn.sh"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o700)
    return path


def _run_command(
    argv: list[str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _services_text(
    run: RunFn, talosctl: str, ip: str, talosconfig: str
) -> str | None:
    try:
        result = run(
            [talosctl, "services", "--nodes", ip, "--talosconfig", talosconfig],
            SERVICE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 and not (result.stdout or "").strip():
        return None
    return result.stdout or ""


def _request_upgrade(
    run: RunFn,
    log: LogFn,
    talosctl: str,
    target: dict[str, str],
    image: str,
    talosconfig: str,
) -> None:
    name = target["name"]
    argv = [
        talosctl,
        "upgrade",
        "--nodes",
        target["ip"],
        "--image",
        image,
        "--preserve",
        "--wait=false",
        "--talosconfig",
        talosconfig,
    ]
    try:
        result = run(argv, UPGRADE_REQUEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(
            f"[talos] {name} upgrade request timed out; "
            "checking whether ext-iscsid came up"
        )
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1][:160] if detail else f"exit {result.returncode}"
        log(f"[talos] {name} upgrade command returned {result.returncode}: {tail}")


def _wait_for_iscsi(
    run: RunFn,
    *,
    talosctl: str,
    ip: str,
    talosconfig: str,
    wait_s: float,
    poll_s: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    deadline = now() + wait_s
    while True:
        text = _services_text(run, talosctl, ip, talosconfig)
        if text is not None and iscsi_running(parse_services(text)):
            return True
        if now() >= deadline:
            return False
        sleep(poll_s)


def prepare_before_longhorn(
    *,
    targets: list[dict[str, str]],
    image: str,
    talosconfig: str,
    log: LogFn,
    run: RunFn | None = None,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    iscsi_wait_s: float = ISCSI_WAIT_S,
    poll_s: float = SERVICE_POLL_S,
) -> dict[str, Any]:
    """Clear a stale tunnel annotation, then install iSCSI where it is missing."""
    run = run or _run_command
    now = now or time.monotonic
    sleep = sleep or time.sleep
    cleared: list[str] = []
    upgraded: list[str] = []
    skipped: list[str] = []

    log("[talos] checking kube-ovn iface before Longhorn")
    try:
        daemonset = run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "get",
                "daemonset",
                "kube-ovn-cni",
                "-o",
                "json",
            ],
            30,
        )
    except subprocess.TimeoutExpired:
        daemonset = None
    iface: str | None = None
    if daemonset is not None and daemonset.returncode == 0:
        try:
            iface = daemonset_iface(json.loads(daemonset.stdout or "{}"))
        except json.JSONDecodeError:
            iface = None
    if should_clear_tunnel_annotation(iface):
        try:
            listed = run(["kubectl", "get", "nodes", "-o", "json"], 30)
        except subprocess.TimeoutExpired:
            listed = None
        names: list[str] = []
        if listed is not None and listed.returncode == 0:
            try:
                names = nodes_with_tunnel_annotation(json.loads(listed.stdout or "{}"))
            except json.JSONDecodeError:
                names = []
        else:
            log("[talos] could not list nodes; leaving tunnel annotations in place")
        for name in names:
            try:
                removed = run(
                    [
                        "kubectl",
                        "annotate",
                        "node",
                        name,
                        f"{TUNNEL_ANNOTATION}-",
                    ],
                    30,
                )
            except subprocess.TimeoutExpired:
                return _failed(
                    f"timed out removing the tunnel annotation from {name}",
                    cleared,
                    upgraded,
                    skipped,
                )
            if removed.returncode != 0:
                return _failed(
                    f"could not remove the tunnel annotation from {name}",
                    cleared,
                    upgraded,
                    skipped,
                )
            cleared.append(name)
            log(f"[talos] removed tunnel interface annotation from {name}")
        if cleared:
            try:
                restart = run(
                    [
                        "kubectl",
                        "-n",
                        "kube-system",
                        "rollout",
                        "restart",
                        "daemonset/kube-ovn-cni",
                    ],
                    30,
                )
            except subprocess.TimeoutExpired:
                return _failed(
                    "timed out restarting kube-ovn-cni",
                    cleared,
                    upgraded,
                    skipped,
                )
            if restart.returncode != 0:
                return _failed(
                    "kube-ovn rollout restart failed",
                    cleared,
                    upgraded,
                    skipped,
                )
            try:
                status = run(
                    [
                        "kubectl",
                        "-n",
                        "kube-system",
                        "rollout",
                        "status",
                        "daemonset/kube-ovn-cni",
                        "--timeout=600s",
                    ],
                    620,
                )
            except subprocess.TimeoutExpired:
                return _failed(
                    "kube-ovn did not become ready after dropping the tunnel annotation",
                    cleared,
                    upgraded,
                    skipped,
                )
            if status.returncode != 0:
                return _failed(
                    "kube-ovn did not become ready after dropping the tunnel annotation",
                    cleared,
                    upgraded,
                    skipped,
                )
    else:
        shown = "unreadable" if iface is None else repr(iface)
        log(
            f"[talos] kube-ovn iface is {shown}; "
            "keeping tunnel annotations until the iface is empty"
        )

    if not targets:
        return _ok(cleared, upgraded, skipped)
    if not talosconfig or not Path(talosconfig).is_file():
        return _failed(
            "talosconfig missing; cannot install iSCSI before Longhorn",
            cleared,
            upgraded,
            skipped,
        )
    if not image_has_longhorn_extensions(image):
        return _failed(
            "install image has no Longhorn iSCSI extensions",
            cleared,
            upgraded,
            skipped,
        )

    talosctl = talosctl_command()
    for target in targets:
        name = target["name"]
        kind = target.get("kind") or "node"
        text = _services_text(run, talosctl, target["ip"], talosconfig)
        if text is None:
            return _failed(
                f"{name}: talosctl services failed",
                cleared,
                upgraded,
                skipped,
            )
        if iscsi_running(parse_services(text)):
            skipped.append(name)
            log(f"[talos] {name} already has ext-iscsid")
            continue
        log(f"[talos] {name} ({kind}) is missing ext-iscsid; upgrading")
        _request_upgrade(run, log, talosctl, target, image, talosconfig)
        ready = _wait_for_iscsi(
            run,
            talosctl=talosctl,
            ip=target["ip"],
            talosconfig=talosconfig,
            wait_s=iscsi_wait_s,
            poll_s=poll_s,
            now=now,
            sleep=sleep,
        )
        if not ready:
            return _failed(
                f"{name}: ext-iscsid did not become Running",
                cleared,
                upgraded,
                skipped,
            )
        upgraded.append(name)
        log(f"[talos] {name} ext-iscsid is Running")
    return _ok(cleared, upgraded, skipped)


def _ok(
    cleared: list[str], upgraded: list[str], skipped: list[str]
) -> dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        "cleared": cleared,
        "upgraded": upgraded,
        "skipped": skipped,
    }


def _failed(
    error: str,
    cleared: list[str],
    upgraded: list[str],
    skipped: list[str],
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "cleared": cleared,
        "upgraded": upgraded,
        "skipped": skipped,
    }


def _parse_node(item: str) -> dict[str, str]:
    name, sep, ip = item.partition("=")
    name = name.strip()
    ip = ip.strip()
    if not sep or not name or not ip:
        raise ValueError(f"invalid node {item!r}")
    return {"name": name, "ip": ip, "kind": "node"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="talos-prepare")
    parser.add_argument("--talosconfig", default="")
    parser.add_argument("--image", required=True)
    parser.add_argument("--node", action="append", default=[])
    args = parser.parse_args(argv)
    targets: list[dict[str, str]] = []
    for item in args.node:
        try:
            targets.append(_parse_node(item))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    result = prepare_before_longhorn(
        targets=targets,
        image=args.image,
        talosconfig=args.talosconfig,
        log=lambda line: print(line, flush=True),
    )
    if not result.get("ok"):
        print(result.get("error") or "talos prepare failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
