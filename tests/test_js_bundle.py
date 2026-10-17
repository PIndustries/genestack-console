"""The versioned page script is the module graph in one file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.services.js_bundle import bundle_module_count, bundle_source


def test_page_script_is_one_module_graph(tmp_path):
    text = bundle_source()
    assert bundle_module_count() == 51
    assert text.count("__gsc[") >= 51
    assert re.search(r"(?m)^import\s", text) is None
    assert 'from "./' not in text
    assert "import(" not in text
    assert 'Promise.resolve(__gsc["ovh.js"])' in text
    assert "async function api(" in text
    assert "function mountHoneycomb(" in text
    assert "function render(" in text
    path = tmp_path / "app.js"
    path.write_text(text, encoding="utf-8")
    checked = subprocess.run(
        ["node", "--check", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_source_files_remain_on_disk():
    root = Path(__file__).resolve().parents[1] / "app" / "static" / "js"
    source = (root / "app.js").read_text(encoding="utf-8")
    assert 'from "./api.js"' in source
    assert "environment_detail.js?v=ls77" in source
