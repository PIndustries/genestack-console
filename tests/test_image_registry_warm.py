"""Tests for image_registry.warm_for_deploy — priming pull-through caches.

``warm_for_deploy`` shells out via ``_warm_one`` (httpx) and ``_cluster_images``
(kubectl) on the live path, so those are monkeypatched to keep the tests pure
and network-free. ``settings`` is accepted but unused by the function today.
"""

from __future__ import annotations

from app.services import image_registry
from app.services.image_registry import (
    BOOTSTRAP_WARM_IMAGES,
    warm_for_deploy,
)


def _doc():
    return {"pxe": {"next_server": "10.200.0.50", "http_port": 8088}}


def test_warm_for_deploy_dry_run_counts_and_logs(monkeypatch):
    # No warm_one calls should happen on dry-run.
    monkeypatch.setattr(
        image_registry, "_warm_one",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("warm_one called")),
    )
    logs: list[str] = []
    result = warm_for_deploy(_doc(), None, None, logs.append, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["cached"] == len(BOOTSTRAP_WARM_IMAGES)
    assert any("would cache" in line for line in logs)
    # bind is echoed in the log line; the dry-run result has no bind key.
    assert any("10.200.0.50" in line for line in logs)
    assert "bind" not in result


def test_warm_for_deploy_success_primes_all(monkeypatch):
    warmed: list[tuple] = []

    def fake_warm_one(bind, registry, repo, tag, port):
        warmed.append((bind, registry, repo, tag, port))
        return True, f"{registry}/{repo}:{tag} cached (1 parts)"

    monkeypatch.setattr(image_registry, "_warm_one", fake_warm_one)
    monkeypatch.setattr(image_registry, "_cluster_images", lambda kc: [])
    logs: list[str] = []
    result = warm_for_deploy(_doc(), "/path/admin.conf", None, logs.append,
                             dry_run=False)
    assert result["ok"] is True
    assert result["cached"] == len(BOOTSTRAP_WARM_IMAGES)
    assert result["failed"] == 0
    assert result["bind"] == "10.200.0.50"
    # Every bootstrap image was warmed exactly once.
    assert len(warmed) == len(BOOTSTRAP_WARM_IMAGES)
    # All warm calls target the doc bind host.
    assert all(bind == "10.200.0.50" for bind, *_ in warmed)


def test_warm_for_deploy_includes_live_cluster_images(monkeypatch):
    live = ["quay.io/airshipit/heat-api:1.0"]
    warmed: list[tuple] = []

    def fake_warm_one(bind, registry, repo, tag, port):
        warmed.append((registry, repo, tag))
        return True, "cached"

    monkeypatch.setattr(image_registry, "_warm_one", fake_warm_one)
    monkeypatch.setattr(image_registry, "_cluster_images", lambda kc: list(live))
    result = warm_for_deploy(_doc(), "/path/admin.conf", None,
                             lambda _m: None, dry_run=False)
    # bootstrap + one live image that is not already in the bootstrap set.
    assert result["cached"] == len(BOOTSTRAP_WARM_IMAGES) + 1
    warmed_set = {(r, repo, t) for (r, repo, t) in warmed}
    assert ("quay.io", "airshipit/heat-api", "1.0") in warmed_set


def test_warm_for_deploy_dedupes_live_image_already_in_bootstrap(monkeypatch):
    # A live image identical to a bootstrap entry must not be warmed twice.
    first = BOOTSTRAP_WARM_IMAGES[0]
    calls = {"n": 0}

    def fake_warm_one(bind, registry, repo, tag, port):
        calls["n"] += 1
        return True, "cached"

    monkeypatch.setattr(image_registry, "_warm_one", fake_warm_one)
    monkeypatch.setattr(image_registry, "_cluster_images", lambda kc: [first])
    result = warm_for_deploy(_doc(), "/kc", None, lambda _m: None, dry_run=False)
    assert result["cached"] == len(BOOTSTRAP_WARM_IMAGES)
    assert calls["n"] == len(BOOTSTRAP_WARM_IMAGES)


def test_warm_for_deploy_failure_reports_not_ok(monkeypatch):
    def fake_warm_one(bind, registry, repo, tag, port):
        return False, f"{registry}/{repo}:{tag} Error: boom"

    monkeypatch.setattr(image_registry, "_warm_one", fake_warm_one)
    monkeypatch.setattr(image_registry, "_cluster_images", lambda kc: [])
    logs: list[str] = []
    result = warm_for_deploy(_doc(), None, None, logs.append, dry_run=False)
    assert result["ok"] is False
    assert result["cached"] == 0
    assert result["failed"] == len(BOOTSTRAP_WARM_IMAGES)
    assert any("boom" in line for line in logs)


def test_warm_for_deploy_skips_unknown_upstream(monkeypatch):
    # An image whose registry has no pull-through upstream is skipped, not an
    # error. Monkeypatch _upstream_for to return None for one registry.
    real = image_registry._upstream_for
    calls = {"n": 0}

    def selective_upstream(registry):
        if registry == "docker.io":
            return None
        return real(registry)

    def fake_warm_one(bind, registry, repo, tag, port):
        calls["n"] += 1
        return True, "cached"

    monkeypatch.setattr(image_registry, "_upstream_for", selective_upstream)
    monkeypatch.setattr(image_registry, "_warm_one", fake_warm_one)
    monkeypatch.setattr(image_registry, "_cluster_images", lambda kc: [])
    result = warm_for_deploy(_doc(), None, None, lambda _m: None, dry_run=False)
    assert result["ok"] is True
    # cached count excludes the docker.io images (mariadb-operator/rabbitmq
    # live on docker.io / ghcr.io), so it is strictly less than the total set.
    assert result["cached"] < len(BOOTSTRAP_WARM_IMAGES)
    assert calls["n"] == result["cached"]
