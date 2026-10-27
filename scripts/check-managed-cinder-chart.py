#!/usr/bin/env python3
"""Verify managed storage against an actual pinned Cinder chart and base values.

No cluster access, install, network request, or release mutation.
"""

import argparse
import base64
import configparser
import hashlib
import json
import runpy
import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chart", type=Path)
    parser.add_argument("base_values", type=Path)
    args = parser.parse_args()
    runtime = Path(__file__).resolve().parents[1] / "app/assets/managed-cinder.py.in"
    source = runpy.run_path(str(runtime))
    owner = "chart-contract"
    values = source["helm_values"](owner, "pvc-owned.longhorn-system.svc", "/pvc-owned/cinder")
    with tempfile.TemporaryDirectory(prefix="gsc-storage-chart-") as directory:
        override = Path(directory) / "managed.json"
        override.write_text(json.dumps(values))
        result = subprocess.run(
            [
                "helm",
                "template",
                "cinder",
                str(args.chart),
                "--namespace",
                "openstack",
                "--values",
                str(args.base_values),
                "--values",
                str(override),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise SystemExit("Pinned Cinder chart failed to render")
    objects = [x for x in yaml.safe_load_all(result.stdout) if x]

    def find(kind, name):
        return next(x for x in objects if x["kind"] == kind and x["metadata"]["name"] == name)

    worker = find("Deployment", "cinder-volume")
    assert worker["spec"]["replicas"] == 1
    assert worker["spec"]["strategy"] == {"type": "Recreate"}
    pod = worker["spec"]["template"]
    assert pod["metadata"]["annotations"]["genestack.dev/storage-owner"] == owner
    assert pod["spec"]["securityContext"]["fsGroup"] == 42424
    assert pod["spec"]["nodeSelector"] == {"openstack-compute-node": "enabled"}
    assert not any("ceph-keyring" in c["name"] for c in pod["spec"]["initContainers"])
    find("Job", "cinder-bootstrap")
    secret = find("Secret", "cinder-etc")
    backend = configparser.ConfigParser()
    backend.read_string(base64.b64decode(secret["data"]["backends.conf"]).decode())
    assert backend.sections() == ["gsc_longhorn"]
    assert backend.get("gsc_longhorn", "backend_host") == "gsc-" + owner
    assert backend.get("gsc_longhorn", "nas_host") == "pvc-owned.longhorn-system.svc"
    config = configparser.ConfigParser()
    config.read_string(base64.b64decode(secret["data"]["cinder.conf"]).decode())
    assert config["DEFAULT"]["enabled_backends"] == "gsc_longhorn"
    assert config["DEFAULT"]["default_volume_type"] == "genestack"
    binary = next(
        x for x in objects if x["kind"] == "ConfigMap" and x["metadata"]["name"] == "cinder-bin"
    )
    assert "volume type create" in binary["data"]["bootstrap.sh"]
    assert "GENESTACK_LONGHORN" in binary["data"]["bootstrap.sh"]
    print(
        json.dumps(
            {
                "result": "PASS",
                "checks": [
                    "single writer",
                    "retained ownership marker",
                    "NFS backend",
                    "stable backend host",
                    "volume type bootstrap",
                    "no RBD keyring dependency",
                ],
                "chart_sha256": hashlib.sha256(args.chart.read_bytes()).hexdigest(),
                "base_values_sha256": hashlib.sha256(args.base_values.read_bytes()).hexdigest(),
                "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
