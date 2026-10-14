"""Values that are safe to log, return to the operator, or use as a path."""

from __future__ import annotations

import os
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
    base = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(base, name))
    prefix = base if base.endswith(os.sep) else base + os.sep
    if not candidate.startswith(prefix):
        raise ValueError("invalid path")
    return Path(candidate)


def configured_path(value: str) -> Path:
    """Resolve an operator-configured path. Reject NUL and ``..`` parts.

    The path stays wherever the operator configured it. This does not force
    it under a second root.
    """
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid path")
    text = value.strip()
    if ".." in Path(text).parts:
        raise ValueError("invalid path")
    return Path(text).expanduser().resolve()
