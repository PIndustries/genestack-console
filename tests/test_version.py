"""Console version surface — CalVer format, /health payload, bump script."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app import __build__, __version__
from app.version import BUILD, VERSION

CONSOLE_ROOT = Path(__file__).resolve().parents[1]

CALVER_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(-[0-9A-Za-z.-]+)?$")


def test_version_is_calver():
    """VERSION is CalVer: YYYY.MM.DD (optional -suffix), and a real date."""
    assert CALVER_RE.match(VERSION), f"VERSION {VERSION!r} is not YYYY.MM.DD[-suffix]"
    date_part = VERSION.split("-", 1)[0]
    datetime.strptime(date_part, "%Y.%m.%d")  # raises on impossible dates


def test_package_version_matches():
    """app.__version__/__build__ are wired to app.version."""
    assert __version__ == VERSION
    assert __build__ == BUILD


def test_health_carries_version_and_build(client):
    """GET /health reports the CalVer version and the build hash."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == VERSION
    assert data["build"] == BUILD


def _run_bump(version_file: Path, *args: str) -> None:
    script = CONSOLE_ROOT / "scripts" / "bump-version.sh"
    subprocess.run(
        ["bash", str(script), *args],
        env={"GSC_VERSION_FILE": str(version_file), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )


def test_bump_version_writes_today(tmp_path):
    """bump-version.sh rewrites VERSION to today's UTC date."""
    version_file = tmp_path / "version.py"
    version_file.write_text('VERSION = "1999.01.01"\n', encoding="utf-8")
    _run_bump(version_file)
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    assert f'VERSION = "{today}"' in version_file.read_text(encoding="utf-8")


def test_bump_version_with_suffix(tmp_path):
    """bump-version.sh rc1 appends the suffix with a dash."""
    version_file = tmp_path / "version.py"
    version_file.write_text('VERSION = "1999.01.01"\n', encoding="utf-8")
    _run_bump(version_file, "rc1")
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    assert f'VERSION = "{today}-rc1"' in version_file.read_text(encoding="utf-8")
