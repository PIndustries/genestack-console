"""The program publish must not wait for a disk or an ISO."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def test_program_publish_does_not_wait_for_the_disk_or_iso():
    doc = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    assert "concurrency" not in doc
    binary = doc["jobs"]["binary"]["concurrency"]
    assert binary["cancel-in-progress"] is False
    group = binary["group"]
    assert "genestack-console-release-program" in group
    assert "genestack-console-release-image-" in group
    assert "inputs.parts == 'disk'" in group
    assert "inputs.parts == 'iso'" in group
    assert "concurrency" not in doc["jobs"]["appliance"]
