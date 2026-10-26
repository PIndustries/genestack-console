"""Log fields, operator-facing errors, and paths that must stay in one directory."""

from pathlib import Path

import pytest

from app.services.request_safety import (
    client_detail,
    configured_path,
    log_field,
    under_root,
)


class Shown(Exception):
    def __init__(self, detail: str) -> None:
        self.public_detail = detail
        super().__init__(detail)


def test_log_field_strips_newlines_and_limits_length():
    assert log_field("a\nb\rc", limit=10) == "a b c"
    assert log_field("abcdef", limit=3) == "abc"
    assert log_field(None) == ""


def test_client_detail_hides_unexpected_errors():
    assert client_detail(RuntimeError("disk full\nsecret")) == "The request failed."
    assert client_detail(Shown("chart name is invalid")) == "chart name is invalid"


def test_under_root_keeps_the_name_inside_the_directory(tmp_path: Path):
    path = under_root(tmp_path, "pxe/assets/vmlinuz")
    assert path.is_relative_to(tmp_path.resolve())
    assert path.name == "vmlinuz"


@pytest.mark.parametrize("name", ["../etc/passwd", "/etc/passwd", "", "a\x00b"])
def test_under_root_rejects_an_escaping_name(tmp_path: Path, name: str):
    with pytest.raises(ValueError):
        under_root(tmp_path, name)


def test_under_root_rejects_a_symlink_that_leaves_the_directory(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        under_root(root, "escape/file")


def test_configured_path_resolves_an_operator_path(tmp_path: Path):
    target = tmp_path / "console.db"
    assert configured_path(str(target)) == target.resolve()


def test_remote_path_does_not_resolve_console_symlinks(tmp_path: Path):
    local_target = tmp_path / "local"
    local_target.mkdir()
    alias = tmp_path / "remote-directory"
    alias.symlink_to(local_target)
    assert configured_path(str(alias), remote=True) == alias
    assert configured_path("/etc/genestack", remote=True) == Path("/etc/genestack")


@pytest.mark.parametrize("value", ["~/genestack", "relative/path", "../escape", ""])
def test_remote_path_requires_an_absolute_path(value: str):
    with pytest.raises(ValueError):
        configured_path(value, remote=True)


@pytest.mark.parametrize("value", ["../etc/passwd", "/tmp/../etc/passwd", "a\x00b"])
def test_configured_path_rejects_escape(value: str):
    with pytest.raises(ValueError):
        configured_path(value)
