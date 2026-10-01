"""Syntax-check console ansible playbooks when ansible-playbook is available."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLAYBOOK_NAMES = (
    "host_preflight.yml",
    "basic_ops.yml",
)


def _find_playbook(console_root: Path, name: str) -> Path | None:
    candidates = [
        console_root / "ansible" / "playbooks" / name,
        console_root / "ansible" / name,
        console_root / "playbooks" / name,
        console_root / name,
        Path("/opt/genestack-console/ansible/playbooks") / name,
        Path("/opt/genestack/ansible/playbooks") / name,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


@pytest.fixture(scope="module")
def ansible_playbook_bin() -> str:
    """Prefer venv ansible-playbook so `make test` works after `make install`."""
    console_root = Path(__file__).resolve().parents[1]
    candidates = [
        console_root / ".venv" / "bin" / "ansible-playbook",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    path = shutil.which("ansible-playbook")
    if not path:
        pytest.skip("ansible-playbook not installed (pip install ansible-core)")
    return path


@pytest.mark.parametrize("playbook_name", PLAYBOOK_NAMES)
def test_playbook_syntax_check(console_root, ansible_playbook_bin, playbook_name):
    """ansible-playbook --syntax-check succeeds for known playbooks."""
    playbook = _find_playbook(console_root, playbook_name)
    if playbook is None:
        pytest.skip(f"{playbook_name} not found under console ansible paths")

    result = subprocess.run(
        [ansible_playbook_bin, "--syntax-check", str(playbook)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(playbook.parent),
    )
    assert result.returncode == 0, (
        f"syntax-check failed for {playbook}:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
