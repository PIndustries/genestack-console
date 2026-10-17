"""Pack the console page's modules into the one script the browser fetches.

``app.js`` imports every screen, and each screen imports its cards. The
browser loads that graph before the page runs. The versioned ``app.js``
response is those files in one script, with the imports already bound.
The source files stay separate. An unversioned URL still returns the
original module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_JS_ROOT = Path(__file__).resolve().parents[1] / "static" / "js"
_ENTRY = "app.js"
_IMPORT_STMT = re.compile(
    r"""^[ \t]*import\s+(?P<body>[\s\S]*?)\s+from\s*"""
    r"""(?P<q>["'])(?P<spec>[^"']+)(?P=q)\s*;?[ \t]*\n?""",
    re.M,
)
_EXPORT_LINE = re.compile(
    r"^(?P<indent>[ \t]*)export\s+"
    r"(?P<rest>(?:async\s+function|function|class|const|let)\s+"
    r"[A-Za-z_$][\w$]*)",
    re.M,
)
_DYNAMIC_IMPORT = re.compile(
    r"""import\(\s*(?P<q>["'])(?P<spec>\.\.?/[^"']+)(?P=q)\s*\)"""
)
_NAMESPACE = re.compile(r"^\*\s+as\s+([A-Za-z_$][\w$]*)$")
_cache: tuple[tuple[int, ...], str] | None = None


def _resolve(module: Path, spec: str) -> Path:
    """A relative import, kept inside the script directory."""
    raw = spec.split("?", 1)[0]
    target = (module.parent / raw).resolve()
    if target.suffix != ".js":
        target = target.with_suffix(".js")
    root = _JS_ROOT.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"import leaves the script directory: {spec}") from exc
    if not target.is_file():
        raise ValueError(f"missing import {spec} from {module.name}")
    return target


def _key(path: Path) -> str:
    return path.resolve().relative_to(_JS_ROOT.resolve()).as_posix()


def _parse_binding(body: str) -> tuple[str | None, list[tuple[str, str]]]:
    text = " ".join(body.split())
    namespace = _NAMESPACE.fullmatch(text)
    if namespace:
        return namespace.group(1), []
    if not (text.startswith("{") and text.endswith("}")):
        raise ValueError(f"unsupported import: {text}")
    names: list[tuple[str, str]] = []
    for part in text[1:-1].split(","):
        item = part.strip()
        if not item:
            continue
        if " as " in item:
            exported, local = (piece.strip() for piece in item.split(" as ", 1))
        else:
            exported = local = item
        names.append((exported, local))
    return None, names


def _prelude(module: Path, imports: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for body, spec in imports:
        key = json.dumps(_key(_resolve(module, spec)))
        namespace, names = _parse_binding(body)
        if namespace:
            lines.append(f"const {namespace} = __gsc[{key}];")
            continue
        fields = []
        for exported, local in names:
            if exported == local:
                fields.append(exported)
            else:
                fields.append(f"{exported}: {local}")
        lines.append(f"const {{ {', '.join(fields)} }} = __gsc[{key}];")
    return "\n".join(lines)


def _transform(module: Path, source: str) -> tuple[str, str, list[str]]:
    imports: list[tuple[str, str]] = []

    def take_import(match: re.Match[str]) -> str:
        imports.append((match.group("body"), match.group("spec")))
        return ""

    without_imports = _IMPORT_STMT.sub(take_import, source)
    names: list[str] = []

    def take_export(match: re.Match[str]) -> str:
        names.append(match.group("rest").split()[-1])
        return f"{match.group('indent')}{match.group('rest')}"

    body = _EXPORT_LINE.sub(take_export, without_imports)
    leftover = [
        line.strip()
        for line in body.splitlines()
        if line.startswith("export ") or line.startswith("import ")
    ]
    if leftover:
        raise ValueError(f"{_key(module)} has an unparsed module line: {leftover[0]}")

    def bind_dynamic(match: re.Match[str]) -> str:
        key = json.dumps(_key(_resolve(module, match.group("spec"))))
        return f"Promise.resolve(__gsc[{key}])"

    body = _DYNAMIC_IMPORT.sub(bind_dynamic, body)
    return _prelude(module, imports), body, names


def _order(entry: Path) -> list[Path]:
    seen: list[Path] = []
    known: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in known:
            return
        known.add(resolved)
        text = resolved.read_text(encoding="utf-8")
        for match in _IMPORT_STMT.finditer(text):
            visit(_resolve(resolved, match.group("spec")))
        seen.append(resolved)

    visit(entry)
    return seen


def _emit(path: Path) -> str:
    prelude, body, names = _transform(path, path.read_text(encoding="utf-8"))
    key = json.dumps(_key(path))
    assigns = "\n".join(f"  __exports.{name} = {name};" for name in names)
    parts = [
        f"__gsc[{key}] = (() => {{",
        "  const __exports = {};",
    ]
    if prelude:
        parts.append(prelude)
    parts.append(body.rstrip())
    if assigns:
        parts.append(assigns)
    parts.append("  return __exports;")
    parts.append("})();")
    return "\n".join(parts)


def bundle_source() -> str:
    """The page script. Dependencies run before the file that imports them."""
    global _cache
    entry = (_JS_ROOT / _ENTRY).resolve()
    files = _order(entry)
    token = tuple(path.stat().st_mtime_ns for path in files)
    if _cache is not None and _cache[0] == token:
        return _cache[1]
    chunks = ["\"use strict\";", "const __gsc = {};"]
    chunks.extend(_emit(path) for path in files)
    text = "\n".join(chunks) + "\n"
    _cache = (token, text)
    return text


def bundle_module_count() -> int:
    return len(_order((_JS_ROOT / _ENTRY).resolve()))
