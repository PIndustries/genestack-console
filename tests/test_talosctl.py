"""Pinned talosctl client: resolver, 1.14 apply mode, installer pin."""

from __future__ import annotations

import stat
from pathlib import Path

from app.services import talosctl_install
from app.services.talos import TALOSCTL_VERSION, apply_config_cli_mode, talosctl_bin


def test_installer_pins_the_same_client():
    root = Path(__file__).resolve().parents[1]
    py = (root / "app/services/talos.py").read_text(encoding="utf-8")
    sh = (root / "scripts/genestack-console.sh").read_text(encoding="utf-8")
    assert f'TALOSCTL_VERSION = "{TALOSCTL_VERSION}"' in py
    assert f'TALOSCTL_VERSION="${{GSC_TALOSCTL_VERSION:-{TALOSCTL_VERSION}}}"' in sh
    assert TALOSCTL_VERSION == "v1.14.2"


def test_reboot_mode_is_not_sent_to_talosctl():
    cli, reboot = apply_config_cli_mode("reboot")
    assert cli == "auto"
    assert cli != "reboot"
    assert reboot is True
    cli, reboot = apply_config_cli_mode("no-reboot")
    assert cli == "no-reboot"
    assert reboot is False
    cli, reboot = apply_config_cli_mode("staged")
    assert cli == "staged"
    assert reboot is False


def test_talosctl_bin_prefers_the_console_prefix(tmp_path, monkeypatch):
    bundled = tmp_path / "bin" / "talosctl"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(bundled.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("GSC_PREFIX", str(tmp_path))
    monkeypatch.setattr(
        "app.services.talos.shutil.which", lambda _name: "/usr/local/bin/talosctl-old"
    )
    assert talosctl_bin() == str(bundled)


def test_talosctl_bin_uses_path_when_the_prefix_has_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_PREFIX", str(tmp_path))
    monkeypatch.setattr(
        "app.services.talos.shutil.which", lambda _name: "/usr/bin/talosctl"
    )
    assert talosctl_bin() == "/usr/bin/talosctl"


def test_pinned_version_rejects_a_prerelease(monkeypatch):
    monkeypatch.setenv("GSC_TALOSCTL_VERSION", "v1.15.0-alpha.0")
    assert talosctl_install.pinned_version() == TALOSCTL_VERSION
    monkeypatch.setenv("GSC_TALOSCTL_VERSION", "v1.14.3")
    assert talosctl_install.pinned_version() == "v1.14.3"


def test_file_is_elf_rejects_text(tmp_path):
    path = tmp_path / "not-elf"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    assert talosctl_install.file_is_elf(str(path)) is False
    path.write_bytes(b"\x7fELF" + b"rest")
    assert talosctl_install.file_is_elf(str(path)) is True


def test_ensure_does_not_run_during_tests():
    assert talosctl_install.should_ensure() is False
    talosctl_install.start_ensure()


def test_ensure_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("GSC_SKIP_TALOSCTL", "1")
    assert talosctl_install.should_ensure() is False
