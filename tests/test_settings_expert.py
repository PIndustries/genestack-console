"""Expert stays on the Settings tab, and a Talos row opens the Talos inspector."""

from pathlib import Path


def test_expert_panel_is_inside_settings():
    text = Path("app/static/js/pages/environment_detail.js").read_text(encoding="utf-8")
    assert text.count('id="panel-expert"') == 1
    settings = text.index('id="panel-settings"')
    expert = text.index('id="panel-expert"')
    observe = text.index('id="panel-observe"')
    assert settings < expert < observe
    chunk = text[text.rfind("<div", 0, settings) : text.rfind("<div", 0, observe)]
    assert chunk.count("<div") == chunk.count("</div>")
    scope = text.split("function switchSettingsSubtab", 1)[1].split("function switchTab", 1)[0]
    assert 'getElementById("panel-settings")' in scope
    assert 'querySelectorAll("[data-spanel]")' in scope
    assert 'document.querySelectorAll("[data-spanel]")' not in scope
    assert "onTalos: (name) => openTalosMachine(name)" in text
    assert "closeTalosMachine()" in text


def test_ready_node_stays_ready_while_work_runs():
    text = Path("app/static/js/pages/environment_deploy_map.js").read_text(encoding="utf-8")
    chip = text.split('} else if (n.kind === "k8s") {', 1)[1].split("} else if", 1)[0]
    assert 'st.k8sReady ? "Ready" : "not Ready"' in chip
    assert 'n.state === "ok" ? "Ready"' not in chip


def test_overview_map_fills_the_viewport():
    css = Path("app/static/css/portal.css").read_text(encoding="utf-8")
    rule = css.split(
        "#page-root:has(#panel-workflow.tab-panel.active) .sf-shell {", 1
    )[1].split("}", 1)[0]
    assert "height: auto" not in rule
    assert "min-height: 12rem" not in rule
    assert "flex: 1 1 0" in rule
    assert "height: 100%" in rule
    assert "min-height: 0" in rule
    page = css.split("#page-root:has(#panel-workflow.tab-panel.active) {", 1)[1].split(
        "}", 1
    )[0]
    assert "overflow: hidden" in page


def test_talos_row_opens_the_inspector():
    servers = Path("app/static/js/pages/environment_servers.js").read_text(encoding="utf-8")
    hook = servers.split("export function wireServersCard", 1)[1].split("document.getElementById(\"srv-refresh\")", 1)[0]
    assert "hooks.onTalos" in hook
    assert 'row.os === "talos"' in hook
    platform = Path("app/static/js/pages/environment_platform.js").read_text(encoding="utf-8")
    assert "export function openTalosMachine" in platform
    assert "document.body.appendChild(modal)" in platform
    assert "export function closeTalosMachine" in platform
