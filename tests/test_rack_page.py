"""The Rack tab is three views of one payload."""


def test_rack_page_is_mounted(client):
    script = client.get("/static/js/pages/environment_rack.js")
    assert script.status_code == 200
    text = script.text
    assert "Beside the rack" in text
    assert "The rack is the machines. The cloud is what those machines are running." in text
    assert "data-rack-mode" in text
    assert 'data-rack-pick="flat"' in text
    assert 'data-rack-pick="space"' in text
    assert 'data-rack-pick="cloud"' in text
    assert "src.links" in text
    assert "row.height" in text
    assert "stroke-dasharray" in text

    detail = client.get("/static/js/pages/environment_detail.js")
    assert detail.status_code == 200
    assert 'data-tab="rack"' in detail.text
