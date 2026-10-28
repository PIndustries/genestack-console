from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services import console_activation as activation
from app.services import updatecheck

OLD = b"\x7fELFold-program"
NEW = b"\x7fELFnew-program"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    prefix = tmp_path / "console"
    (prefix / "bin").mkdir(parents=True)
    data = prefix / "data"
    data.mkdir()
    settings = SimpleNamespace(data_dir=data)
    binary = prefix / "bin/genestack-console"
    binary.write_bytes(OLD)
    binary.chmod(0o755)
    staged = prefix / "bin/gsc-update-fixture"
    staged.write_bytes(NEW)
    staged.chmod(0o700)
    monkeypatch.setattr(activation, "host_prefix", lambda: prefix)
    monkeypatch.setattr(updatecheck, "host_prefix", lambda: prefix)
    monkeypatch.setattr(activation, "worker_available", lambda _settings: True)
    monkeypatch.setattr(activation, "frozen", lambda: True)
    monkeypatch.setattr(activation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(activation, "_owns_control", lambda _settings: True)
    monkeypatch.setattr(activation, "_services_running", lambda _digest: False)
    return settings, binary, staged


def test_activation_waits_for_running_work_and_keeps_gate_until_both_services_match(
    installation, monkeypatch
):
    settings, binary, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")
    assert binary.read_bytes() == OLD and staged.exists()
    calls = []
    restart = lambda: (calls.append("restart") or True, "restarting", True)
    assert not activation.activate_if_idle(settings, busy=lambda: 1, restart=restart)
    assert binary.read_bytes() == OLD and calls == []
    assert activation.activate_if_idle(settings, busy=lambda: 0, restart=restart)
    assert binary.read_bytes() == NEW and not staged.exists()
    assert activation.pending(settings), (
        "No new claims before both services execute the new program"
    )
    monkeypatch.setattr(
        activation, "_services_running", lambda digest: digest == hashlib.sha256(NEW).hexdigest()
    )
    assert not activation.activate_if_idle(settings, busy=lambda: 0, restart=restart)
    assert not activation.pending(settings) and calls == ["restart"]


def test_failed_restart_is_retryable_after_the_atomic_replace(installation):
    settings, binary, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")
    assert not activation.activate_if_idle(
        settings, busy=lambda: 0, restart=lambda: (False, "unavailable", False)
    )
    assert binary.read_bytes() == NEW and not staged.exists() and activation.pending(settings)
    assert activation.activate_if_idle(
        settings, busy=lambda: 0, restart=lambda: (True, "restarting", True)
    )
    assert binary.read_bytes() == NEW and activation.pending(settings)


@pytest.mark.parametrize("kind", ["traversal", "tampered", "symlink"])
def test_invalid_requests_never_replace_the_program_or_touch_foreign_files(
    installation, tmp_path, kind
):
    settings, binary, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"private-config-preserved")
    request = activation._path(settings, "request")
    if kind == "traversal":
        value = json.loads(request.read_text())
        value["staged"] = "../../foreign"
        request.write_text(json.dumps(value))
    elif kind == "tampered":
        staged.write_bytes(b"\x7fELFchanged")
    else:
        staged.unlink()
        staged.symlink_to(foreign)
    assert not activation.activate_if_idle(
        settings, busy=lambda: 0, restart=lambda: pytest.fail("Must not restart")
    )
    assert binary.read_bytes() == OLD and foreign.read_bytes() == b"private-config-preserved"
    assert not activation.pending(settings)


def test_a_second_download_cannot_overwrite_an_existing_request(installation):
    settings, _, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")
    request = activation._path(settings, "request")
    before = request.read_bytes()
    other = staged.with_name("gsc-update-second")
    other.write_bytes(NEW)
    other.chmod(0o700)
    with pytest.raises(FileExistsError):
        activation.queue(settings, other, "2026.10.28.3")
    assert request.read_bytes() == before


def test_unreadable_job_state_defers_without_changing_program(installation):
    settings, binary, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")

    def unreadable():
        raise RuntimeError("database unavailable")

    assert not activation.activate_if_idle(
        settings, busy=unreadable, restart=lambda: pytest.fail("Must wait")
    )
    assert binary.read_bytes() == OLD and staged.exists() and activation.pending(settings)


def test_nonroot_api_only_stages_and_queues_activation(installation, monkeypatch):
    from app.services import bootc

    settings, binary, _ = installation
    monkeypatch.setattr(activation.os, "geteuid", lambda: 501)
    monkeypatch.setattr(bootc, "installed", lambda: True)
    monkeypatch.setattr(
        updatecheck,
        "status",
        lambda _: {
            "latest": "2026.10.28.2",
            "binary": "https://example.invalid/program",
            "update_available": True,
        },
    )
    monkeypatch.setattr(updatecheck, "active_jobs", lambda: 0)
    monkeypatch.setattr(updatecheck, "_validate_program", lambda _path, _version: True)

    class Download:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield NEW

        def stream(self, *_args):
            return self

    monkeypatch.setattr(updatecheck.httpx, "Client", lambda **_kwargs: Download())
    result = updatecheck._apply_binary_locked(settings)
    assert result["ok"] and result["activation_queued"]
    assert binary.read_bytes() == OLD
    value = json.loads(activation._path(settings, "request").read_text())
    assert value["sha256"] == hashlib.sha256(NEW).hexdigest()
    assert (binary.parent / value["staged"]).read_bytes() == NEW


def test_manual_update_refuses_busy_jobs_before_any_download(installation, monkeypatch):
    settings, binary, _ = installation
    monkeypatch.setattr(updatecheck, "status", lambda _: {})
    monkeypatch.setattr(updatecheck, "active_jobs", lambda: 1)
    monkeypatch.setattr(
        updatecheck.httpx, "Client", lambda **_: pytest.fail("No download while busy")
    )
    result = updatecheck._apply_binary_locked(settings)
    assert not result["applied"] and binary.read_bytes() == OLD


def test_appliance_watch_uses_managed_worker_without_changing_legacy_bootc_behavior(monkeypatch):
    from app.services import bootc

    monkeypatch.setattr(bootc, "installed", lambda: True)
    settings = SimpleNamespace(update_watch=True)
    info = {"latest": "2026.10.28.2", "update_available": True}
    monkeypatch.setattr(activation, "worker_available", lambda _: False)
    result = updatecheck.watch_once(settings, info=info, installed=True, busy=lambda: 0)
    assert result["skipped"] == "bootc"
    monkeypatch.setattr(activation, "worker_available", lambda _: True)
    result = updatecheck.watch_once(
        settings, info=info, installed=True, busy=lambda: 0, apply=lambda _: {"applied": True}
    )
    assert result["applied"]


def test_claim_gate_and_racing_inline_call_retain_queued_work(tmp_path, monkeypatch):
    from app.db import Base
    from app.models import Job, JobStatus
    from app.services.job_runner import JobRunner
    from app.worker import runner

    engine = create_engine("sqlite:///" + str(tmp_path / "jobs.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "SessionLocal", factory)
    monkeypatch.setattr(runner, "init_db", lambda: None)
    monkeypatch.setattr(activation, "pending", lambda _: True)
    with factory() as db:
        job = Job(
            operation="internal.health",
            params={},
            status=JobStatus.queued,
            log_text="",
            created_by="update-test",
        )
        db.add(job)
        db.commit()
        identifier = job.id
    assert runner.process_queued_jobs(once=True, abandon_running=False) == 0
    with factory() as db:
        job = db.get(Job, identifier)
        assert job.status == JobStatus.queued
        job.status = JobStatus.running
        db.commit()
        JobRunner(db).run_job(job)
        assert job.status == JobStatus.queued and job.started_at is None
    engine.dispose()


def test_vendor_service_unit_is_recognized_for_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(
        activation.Path,
        "is_file",
        lambda p: str(p) == "/usr/lib/systemd/system/genestack-console.service",
    )
    monkeypatch.setattr(
        updatecheck.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name in {"systemctl", "systemd-run"} else None,
    )
    monkeypatch.setattr(updatecheck.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        updatecheck.subprocess,
        "run",
        lambda command, **_: (
            calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    restarted, _, ok = updatecheck._restart_services(immediate=True)
    assert restarted and ok and "--wait" in calls[0] and "--on-active=1s" not in calls[0]
    assert calls[0][-2:] == ["genestack-console.service", "genestack-console-worker.service"]


def test_worker_daemon_stops_claiming_after_scheduling_activation(monkeypatch):
    from app.worker import runner

    calls = []
    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(activation, "publish_worker", lambda _: calls.append("advertise"))
    monkeypatch.setattr(activation, "activate_if_idle", lambda _: calls.append("activate") or True)
    monkeypatch.setattr(activation, "withdraw_worker", lambda _: calls.append("withdraw"))
    monkeypatch.setattr(
        runner, "process_queued_jobs", lambda **_: pytest.fail("Do not claim before restart")
    )
    assert runner.run_daemon(interval=0, limit=10, collector_enabled=False, max_ticks=1) == 0
    assert calls == ["advertise", "activate", "withdraw"]


def test_jobs_arriving_during_download_prevent_staging_activation(installation, monkeypatch):
    settings, binary, original_stage = installation
    monkeypatch.setattr(
        updatecheck,
        "status",
        lambda _: {
            "latest": "2026.10.28.2",
            "binary": "https://example.invalid/program",
            "update_available": True,
        },
    )
    counts = iter([0, 1])
    monkeypatch.setattr(updatecheck, "active_jobs", lambda: next(counts))

    class Download:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield NEW

        def stream(self, *_):
            return self

    monkeypatch.setattr(updatecheck.httpx, "Client", lambda **_: Download())
    result = updatecheck._apply_binary_locked(settings)
    assert not result["applied"] and not activation.pending(settings) and binary.read_bytes() == OLD
    assert list(binary.parent.glob("gsc-update-*")) == [original_stage]


def test_api_does_not_redownload_a_pending_activation(installation, monkeypatch):
    settings, binary, staged = installation
    activation.queue(settings, staged, "2026.10.28.2")
    monkeypatch.setattr(updatecheck, "status", lambda _: {})
    monkeypatch.setattr(
        updatecheck, "active_jobs", lambda: pytest.fail("Pending update must return directly")
    )
    result = updatecheck._apply_binary_locked(settings)
    assert result["activation_queued"] and not result["applied"] and binary.read_bytes() == OLD


def test_unprivileged_process_cannot_advertise_worker_control(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "frozen", lambda: True)
    monkeypatch.setattr(activation.os, "geteuid", lambda: 501)
    settings = SimpleNamespace(data_dir=tmp_path)
    assert not activation.publish_worker(settings)
    assert not activation._path(settings, "worker").exists()


def test_program_version_validation_uses_a_clean_frozen_child_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "old-runtime")
    monkeypatch.setenv("PYTHONPATH", "old-development-code")
    monkeypatch.setenv("LD_LIBRARY_PATH", "old-runtime-libraries")
    monkeypatch.setenv("CONSOLE_CONFIG", "/existing/config.yaml")

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(
            returncode=0, stdout="genestack-console 2026.10.28.2 (build)", stderr=""
        )

    monkeypatch.setattr(updatecheck.subprocess, "run", run)
    assert updatecheck._validate_program(tmp_path / "candidate", "2026.10.28.2")
    assert not updatecheck._validate_program(tmp_path / "candidate", "2026.10.28.3")
    assert calls[0][0][-1] == "--version"
    for key in ["NUITKA_ONEFILE_PARENT", "PYTHONPATH", "LD_LIBRARY_PATH"]:
        assert key not in calls[0][1]
    assert calls[0][1]["CONSOLE_CONFIG"] == "/existing/config.yaml"


def test_incompatible_program_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        updatecheck.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=127, stdout="", stderr="missing runtime"
        ),
    )
    assert not updatecheck._validate_program(tmp_path / "candidate", "2026.10.28.2")
