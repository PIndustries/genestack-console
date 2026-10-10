"""Machines stays responsive while a cluster is not answering."""

from __future__ import annotations


def test_machines_tab_does_not_run_the_overview_poll(client):
    detail = client.get("/static/js/pages/environment_detail.js")
    assert detail.status_code == 200
    text = detail.text
    assert "refreshFleetLine(envId)" in text
    assert "loadDeployMap(envId)" not in text
    assert 'activePtab === "kubernetes"' in text
    assert "environment_servers.js?v=ls65" in text
    assert "environment_deploy_map.js?v=ls64" in text

    servers = client.get("/static/js/pages/environment_servers.js")
    assert servers.status_code == 200
    body = servers.text
    paint = body.split('msg.textContent = "Loading…"', 1)[1].split("loadReach(envId)", 1)[0]
    waited = paint.split("await Promise.all", 1)[1].split("]);", 1)[0]
    assert "/servers`" in waited or "/servers\"" in waited or "/servers`" in paint
    assert "/k8s/nodes" not in waited

    deploy = client.get("/static/js/pages/environment_deploy_map.js")
    assert deploy.status_code == 200
    script = deploy.text
    assert "function overviewOpen()" in script
    assert "export function pauseMapPoll()" in script
    assert "export function refreshFleetLine(id)" in script
    tick = script.split("async function tick()", 1)[1].split("let fleetLineGen", 1)[0]
    assert "!overviewOpen()" in tick

    page = client.get("/ui")
    assert page.status_code == 200
    assert "app.js?v=" in page.text
    assert "v=ls65" in page.text

    assert "The console took too long to answer" in body
    assert "No answer" in body
    assert "The saved Talos identity does not match these machines." in body
    assert "Continue from here" in body
    assert "kubeconfig this console can read" in body
    assert "Ready control plane" in body
    assert "iscsi-tools and util-linux-tools" in body
    assert "loadServersCard(envId)" in body.split("isTimeout", 1)[1]

    api_js = client.get("/static/js/api.js")
    assert api_js.status_code == 200
    assert "The console took too long to answer." in api_js.text
    assert "Check your connection and try again." not in api_js.text.split("AbortError", 1)[1].split("TypeError", 1)[0]

    shell = client.get("/static/js/app.js")
    assert "healthMisses" in shell.text
    assert "health: slow" in shell.text
