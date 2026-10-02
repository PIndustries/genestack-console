"""One parameter on an operation.

What: the name, whether the caller must send it, and a short description.
Where: app/modules/params.py. Operation files import ``p``.
Why: every operation describes its parameters the same way.
"""

from __future__ import annotations

from typing import Any


def p(
    name: str,
    required: bool = False,
    description: str = "",
    type_: str = "string",
    *,
    default: Any = None,
    enum: list[Any] | None = None,
) -> dict:
    return {
        "name": name,
        "required": required,
        "description": description,
        "type": type_,
        "default": default,
        "enum": enum,
    }
