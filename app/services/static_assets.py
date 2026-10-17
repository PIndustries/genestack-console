"""Keep one build's scripts in the browser.

The page's modules are served as one script. A URL that names this build
can stay cached, so opening the console again does not download it a second
time. The shell itself stays revalidated, so the next build points at a new
URL.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.version import BUILD, VERSION

_STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
_RELATIVE_IMPORT = re.compile(
    r"""(?P<lead>\bfrom\s+|import\s*(?:\(\s*)?)(?P<q>["'])(?P<body>\.{1,2}/[^"']+)(?P=q)"""
)
_HTML_ASSET = re.compile(r"(?P<path>/static/[^\"'\s?#]+)(?:\?[^\"'\s]*)?")
_rewritten: dict[tuple[str, str], bytes] = {}


def asset_stamp() -> str:
    return f"{VERSION}-{BUILD}"


def pin_module(source: str, stamp: str | None = None) -> str:
    """Point relative imports at this build. An older hand pin is replaced."""
    version = stamp or asset_stamp()

    def repl(match: re.Match[str]) -> str:
        path = match.group("body").split("?", 1)[0]
        return f"{match.group('lead')}{match.group('q')}{path}?v={version}{match.group('q')}"

    return _RELATIVE_IMPORT.sub(repl, source)


def stamp_html(html: str, stamp: str | None = None) -> str:
    """One query on each /static URL. A second ?v= is not appended."""
    version = stamp or asset_stamp()

    def repl(match: re.Match[str]) -> str:
        return f"{match.group('path')}?v={version}"

    return _HTML_ASSET.sub(repl, html)


def static_file(url_path: str) -> Path | None:
    """Resolve a /static URL to a file under the static root."""
    if not url_path.startswith("/static/"):
        return None
    rel = url_path.removeprefix("/static/").lstrip("/")
    if not rel or "\\" in rel or ".." in rel.split("/"):
        return None
    root = _STATIC_ROOT.resolve()
    file = (root / rel).resolve()
    try:
        file.relative_to(root)
    except ValueError:
        return None
    if not file.is_file():
        return None
    return file


def rewritten_module(url_path: str, stamp: str | None = None) -> bytes | None:
    file = static_file(url_path)
    if file is None or file.suffix != ".js":
        return None
    version = stamp or asset_stamp()
    key = (str(file), version)
    cached = _rewritten.get(key)
    if cached is not None:
        return cached
    if file.name == "app.js" and file.parent.name == "js":
        from app.services.js_bundle import bundle_source

        data = bundle_source().encode("utf-8")
    else:
        text = file.read_text(encoding="utf-8")
        data = pin_module(text, version).encode("utf-8")
    _rewritten[key] = data
    return data
