"""Last good Overview and cloud answers stay on disk."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.config import get_settings
from app.services import remembered


def _id() -> str:
    return "env-" + uuid.uuid4().hex[:10]


def test_overview_roundtrip_and_file_mode():
    env_id = _id()
    assert remembered.save(
        "overview",
        env_id,
        {"environment_id": env_id, "platform": {"nodes": [{"name": "n1"}]}},
    )
    loaded = remembered.load("overview", env_id)
    assert loaded is not None
    assert loaded["age_s"] >= 0
    assert loaded["body"]["platform"]["nodes"][0]["name"] == "n1"
    assert "remembered" not in loaded["body"]
    path = Path(get_settings().data_dir) / "remembered" / "overview" / f"{env_id}.json"
    assert (path.stat().st_mode & 0o777) == 0o640
    assert (path.parent.stat().st_mode & 0o777) == 0o750


def test_bad_ids_are_not_written():
    assert remembered.save("overview", "../etc", {"platform": {"nodes": []}}) is False
    assert remembered.save("overview", "a/b", {"platform": {"nodes": []}}) is False
    assert remembered.save("notes", "abc", {"platform": {"nodes": []}}) is False
    assert remembered.load("overview", "../etc") is None


def test_a_failed_overview_does_not_replace_a_good_one():
    env_id = _id()
    assert remembered.save("overview", env_id, {"platform": {"nodes": [{"name": "n1"}]}})
    assert (
        remembered.save(
            "overview",
            env_id,
            {
                "platform": {"ok": False, "error": "unavailable"},
                "cloud": {"ok": False, "error": "missing"},
            },
        )
        is False
    )
    loaded = remembered.load("overview", env_id)
    assert loaded is not None
    assert loaded["body"]["platform"]["nodes"][0]["name"] == "n1"


def test_unavailable_cloud_is_not_saved():
    env_id = _id()
    assert remembered.save("cloud", env_id, {"available": False, "servers": []}) is False
    assert remembered.load("cloud", env_id) is None
    assert remembered.save("cloud", env_id, {"available": True, "servers": [], "cached": True})
    loaded = remembered.load("cloud", env_id)
    assert loaded is not None
    assert loaded["body"]["available"] is True
    assert "cached" not in loaded["body"]


def test_corrupt_file_is_ignored():
    env_id = _id()
    assert remembered.save("overview", env_id, {"platform": {"nodes": [{"name": "n1"}]}})
    path = Path(get_settings().data_dir) / "remembered" / "overview" / f"{env_id}.json"
    path.write_text("not-json", encoding="utf-8")
    assert remembered.load("overview", env_id) is None


def test_cloud_card_refreshes_a_remembered_inventory():
    text = Path("app/static/js/pages/environment_cloud.js").read_text(encoding="utf-8")
    card = text.split("export async function loadCloudCard", 1)[1].split(
        "export function destroyCloudCard", 1
    )[0]
    assert "refreshCloudBehind(activeEnvId, cache)" in card
    assert "await refreshCloudBehind" not in card
