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


def test_talos_row_opens_the_inspector():
    servers = Path("app/static/js/pages/environment_servers.js").read_text(encoding="utf-8")
    hook = servers.split("export function wireServersCard", 1)[1].split("document.getElementById(\"srv-refresh\")", 1)[0]
    assert "hooks.onTalos" in hook
    assert 'row.os === "talos"' in hook
    platform = Path("app/static/js/pages/environment_platform.js").read_text(encoding="utf-8")
    assert "export function openTalosMachine" in platform
    assert "document.body.appendChild(modal)" in platform
    assert "export function closeTalosMachine" in platform
