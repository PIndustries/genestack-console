"""The class file for one area.

What: a module is a folder. This class lists the Python files in that folder.
Where: app/modules/base.py. Each area's __init__.py subclasses Module.
Why: adding an operation is a new file plus one name in ``functions``. The class
does not call the next file. The console calls the one file that matches the job.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType


class Module:
    """List of function files for one area, in order."""

    name = ""
    functions: tuple[str, ...] = ()

    def function_modules(self) -> Iterator[ModuleType]:
        package = type(self).__module__
        for item in self.functions:
            dotted = item if "." in item else f"{package}.{item}"
            yield importlib.import_module(dotted)
