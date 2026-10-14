"""Log fields, operator-facing errors, and paths that must stay in one directory."""

from pathlib import Path

import pytest

from app.services.request_safety import client_detail, log_field, under_root


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
