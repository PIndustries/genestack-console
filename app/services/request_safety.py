"""Values that are safe to log, return to the operator, or use as a path."""

from __future__ import annotations

from pathlib import Path


def log_field(value: object, limit: int = 200) -> str:
    """One log field. Newlines cannot forge a second log line."""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def client_detail(exc: BaseException) -> str:
    """Message for an HTTP error.

    Our own errors set ``public_detail`` to the sentence the page should
    show. Anything else becomes a fixed sentence. The journal gets the
    real exception.
    """
    detail = getattr(exc, "public_detail", None)
    if isinstance(detail, str) and detail.strip():
        return log_field(detail, 300)
    return "The request failed."


def under_root(root: Path, name: str) -> Path:
    """Resolve ``name`` inside ``root``. Reject a name that escapes it."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("invalid path")
    if name.startswith(("/", "\\")) or ".." in Path(name).parts:
        raise ValueError("invalid path")
    base = Path(root).resolve()
    candidate = (base / name).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("invalid path")
    return candidate
