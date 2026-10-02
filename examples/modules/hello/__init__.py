"""A sample module. The console does not load this folder unless you ask.

What: one extra operation, hello.say, that returns a greeting.
Where: examples/modules/hello/. HelloModule lists say.py.
Why: this is the same shape as a built-in module, so a new module can copy it.

Put this folder's path in config.yaml:

    modules:
      paths:
        - examples/modules/hello

A path in that file is relative to the config file's directory.
"""

from __future__ import annotations

from app.modules.base import Module


class HelloModule(Module):
    name = "hello"
    functions = ("say",)
