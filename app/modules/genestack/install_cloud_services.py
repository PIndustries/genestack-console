"""Complete the private cloud after the reviewed cluster network and storage."""

from __future__ import annotations

import json
import re
import shutil
import time

import httpx
import yaml
from sqlalchemy import select

from app.models import Job, JobLogChunk, JobStatus

HANDLERS = ("genestack_install_cloud_services",)
OPERATION = {
    "id": "genestack.install_cloud_services",
    "name": "Install private cloud services",
    "description": "Size and install OpenStack on the reviewed nodes, retaining their existing storage and keeping provider networks unconfigured.",
    "required_role": "admin",
    "backend": "genestack",
    "mutating": True,
    "params": [],
    "handler": "genestack_install_cloud_services",
    "timeout_seconds": 21600,
}


def run(
    self,
    handler,
    op,
    job,
    env,
    log,
    ctx,
    params,
    deadline,
    check_cancel,
    dry,
    timeout,
    gs_root,
    ans_root,
    extra_env,
    ssh_target,
    remote_env,
    executor,
    agent_env_id,
):
    from app.services import (
        cloud_services,
        cluster_network,
        cluster_storage,
        deploy,
        envconfig,
        reviewed_cluster,
        talos,
        talos_compute,
    )
    from app.services import genestack_bridge as bridge
    from app.services.osclient import OpenStackError, client_from_context

    if env is None or ssh_target or agent_env_id or ctx.config_dir is None:
        return {
            "ok": False,
            "error": "Guided cloud installation requires the local deployer appliance",
            "returncode": 2,
        }
    try:
        original, version = cluster_network.configuration(self.db, env)
        storage = self.db.scalar(
            select(Job)
            .where(
                Job.environment_id == env.id, Job.operation == "genestack.install_cluster_storage"
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if storage is None or storage.status != JobStatus.success or storage.dry_run is not False:
            raise ValueError("Complete the real persistent storage check before cloud services")
        doc, plan = cloud_services.configure(original, env.id)
        if dry:
            return {"ok": True, "dry_run": True, "proposal": plan, "hardware_modified": False}
        path = ctx.ensure_kubeconfig()
        if not path:
            raise ValueError("This cloud has no stored Kubernetes connection")
        scoped = dict(extra_env or {}, KUBECONFIG=path)
        scoped.update(envconfig.doc_env(doc))
        scoped["ENVOY_GATEWAY_CONFIG_FILE"] = str(ctx.config_dir / "envoy-gateways.yaml")

        def command(argv, limit=60):
            check_cancel()
            remaining = int((deadline or (time.monotonic() + limit)) - time.monotonic())
            if remaining <= 0:
                raise ValueError("Cloud installation deadline exceeded")
            return bridge.run_command(
                argv,
                cwd=gs_root,
                timeout=min(limit, remaining),
                dry_run=False,
                extra_env=scoped,
                log=log,
            )

        result = command(["kubectl", "get", "nodes", "-o", "json", "--request-timeout=20s"], 30)
        if result.get("returncode") != 0:
            raise ValueError("Cannot verify the reviewed cloud nodes")
        nodes = json.loads(result.get("stdout") or "{}")
        cluster_network.verified_nodes(doc, nodes)
        classes = command(
            ["kubectl", "get", "storageclass", "general", "general-multi-attach", "-o", "json"],
            30,
        )
        if classes.get("returncode") != 0:
            raise ValueError("Cannot verify the installed Longhorn storage classes")
        class_list = json.loads(classes.get("stdout") or "{}")
        doc = cluster_storage.reconcile_existing_storage_classes(
            doc, env.id, class_list.get("items") or []
        )
        scoped.update(envconfig.doc_env(doc))
        if any(
            not any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in n.get("status", {}).get("conditions", [])
            )
            for n in nodes["items"]
        ):
            raise ValueError("Every reviewed node must be Ready before cloud services")
        # An inherited external bridge is never repurposed by this guided path.
        if any(
            n["metadata"].get("annotations", {}).get("ovn.openstack.org/ports")
            for n in nodes["items"]
        ):
            raise ValueError("An existing provider uplink requires advanced network review")
        compute = sorted(
            name for name, server in doc["servers"].items() if "compute" in server["roles"]
        )
        for name in compute:
            ip = doc["servers"][name]["ip"]
            inspected = command(
                [
                    talos.talosctl_command(),
                    "--nodes",
                    ip,
                    "--endpoints",
                    ip,
                    "--talosconfig",
                    str(ctx.config_dir / "talos/talosconfig"),
                    "read",
                    "/proc/cpuinfo",
                ],
                30,
            )
            if inspected.get("returncode") != 0:
                raise ValueError("Cannot inspect the reviewed compute CPU")
            doc = talos_compute.configure(doc, name, inspected.get("stdout") or "")
        reviewed_cluster.validate_talos_configuration(
            doc, env, temporary_root=self.settings.data_dir / "installation-preflight", log=log
        )
        if not original.get("cloud_services"):
            existing = command(
                ["kubectl", "-n", "openstack", "get", "deployments,statefulsets", "-o", "json"], 30
            )
            if existing.get("returncode") != 0:
                raise ValueError(
                    "Cannot check for existing cloud services before first installation"
                )
            workloads = json.loads(existing.get("stdout") or "{}").get("items") or []
            if any(w["metadata"]["name"] != "genestack-cinder-data" for w in workloads):
                raise ValueError(
                    "Existing cloud services require advanced review before guided installation"
                )
        if not shutil.which("arping"):
            raise ValueError(
                "Prepare an updated deployer with the private-address conflict check tool"
            )
        # Only new reservations are probed. A retained MetalLB VIP responds on retries.
        if not original.get("cloud_services"):
            import ipaddress

            first = ipaddress.IPv4Address(plan["endpoint_ip"])
            interface = str(doc["pxe"]["interface"])
            for offset in range(8):
                result = command(
                    ["arping", "-D", "-c", "2", "-w", "3", "-I", interface, str(first + offset)], 8
                )
                if result.get("returncode") != 0:
                    raise ValueError(
                        "A proposed private service address is already in use; review the network"
                    )
        # The upstream Envoy CLI writes its optional completion here. Minimal
        # immutable deployers may predate the image's bash-completion package.
        completion = command(["install", "-d", "-m", "0755", "/etc/bash_completion.d"], 10)
        if completion.get("returncode") != 0:
            raise ValueError("Cannot prepare the deployer's command completion directory")
        if envconfig.get_current(self.db, env)[1].version != version:
            raise ValueError("Configuration changed; review before installing cloud services")
        if doc != original:
            envconfig.put_version(
                self.db,
                env,
                envconfig._dump(doc),
                job.created_by or "system",
                expected_version=version,
            )
        for name in compute:
            ip = doc["servers"][name]["ip"]
            base = [
                talos.talosctl_command(),
                "--nodes",
                ip,
                "--endpoints",
                ip,
                "--talosconfig",
                str(ctx.config_dir / "talos/talosconfig"),
            ]
            patch = yaml.safe_dump_all(talos_compute.documents(name, doc["talos"], doc["servers"]))
            patched = command(
                [*base, "patch", "machineconfig", "--patch", patch, "--mode=no-reboot"], 60
            )
            if patched.get("returncode") != 0:
                raise ValueError("Cannot configure Talos-managed compute modules without rebooting")
            limit = time.monotonic() + 60
            while True:
                ready = command([*base, "read", "/sys/class/misc/kvm/dev"], 10)
                if ready.get("returncode") == 0 and ":" in (ready.get("stdout") or ""):
                    break
                if time.monotonic() >= limit:
                    raise ValueError("The reviewed compute node has not exposed its KVM device")
                time.sleep(2)
        from_stage = "infrastructure"
        previous = self.db.scalar(
            select(Job)
            .where(
                Job.environment_id == env.id,
                Job.operation == OPERATION["id"],
                Job.status == JobStatus.failed,
                Job.dry_run.is_(False),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if previous is not None:
            chunks = self.db.scalars(
                select(JobLogChunk.body).where(
                    JobLogChunk.job_id == previous.id,
                    JobLogChunk.body.contains("[deploy] pushed config version="),
                )
            ).all()
            applied_versions = [
                int(v)
                for text in chunks
                for v in re.findall(r"\[deploy\] pushed config version=(\d+)\b", text)
            ]
            if cloud_services.storage_retry_candidate(
                previous.error, applied_versions, version, doc == original
            ):
                try:
                    with client_from_context(ctx) as cloud:
                        cloud.list_projects()
                        cloud.list_images()
                        cloud.list_networks()
                        services = cloud.list_compute_services()
                        core_ready = {s.get("host") for s in services} == set(compute) and all(
                            s.get("state") == "up" and s.get("status") == "enabled"
                            for s in services
                        )
                    runtime = command(
                        [
                            "kubectl",
                            "-n",
                            "openstack",
                            "rollout",
                            "status",
                            "daemonset/libvirt-libvirt-default",
                            "--timeout=5s",
                        ],
                        10,
                    )
                    marker = command(
                        [
                            "kubectl",
                            "-n",
                            "openstack",
                            "get",
                            "daemonset",
                            "libvirt-libvirt-default",
                            "-o=jsonpath={.spec.template.metadata.annotations.genestack\\.dev/libvirt-talos-runtime}",
                        ],
                        10,
                    )
                    mount = command(
                        [
                            "kubectl",
                            "-n",
                            "openstack",
                            "get",
                            "daemonset",
                            "libvirt-libvirt-default",
                            "-o=jsonpath={.spec.template.spec.volumes[?(@.name=='run')].hostPath.path}",
                        ],
                        10,
                    )
                    bootstrap = command(
                        [
                            "kubectl",
                            "-n",
                            "openstack",
                            "wait",
                            "--for=condition=complete",
                            "--timeout=5s",
                            "job/nova-cell-setup",
                            "job/nova-bootstrap",
                        ],
                        10,
                    )
                    runtime_matches = (
                        marker.get("returncode") == 0
                        and (marker.get("stdout") or "").strip()
                        == cloud_services.libvirt_runtime_digest()
                        and mount.get("returncode") == 0
                        and (mount.get("stdout") or "").strip() == "/run/libvirt"
                    )
                    if (
                        core_ready
                        and runtime_matches
                        and runtime.get("returncode") == 0
                        and bootstrap.get("returncode") == 0
                    ):
                        from_stage = "platform-extras"
                        log(
                            "[cloud] Earlier services and their applied configuration are verified; retrying block storage"
                        )
                except (OpenStackError, httpx.HTTPError):
                    log("[cloud] Earlier services need verification; repeating full cloud setup")
        result = deploy.run_deploy(
            self.db,
            env,
            ctx,
            log,
            dry_run=False,
            skip_push=False,
            timeout=timeout,
            extra_env=scoped,
            ssh_target=None,
            remote_env={},
            settings=self.settings,
            from_stage=from_stage,
            until_stage="platform-extras",
            include_testing=False,
            deadline=deadline,
            check_cancel=check_cancel,
            parallelism=1,
        )
        if not result.get("ok"):
            return dict(result, cloud_services_ready=False)
        if result.get("optional_failed"):
            return dict(
                result,
                ok=False,
                cloud_services_ready=False,
                error="Required private cloud services failed: "
                + ", ".join(result["optional_failed"]),
            )
        # Availability can still refer to an old pod while an anti-affinity
        # rollout is stuck. Require the updated operator deployments too.
        for name in ["mariadb-operator", "mariadb-operator-webhook"]:
            updated = command(
                [
                    "kubectl",
                    "-n",
                    "mariadb-system",
                    "rollout",
                    "status",
                    "deployment/" + name,
                    "--timeout=300s",
                ],
                310,
            )
            if updated.get("returncode") != 0:
                raise ValueError("The database operator update has not completed")
        for resource in [
            "daemonset/libvirt-libvirt-default",
            "deployment/cinder-api",
            "deployment/cinder-scheduler",
            "deployment/cinder-volume",
        ]:
            ready = command(
                ["kubectl", "-n", "openstack", "rollout", "status", resource, "--timeout=300s"], 310
            )
            if ready.get("returncode") != 0:
                raise ValueError("A required compute or storage workload is not ready: " + resource)
        bootstrapped = command(
            [
                "kubectl",
                "-n",
                "openstack",
                "wait",
                "--for=condition=complete",
                "--timeout=300s",
                "job/nova-cell-setup",
                "job/nova-bootstrap",
                "job/cinder-bootstrap",
            ],
            310,
        )
        if bootstrapped.get("returncode") != 0:
            raise ValueError("The compute and managed storage bootstrap jobs must complete")
        # Helm success is insufficient: use the same authenticated APIs as the apps.
        with client_from_context(ctx) as cloud:
            cloud.list_projects()
            cloud.list_images()
            cloud.list_networks()
            cloud.list_volumes()
            limit = time.monotonic() + 180
            last_error = None
            while True:
                check_cancel()
                error = cloud_services.readiness_error(
                    doc,
                    cloud.list_compute_services(),
                    cloud.json("GET", "volume", cloud._cinder_path("os-services")).get(
                        "services", []
                    ),
                    cloud.list_flavors(),
                    cloud.json("GET", "volume", cloud._cinder_path("types")).get(
                        "volume_types", []
                    ),
                )
                if error is None:
                    break
                if error != last_error:
                    log("[cloud] " + error)
                    last_error = error
                if time.monotonic() >= limit:
                    raise ValueError(error)
                time.sleep(3)
        self.write_audit(
            actor=job.created_by or "system",
            action="env.install_cloud_services",
            resource_type="environment",
            resource_id=env.id,
            environment_id=env.id,
            details={"private_network_only": True, "service_replicas": plan["service_replicas"]},
            success=True,
        )
        return {
            "ok": True,
            "dry_run": False,
            "cloud_services_ready": True,
            "message": "Private cloud APIs and compute are ready. Create a private network and your first server",
        }
    except (ValueError, OSError, OpenStackError) as error:
        return {
            "ok": False,
            "dry_run": False,
            "cloud_services_ready": False,
            "error": str(error),
            "returncode": 2,
        }
