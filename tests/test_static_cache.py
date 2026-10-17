"""A reload keeps the scripts from this build."""

from __future__ import annotations

from app.services.static_assets import asset_stamp, pin_module


def test_pin_module_replaces_a_hand_pin():
    source = (
        'import { api } from "../api.js";\n'
        'import map from "./environment_deploy_map.js?v=ls77";\n'
    )
    pinned = pin_module(source, "2026.10.16.3-dev")
    assert 'from "../api.js?v=2026.10.16.3-dev"' in pinned
    assert 'from "./environment_deploy_map.js?v=2026.10.16.3-dev"' in pinned
    assert "ls77" not in pinned


def test_versioned_script_stays_cached(client):
    stamp = asset_stamp()
    page = client.get("/ui")
    assert page.status_code == 200
    assert f"/static/js/app.js?v={stamp}" in page.text
    assert f"/static/css/portal.css?v={stamp}" in page.text
    assert "?v=" + stamp + "?" not in page.text

    fresh = client.get(f"/static/js/app.js?v={stamp}")
    assert fresh.status_code == 200
    assert fresh.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "const __gsc" in fresh.text
    assert 'from "./api.js' not in fresh.text
    assert "environment_detail.js?v=ls77" not in fresh.text

    stored = client.get("/static/js/app.js")
    assert stored.status_code == 200
    assert stored.headers["cache-control"] == "no-cache"
    assert "environment_detail.js?v=ls77" in stored.text

    css = client.get(f"/static/css/portal.css?v={stamp}")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "public, max-age=31536000, immutable"

    detail = client.get(f"/static/js/pages/environment_detail.js?v={stamp}")
    assert detail.status_code == 200
    assert f'from "../api.js?v={stamp}"' in detail.text
    assert f"./environment_deploy_map.js?v={stamp}" in detail.text
    assert "ls77" not in detail.text
