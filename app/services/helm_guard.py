"""Stop a Helm install that is waiting on a pod Kubernetes cannot schedule.

Genestack charts pin control-plane services to ``openstack-control-plane``
and pass ``--timeout 120m``. Helm waits that long for hook jobs. A pod that
cannot schedule never becomes Ready, so one taint burns two hours. This
wrapper is placed ahead of the real ``helm`` on PATH for a local live
deploy. ``helm list`` and ``helm show`` pass through. An upgrade that still
has an unschedulable pod after a short grace is stopped with the scheduler
message. ``helm upgrade`` is not stubbed and its wait is unchanged.

Install scripts each run ``helm repo update``, which re-downloads every
repository index. The first update in a deploy still downloads. Later
updates reuse that on-disk index instead of downloading it again.

cert-manager is installed with Gateway API support before the Envoy chart
has applied those CRDs. The same wrapper applies the CRDs from the Envoy
chart version in ``helm-chart-versions.yaml`` before that upgrade.
"""

from __future__ import annotations

import inspect
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_UNSCHEDULABLE_GRACE_S = 60
DEFAULT_GUARD_DELAY_S = 45
# Beside the wrapper. Cleared when the wrapper is installed so each deploy
# gets one real ``helm repo update``.
REPO_INDEX_STAMP = ".repo-update-stamp"


def envoy_gateway_version(versions_text: str) -> str:
    """Chart version the infrastructure script will install for Envoy."""
    for line in versions_text.splitlines():
        if not line.startswith("envoyproxy-gateway:"):
            continue
        _key, _sep, value = line.partition(":")
        version = value.strip().strip("'\"")
        if version:
            return version
    return "v1.7.0"


def _parse_rfc3339(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017
    return parsed


def unschedulable_messages(
    pod_list: dict, *, now: datetime, grace_s: float
) -> list[str]:
    """Pods Pending because the scheduler has no place for them.

    A pod that is still pulling an image, or that only just became
    unschedulable, is left alone. ``grace_s`` is how long that condition
    has to have been true.
    """
    found: list[str] = []
    items = pod_list.get("items") if isinstance(pod_list, dict) else None
    if not isinstance(items, list):
        return found
    for pod in items:
        if not isinstance(pod, dict):
            continue
        meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        conditions = status.get("conditions") if isinstance(status, dict) else None
        if not isinstance(conditions, list):
            continue
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") != "PodScheduled" or cond.get("status") != "False":
                continue
            if cond.get("reason") != "Unschedulable":
                continue
            changed = _parse_rfc3339(cond.get("lastTransitionTime"))
            if changed is None:
                continue
            if (now - changed).total_seconds() < grace_s:
                continue
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            message = str(cond.get("message") or "unschedulable").strip()
            found.append(f"{namespace}/{name}: {message}")
    return found


# A compiled console has no .py files, so inspect.getsource raises OSError
# ("could not get source code") and the deploy dies before the first stage.
# These copies are what the wrapper uses then. tests/test_helm_guard.py
# checks them against the live functions whenever source is available.
_FROZEN_HELPERS = {
    "envoy_gateway_version": '''\
def envoy_gateway_version(versions_text: str) -> str:
    """Chart version the infrastructure script will install for Envoy."""
    for line in versions_text.splitlines():
        if not line.startswith("envoyproxy-gateway:"):
            continue
        _key, _sep, value = line.partition(":")
        version = value.strip().strip("'\\"")
        if version:
            return version
    return "v1.7.0"
''',
    "_parse_rfc3339": '''\
def _parse_rfc3339(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017
    return parsed
''',
    "unschedulable_messages": '''\
def unschedulable_messages(
    pod_list: dict, *, now: datetime, grace_s: float
) -> list[str]:
    """Pods Pending because the scheduler has no place for them.

    A pod that is still pulling an image, or that only just became
    unschedulable, is left alone. ``grace_s`` is how long that condition
    has to have been true.
    """
    found: list[str] = []
    items = pod_list.get("items") if isinstance(pod_list, dict) else None
    if not isinstance(items, list):
        return found
    for pod in items:
        if not isinstance(pod, dict):
            continue
        meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        conditions = status.get("conditions") if isinstance(status, dict) else None
        if not isinstance(conditions, list):
            continue
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") != "PodScheduled" or cond.get("status") != "False":
                continue
            if cond.get("reason") != "Unschedulable":
                continue
            changed = _parse_rfc3339(cond.get("lastTransitionTime"))
            if changed is None:
                continue
            if (now - changed).total_seconds() < grace_s:
                continue
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            message = str(cond.get("message") or "unschedulable").strip()
            found.append(f"{namespace}/{name}: {message}")
    return found
''',
}


def _helper_source(fn) -> str:
    """Function text for the wrapper. The frozen copy is the compiled-binary path."""
    try:
        return textwrap.dedent(inspect.getsource(fn))
    except OSError:
        return _FROZEN_HELPERS[fn.__name__]


def _guard_main_source() -> str:
    detect = _helper_source(unschedulable_messages)
    parse = _helper_source(_parse_rfc3339)
    version = _helper_source(envoy_gateway_version)
    # Function bodies contain braces. Replace markers instead of formatting.
    template = textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        import json
        import os
        import signal
        import subprocess
        import sys
        import time
        from datetime import datetime, timezone

        __PARSE__

        __DETECT__

        __VERSION__

        def _namespace(argv):
            previous = ""
            for arg in argv:
                if previous in ("--namespace", "-n"):
                    return arg
                if arg.startswith("--namespace="):
                    return arg.split("=", 1)[1]
                previous = arg
            return ""

        def _delay():
            raw = os.environ.get("GENESTACK_HELM_GUARD_DELAY", "__GUARD_DELAY__")
            try:
                return max(0.0, float(raw))
            except ValueError:
                return __GUARD_DELAY__

        def _grace():
            raw = os.environ.get(
                "GENESTACK_HELM_UNSCHEDULABLE_GRACE", "__UNSCHEDULABLE_GRACE__"
            )
            try:
                return max(0.0, float(raw))
            except ValueError:
                return __UNSCHEDULABLE_GRACE__

        def _versions_text():
            root = os.environ.get("GENESTACK_OVERRIDES_DIR") or os.environ.get(
                "GENESTACK_CONFIG"
            )
            if not root:
                return ""
            path = os.path.join(root, "helm-chart-versions.yaml")
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read()
            except OSError:
                return ""

        def _crd_present():
            probe = subprocess.run(
                ["kubectl", "get", "crd", "gateways.gateway.networking.k8s.io"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return probe.returncode == 0

        def _apply_gateway_crds(real):
            if _crd_present():
                return 0
            version = envoy_gateway_version(_versions_text())
            print(
                "Applying Gateway API CRDs from envoy gateway "
                + version
                + " before cert-manager",
                flush=True,
            )
            show = subprocess.run(
                [
                    real,
                    "show",
                    "crds",
                    "oci://docker.io/envoyproxy/gateway-helm",
                    "--version",
                    version,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if show.returncode != 0 or not show.stdout.strip():
                sys.stderr.write(
                    "helm-guard: Gateway API CRDs were not applied; "
                    "cert-manager enables them before Envoy installs the chart\\n"
                )
                if show.stderr:
                    sys.stderr.write(show.stderr)
                return show.returncode or 2
            applied = subprocess.run(
                ["kubectl", "apply", "--server-side", "--force-conflicts", "-f", "-"],
                input=show.stdout,
                text=True,
                check=False,
            )
            return applied.returncode

        def _stuck(namespace):
            listed = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace, "-o", "json"],
                capture_output=True,
                text=True,
                check=False,
            )
            if listed.returncode != 0 or not listed.stdout.strip():
                return []
            try:
                payload = json.loads(listed.stdout)
            except json.JSONDecodeError:
                return []
            return unschedulable_messages(
                payload, now=datetime.now(timezone.utc), grace_s=_grace()
            )

        def _repo_stamp():
            here = os.path.dirname(os.path.abspath(__file__))
            return os.path.join(here, "__REPO_STAMP__")

        def _reuse_repo_index(real, argv):
            # helm repo update re-fetches every index. One success per deploy
            # is enough; chart versions do not change mid-deploy. A failed
            # update leaves no stamp, so the next call still downloads.
            if len(argv) < 2 or argv[0] != "repo" or argv[1] != "update":
                return None
            if "--help" in argv or "-h" in argv:
                return None
            stamp = _repo_stamp()
            if os.path.isfile(stamp):
                print(
                    "helm-guard: reusing the helm repo index from this deploy",
                    flush=True,
                )
                return 0
            proc = subprocess.run([real, *argv], check=False)
            code = proc.returncode if proc.returncode is not None else 1
            if code == 0:
                try:
                    fd = os.open(stamp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o640)
                    os.write(fd, b"ok\\n")
                    os.close(fd)
                except OSError:
                    pass
            return code

        def main(argv):
            real = os.environ.get("GENESTACK_HELM_REAL", "").strip()
            if not real:
                sys.stderr.write("helm-guard: GENESTACK_HELM_REAL is empty\\n")
                return 127
            reused = _reuse_repo_index(real, argv)
            if reused is not None:
                return reused
            if not argv or argv[0] not in ("upgrade", "install"):
                os.execv(real, [real, *argv])
            if "cert-manager" in " ".join(argv):
                crd_rc = _apply_gateway_crds(real)
                if crd_rc != 0:
                    return crd_rc
            proc = subprocess.Popen(
                [real, *argv], start_new_session=True
            )
            time.sleep(_delay())
            namespace = _namespace(argv)
            while proc.poll() is None:
                if namespace:
                    stuck = _stuck(namespace)
                    if stuck:
                        print("helm-guard: " + stuck[0], flush=True)
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=10)
                        return 2
                time.sleep(15)
            return proc.returncode or 0

        if __name__ == "__main__":
            raise SystemExit(main(sys.argv[1:]))
        '''
    )
    return (
        template.replace("__PARSE__", parse)
        .replace("__DETECT__", detect)
        .replace("__VERSION__", version)
        .replace("__GUARD_DELAY__", str(DEFAULT_GUARD_DELAY_S))
        .replace("__UNSCHEDULABLE_GRACE__", str(DEFAULT_UNSCHEDULABLE_GRACE_S))
        .replace("__REPO_STAMP__", REPO_INDEX_STAMP)
    )


def render_helm_guard() -> str:
    """Standalone wrapper. It does not import the console package."""
    return _guard_main_source()


def install_helm_guard(dest: Path) -> Path:
    """Write the wrapper as ``dest/helm`` and return that path.

    Drops a previous deploy's repo-index stamp so the next ``helm repo
    update`` downloads once.
    """
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "helm"
    path.write_text(render_helm_guard(), encoding="utf-8")
    os.chmod(path, 0o750)
    (dest / REPO_INDEX_STAMP).unlink(missing_ok=True)
    return path


# Genestack install scripts call `yq eval`. Python yq accepts --version and
# then treats the expression as a filename, so Neutron sees an empty TLS flag.
_YQ_SHIM = """\
#!/bin/sh
# Use mikefarah yq. A Python yq earlier on PATH breaks `yq eval`.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin="$here/yq.bin"
if [ -x "$bin" ]; then
  exec "$bin" "$@"
fi
# YQ_DOWNLOAD_URL is the binary to install. Search PATH only when it is
# unset: GitHub-hosted Ubuntu has mikefarah yq at /usr/bin/yq, which would
# otherwise hide a python yq that is first and skip the download.
url=${YQ_DOWNLOAD_URL:-}
if [ -z "$url" ]; then
  found=
  oldifs=$IFS
  IFS=:
  for dir in $PATH; do
    [ -n "$dir" ] || continue
    cand="$dir/yq"
    if [ "$cand" = "$here/yq" ] || [ ! -x "$cand" ] || [ -d "$cand" ]; then
      continue
    fi
    ver=$("$cand" --version 2>&1 || true)
    case "$ver" in
      *mikefarah*|*github.com/mikefarah/yq*)
        found=$cand
        break
        ;;
    esac
  done
  IFS=$oldifs
  if [ -n "$found" ]; then
    exec "$found" "$@"
  fi
  osname=$(uname -s)
  case "$osname" in
    Linux) osname=linux ;;
    Darwin) osname=darwin ;;
    *)
      echo "yq: unsupported OS $(uname -s)" >&2
      exit 1
      ;;
  esac
  arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *)
      echo "yq: unsupported architecture $(uname -m)" >&2
      exit 1
      ;;
  esac
  url="https://github.com/mikefarah/yq/releases/download/v4.47.2/yq_${osname}_${arch}"
fi
tmp="$bin.part"
rm -f "$tmp"
case "$url" in
  file://*)
    # Local payload. curl on some Linux builds rejects the file protocol.
    src=${url#file://}
    case "$src" in
      localhost/*) src="/${src#localhost/}" ;;
    esac
    cp "$src" "$tmp" || {
      echo "yq: could not download mikefarah yq" >&2
      exit 1
    }
    ;;
  *)
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$url" -o "$tmp" || {
        echo "yq: could not download mikefarah yq" >&2
        exit 1
      }
    elif command -v wget >/dev/null 2>&1; then
      wget -q -O "$tmp" "$url" || {
        echo "yq: could not download mikefarah yq" >&2
        exit 1
      }
    else
      echo "yq: curl or wget is required to install mikefarah yq" >&2
      exit 1
    fi
    ;;
esac
chmod 755 "$tmp"
ver=$("$tmp" --version 2>&1 || true)
case "$ver" in
  *mikefarah*|*github.com/mikefarah/yq*) ;;
  *)
    rm -f "$tmp"
    echo "yq: downloaded binary is not mikefarah" >&2
    exit 1
    ;;
esac
mv "$tmp" "$bin"
exec "$bin" "$@"
"""


def install_yq_shim(dest: Path) -> Path:
    """Write ``dest/yq`` so install scripts find mikefarah yq first."""
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "yq"
    path.write_text(_YQ_SHIM, encoding="utf-8")
    os.chmod(path, 0o750)
    return path


def arm_helm_guard(extra_env: dict[str, str], dest: Path) -> dict[str, str]:
    """Return env that runs the wrapper instead of Helm.

    Unchanged when ``helm`` is not installed. The real binary is recorded
    before PATH changes, so the wrapper cannot call itself.
    """
    import shutil

    search = extra_env.get("PATH") or os.environ.get("PATH")
    real = shutil.which("helm", path=search)
    if not real:
        return extra_env
    directory = install_helm_guard(dest).parent
    install_yq_shim(directory)
    updated = dict(extra_env)
    current = updated.get("PATH") or os.environ.get("PATH") or ""
    updated["PATH"] = f"{directory}{os.pathsep}{current}" if current else str(directory)
    updated["GENESTACK_HELM_REAL"] = real
    return updated
