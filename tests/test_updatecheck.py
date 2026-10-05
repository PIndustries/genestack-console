from types import SimpleNamespace

from app.services import updatecheck
from app.services.updatecheck import is_newer, notes_from_body, parse_calver


def test_calver_newer():
    assert is_newer("2026.09.01", "2026.08.06")
    assert not is_newer("2026.08.06", "2026.08.06")
    assert not is_newer("2026.07.01", "2026.08.06")
    assert is_newer("2026.08.06", "2026.08.06-rc1")
    assert parse_calver("2026.9.1")[1] == 9


def test_calver_build_number():
    assert is_newer("2026.10.04.3", "2026.10.04.2")
    assert not is_newer("2026.10.04.2", "2026.10.04.3")
    assert is_newer("2026.10.04.1", "2026.10.04.1-rc1")
    assert is_newer("2026.10.05.1", "2026.10.04.9")
    assert not is_newer("2026.10.04.3", "2026.10.04.3")
    assert is_newer("2026.10.04.1", "2026.10.03")
    assert not is_newer("v2026.10.04.3", "2026.10.04.3")
    assert parse_calver("2026.10.04.3")[3] == 3
    assert parse_calver("2026.10.03")[3] == 0
    assert parse_calver("2026.08.06-rc1")[4] == 0
    assert parse_calver("2026.08.06")[4] == 1


def test_notes_from_body_bullets_are_the_changes():
    notes = notes_from_body(
        "Machines is one page, and the image cache is on Overview.\n"
        "- Talos and Ubuntu are tabs on Machines.\n"
        "- Image cache sits above the map.\n"
    )
    assert notes["summary"].startswith("Machines is one page")
    assert notes["items"] == [
        "Talos and Ubuntu are tabs on Machines.",
        "Image cache sits above the map.",
    ]


def test_source_checkout_does_not_apply():
    assert updatecheck.can_apply_here() is False


def test_watch_loop_returns_when_stopped():
    import asyncio

    stop = asyncio.Event()
    stop.set()
    asyncio.run(asyncio.wait_for(updatecheck.watch_loop(stop, lambda: None), 2))


def test_watch_skips_while_jobs_run():
    calls = []
    result = updatecheck.watch_once(
        SimpleNamespace(update_watch=True),
        info={"update_available": True, "latest": "2026.10.05.6"},
        installed=True,
        busy=lambda: 2,
        apply=lambda settings: calls.append(settings),
    )
    assert result["skipped"] == "jobs"
    assert result["active_jobs"] == 2
    assert calls == []


def test_watch_applies_when_idle_without_the_timer_flag():
    result = updatecheck.watch_once(
        SimpleNamespace(update_watch=True),
        info={"update_available": True, "latest": "2026.10.05.6"},
        installed=True,
        busy=lambda: 0,
        apply=lambda settings: {"applied": True, "ok": True, "message": "replaced"},
    )
    assert result["applied"] is True


def test_watch_skips_when_disabled_or_not_installed():
    info = {"update_available": True, "latest": "2026.10.05.6"}
    off = updatecheck.watch_once(
        SimpleNamespace(update_watch=False),
        info=info,
        installed=True,
        busy=lambda: 0,
        apply=lambda settings: {"applied": True},
    )
    assert off["skipped"] == "watch off"
    checkout = updatecheck.watch_once(
        SimpleNamespace(update_watch=True),
        info=info,
        installed=False,
        busy=lambda: 0,
        apply=lambda settings: {"applied": True},
    )
    assert checkout["skipped"] == "not installed"


def test_watch_holds_after_a_non_linux_binary():
    updatecheck.clear_caches()
    calls = {"n": 0}

    def apply(settings):
        calls["n"] += 1
        return {"applied": False, "message": "download is not a Linux ELF"}

    info = {"update_available": True, "latest": "2026.10.05.6"}
    settings = SimpleNamespace(update_watch=True)
    first = updatecheck.watch_once(
        settings, info=info, installed=True, busy=lambda: 0, apply=apply
    )
    second = updatecheck.watch_once(
        settings, info=info, installed=True, busy=lambda: 0, apply=apply
    )
    assert "Linux ELF" in first["message"]
    assert second["skipped"] == "held"
    assert calls["n"] == 1
    updatecheck.clear_caches()


def test_feed_uses_release_notes_and_drops_other_urls(monkeypatch):
    updatecheck.clear_caches()

    def fake(url):
        if "/releases?" in url:
            return [
                {
                    "tag_name": "v2026.10.05.5",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-10-05T16:00:00Z",
                    "html_url": (
                        "https://github.com/PIndustries/genestack-console/"
                        "releases/tag/v2026.10.05.5"
                    ),
                    "body": "Machines is one page.\n- Talos and Ubuntu are tabs.\n",
                }
            ]
        if "/actions/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 7,
                        "name": "release",
                        "display_title": "v2026.10.05.5",
                        "status": "completed",
                        "conclusion": "success",
                        "event": "push",
                        "head_sha": "8da63652f55bd666",
                        "head_branch": "v2026.10.05.5",
                        "html_url": (
                            "https://github.com/PIndustries/genestack-console/actions/runs/7"
                        ),
                        "run_started_at": "2026-10-05T15:52:21Z",
                    },
                    {
                        "id": 8,
                        "name": "ci",
                        "display_title": "fix the map",
                        "status": "completed",
                        "conclusion": "success",
                        "event": "push",
                        "head_sha": "abcdef1234567890",
                        "head_branch": "main",
                        "html_url": "https://evil.example/runs/8",
                        "run_started_at": "2026-10-05T15:00:00Z",
                    },
                ]
            }
        if url.endswith("/jobs?per_page=30"):
            return {
                "jobs": [
                    {
                        "name": "binary",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": (
                            "https://github.com/PIndustries/genestack-console/"
                            "actions/runs/7/job/1"
                        ),
                    }
                ]
            }
        return None

    monkeypatch.setattr(updatecheck, "fetch_github", fake)
    monkeypatch.setattr(
        updatecheck,
        "status",
        lambda settings: {"current": "2026.10.05.5", "watch": True},
    )
    data = updatecheck.feed(SimpleNamespace())
    assert data["releases"][0]["items"] == ["Talos and Ubuntu are tabs."]
    assert data["pipeline"][0]["name"] == "release"
    assert data["pipeline"][0]["jobs"][0]["name"] == "binary"
    assert data["pipeline"][0]["sha"] == "8da6365"
    assert data["pipeline"][1]["html_url"] is None
    assert data["github_reachable"] is True
    updatecheck.clear_caches()


def test_active_jobs_counts_a_queued_row():
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    before = updatecheck.active_jobs()
    db = SessionLocal()
    job = Job(operation="test.updatecheck", status=JobStatus.queued, log_text="")
    db.add(job)
    db.commit()
    try:
        assert updatecheck.active_jobs() == before + 1
    finally:
        db.delete(job)
        db.commit()
        db.close()


def test_update_feed_route(client, viewer_headers, monkeypatch):
    monkeypatch.setattr(
        "app.routers.update.updatecheck.feed",
        lambda settings: {
            "current": "2026.10.05.5",
            "watch": True,
            "releases": [
                {"version": "2026.10.05.5", "items": ["Talos and Ubuntu are tabs."]}
            ],
            "pipeline": [{"name": "release", "conclusion": "success"}],
            "github_reachable": True,
        },
    )
    denied = client.get("/api/v1/update/feed")
    assert denied.status_code == 401
    res = client.get("/api/v1/update/feed", headers=viewer_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["releases"][0]["items"][0].startswith("Talos")
    assert body["pipeline"][0]["name"] == "release"
