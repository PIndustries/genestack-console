"""Machines stays responsive while a cluster is not answering."""

from __future__ import annotations


def test_machines_tab_does_not_run_the_overview_poll(client):
    detail = client.get("/static/js/pages/environment_detail.js")
    assert detail.status_code == 200
    text = detail.text
    assert "refreshFleetLine(envId)" in text
    assert "loadDeployMap(envId)" not in text
    assert 'activePtab === "kubernetes"' in text
    assert "environment_servers.js?v=ls73" in text
    assert "environment_next.js?v=ls71" in text
    assert "environment_deploy_map.js?v=ls75" in text
    assert 'nextStripHtml("overview")' in text

    servers = client.get("/static/js/pages/environment_servers.js")
    assert servers.status_code == 200
    body = servers.text
    paint = body.split('msg.textContent = "Loading…"', 1)[1].split("loadReach(envId)", 1)[0]
    assert "tbody.innerHTML" in paint
    before_table = paint.split("tbody.innerHTML", 1)[0]
    assert "/servers`" in before_table or '/servers"' in before_table
    # The wait that gates the table is the inventory read, not the node dial.
    assert "await " in before_table
    first_wait = before_table.split("await ", 1)[1]
    waited = first_wait.split(";", 1)[0]
    if "Promise.all" in waited:
        waited = first_wait.split("]);", 1)[0]
    assert "/servers" in waited
    assert "/k8s/nodes" not in waited
    assert "/platform" not in waited
    assert "/servers/reach" not in before_table

    platform = client.get("/static/js/pages/environment_platform.js")
    assert platform.status_code == 200
    platform_src = platform.text
    card = platform_src.split("export async function loadPlatformCard", 1)[1]
    assert "/servers`" in card or '/servers"' in card
    rows = platform_src.split("function rowsHtml", 1)[1].split("function inspTabState", 1)[0]
    assert "pending" in rows
    assert "checkingCell()" in rows
    assert 'muted">checking</span>' in platform_src.split("function checkingCell", 1)[1].split("function nodeDown", 1)[0]

    deploy = client.get("/static/js/pages/environment_deploy_map.js")
    assert deploy.status_code == 200
    script = deploy.text
    assert "function overviewOpen()" in script
    assert "/api/v1/jobs/" in script
    assert "/api/v1/ops/jobs/" not in script
    assert "No stage yet means the log has not loaded" in script
    assert "export function pauseMapPoll()" in script
    assert "function heavyDue()" in script
    assert "HEAVY_MS = 60000" in script
    assert "export function refreshFleetLine(id)" in script
    tick = script.split("async function tick()", 1)[1].split("let fleetLineGen", 1)[0]
    assert "!overviewOpen()" in tick

    page = client.get("/ui")
    assert page.status_code == 200
    assert "app.js?v=" in page.text
    assert "v=ls69" in page.text

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
    assert "environment_detail.js?v=ls76" in shell.text
    assert "env_wizard.js?v=ls67" in shell.text

    nxt = client.get("/static/js/pages/environment_next.js")
    assert nxt.status_code == 200
    assert "Add the machines." in nxt.text
    assert "Adding a machine does not boot it" in nxt.text
    assert "Install Talos on the machines you added." in nxt.text
    assert "Boot the Talos ISO on each machine yourself." in nxt.text
    assert "This button does not boot it." in nxt.text
    assert "The boot image default is" in nxt.text
    assert "v1.13.9" in nxt.text
    assert "The install disk defaults to /dev/sda." in nxt.text
    assert "When the machine is waiting for a config, use Talos is already installed." in nxt.text
    assert "A machine already running Talos is replaced only after that machine is named." in nxt.text
    assert "Install from the management port" in nxt.text
    assert "The running install will be replaced and the disks are wiped." in nxt.text
    assert "genestack.greenfield" in nxt.text
    assert 'boot: "auto"' in nxt.text
    assert "Record Ubuntu on each machine." in nxt.text
    assert "Select the rows on the Ubuntu tab, then Already have an OS." in nxt.text
    assert "That records the machine. It does not reboot it." in nxt.text
    assert "Deploy over SSH." in nxt.text
    assert "Already have an OS recorded these machines." in nxt.text
    assert "Deploy installs Kubernetes and OpenStack over SSH. It does not reboot them." in nxt.text
    assert "Install the cloud from infrastructure." in nxt.text
    assert "The cloud is installed." in nxt.text
    assert "A machine is not in the cloud yet." in nxt.text
    assert "It still needs an operating system, then the compute stage." in nxt.text
    assert "Adding it did not join it." in nxt.text
    assert "Open rack" in nxt.text
    assert '.tab[data-tab="rack"]' in nxt.text
    assert "This environment only logs." in nxt.text
    assert 'id="env-next-overview"' not in nxt.text or "env-next-" in nxt.text

    wizard = client.get("/static/js/pages/env_wizard.js")
    assert wizard.status_code == 200
    assert "The next action is at the top of Overview and Machines." in wizard.text
