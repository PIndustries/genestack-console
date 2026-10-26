"""Cancelled queued services must still let the failed pipeline finish."""

from __future__ import annotations

import concurrent.futures
import threading
import types

from app.services import deploy
from app.services.envcontext import EnvContext


def test_parallel_failure_drains_cancelled_queued_futures(monkeypatch, tmp_path):
    submitted = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    result = []
    errors = []
    real_pool = concurrent.futures.ThreadPoolExecutor

    class QueuedPool(real_pool):
        """Hold the failing item until the later items are definitely queued."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.count = 0

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            cancel = future.cancel

            def observe_cancel():
                did_cancel = cancel()
                if did_cancel:
                    cancelled.set()
                return did_cancel

            future.cancel = observe_cancel
            self.count += 1
            if self.count == 4:
                submitted.set()
            return future

    monkeypatch.setattr(
        deploy,
        "concurrent",
        types.SimpleNamespace(
            futures=types.SimpleNamespace(
                ThreadPoolExecutor=QueuedPool,
                as_completed=concurrent.futures.as_completed,
                CancelledError=concurrent.futures.CancelledError,
            )
        ),
    )

    def command(argv, **kwargs):
        if argv[1] == "fail.sh":
            assert submitted.wait(5)
            return {"returncode": 1}
        if argv[1] == "running.sh":
            assert cancelled.wait(5)
        return {"returncode": 0}

    monkeypatch.setattr(deploy.bridge, "run_command", command)
    ctx = EnvContext(
        environment=None,
        genestack_root=tmp_path,
        config_dir=None,
        dry_run=False,
        kubeconfig=None,
    )

    def run():
        try:
            result.append(
                deploy._run_services_parallel(
                    {"id": "core"},
                    [
                        {"name": name, "script": name + ".sh"}
                        for name in ["fail", "running", "queued1", "queued2"]
                    ],
                    parallelism=1,
                    ctx=ctx,
                    log=lambda message: None,
                    dry_run=False,
                    timeout=10,
                    deadline=None,
                    extra_env={},
                    ssh_target=None,
                    remote_env={},
                    agent_env_id=None,
                    check_cancel=None,
                    stages_completed=0,
                    stages_total=2,
                    version=None,
                    from_stage=None,
                    item_modes={"waited": 0, "beside": 0},
                )
            )
        except Exception as exc:  # noqa: BLE001 - propagate worker errors to the test owner
            errors.append(exc)
        finally:
            finished.set()

    # A regression must fail within a bound rather than hang the test runner.
    owner = threading.Thread(target=run, daemon=True)
    owner.start()
    try:
        assert finished.wait(5), "Cancellation left the pipeline waiting forever"
        assert not errors
        assert result[0]["failed_at"] == "core/fail"
        assert result[0]["returncode"] == 1
    finally:
        submitted.set()
        cancelled.set()
