"""Load built-in operations and any module folder the operator adds.

What: the list of operations the console can run.
Where: app/modules/__init__.py. Built-in areas are the folders next to this file.
Extra folders come from ``modules.paths`` in config.yaml, or from the
``genestack_console.modules`` entry point.
Why: a new operation is a file in a folder. It does not have to edit the job runner.

The class file in each folder lists function files. It does not call them.
The job runner calls the one function whose HANDLERS name matches the job.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from app.modules.base import Module

# Same order the job runner used to ask each area. Catalog order is separate
# and lives in app/modules/order.py.
_BUILTINS = (
    "app.modules.console",
    "app.modules.openstack",
    "app.modules.ansible",
    "app.modules.genestack",
    "app.modules.hostvm",
    "app.modules.baremetal",
    "app.modules.agents",
    "app.modules.ovh",
    "app.modules.apps",
    "app.modules.hardware",
    "app.modules.platform",
    "app.modules.hosts",
)

_ENTRY_GROUP = "genestack_console.modules"


class ModuleLoadError(ValueError):
    """A module folder or entry point could not be loaded."""


class DuplicateModuleError(ModuleLoadError):
    """Two modules claim the same name, handler, or operation id."""


@dataclass
class _Loaded:
    modules: list[Module]
    handlers: dict[str, Callable]
    operations: list[dict]


_CACHE: dict[tuple[str, ...], _Loaded] = {}


def clear_module_cache() -> None:
    """Drop cached loads. Tests use this after writing a temporary module."""
    _CACHE.clear()
    for name in list(sys.modules):
        if name.startswith("genestack_console_extra_"):
            del sys.modules[name]


def _instance_from_module(module: ModuleType) -> Module:
    found: list[type[Module]] = []
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, Module)
            and obj is not Module
            and obj.__module__ == module.__name__
        ):
            found.append(obj)
    if len(found) != 1:
        names = [cls.__name__ for cls in found] or ["none"]
        raise ModuleLoadError(
            f"{module.__name__} must define one Module subclass, found {', '.join(names)}"
        )
    return found[0]()


def _load_directory(path: Path) -> Module:
    path = path.resolve()
    init = path / "__init__.py"
    if not init.is_file():
        raise ModuleLoadError(f"{path} has no __init__.py")
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    mod_name = f"genestack_console_extra_{digest}"
    existing = sys.modules.get(mod_name)
    if existing is not None:
        return _instance_from_module(existing)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        init,
        submodule_search_locations=[str(path)],
    )
    if spec is None or spec.loader is None:
        raise ModuleLoadError(f"cannot load module at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return _instance_from_module(module)


def _entry_point_modules() -> list[Module]:
    from importlib.metadata import entry_points

    found = entry_points(group=_ENTRY_GROUP)
    loaded: list[Module] = []
    for ep in found:
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise ModuleLoadError(f"entry point {ep.name} failed to load: {exc}") from exc
        if isinstance(obj, type) and issubclass(obj, Module) and obj is not Module:
            loaded.append(obj())
            continue
        raise ModuleLoadError(f"entry point {ep.name} must be a Module subclass")
    return loaded


def _operations_of(fn: ModuleType) -> list[dict]:
    many = getattr(fn, "OPERATIONS", None)
    if many is not None:
        return list(many)
    one = getattr(fn, "OPERATION", None)
    if one is None:
        return []
    return [one]


def _index(modules: list[Module]) -> _Loaded:
    handlers: dict[str, Callable] = {}
    handler_home: dict[str, str] = {}
    op_ids: dict[str, str] = {}
    names: dict[str, str] = {}
    operations: list[dict] = []
    for mod in modules:
        home = type(mod).__module__
        if not mod.name:
            raise ModuleLoadError(f"{home} has no module name")
        if mod.name in names:
            raise DuplicateModuleError(
                f"module name {mod.name!r} is already used by {names[mod.name]}"
            )
        names[mod.name] = home
        if not mod.functions:
            raise ModuleLoadError(f"{home} lists no function files")
        for fn in mod.function_modules():
            owned = tuple(getattr(fn, "HANDLERS", ()) or ())
            if not owned:
                raise ModuleLoadError(f"{fn.__name__} has no HANDLERS")
            run = getattr(fn, "run", None)
            if not callable(run):
                raise ModuleLoadError(f"{fn.__name__} has no run function")
            for handler in owned:
                if handler in handler_home:
                    raise DuplicateModuleError(
                        f"handler {handler!r} is already defined in {handler_home[handler]}"
                    )
                handler_home[handler] = fn.__name__
                handlers[handler] = run
            for op in _operations_of(fn):
                oid = op.get("id")
                hid = op.get("handler")
                if not oid:
                    raise ModuleLoadError(f"{fn.__name__} has an operation with no id")
                if hid not in owned:
                    raise ModuleLoadError(
                        f"{fn.__name__} operation {oid!r} uses handler {hid!r}, "
                        "which is not in HANDLERS"
                    )
                if oid in op_ids:
                    raise DuplicateModuleError(
                        f"operation id {oid!r} is already defined in {op_ids[oid]}"
                    )
                op_ids[oid] = fn.__name__
                operations.append(op)
    return _Loaded(modules=modules, handlers=handlers, operations=operations)


def _normalize(paths: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        out.append(str(path))
    return tuple(out)


def load_modules(paths: Sequence[str] | None = None) -> list[Module]:
    """Built-in modules, then each path, then installed entry points.

    ``paths`` defaults to ``modules.paths`` from config.yaml. The result is
    cached by that path list. A duplicate handler or operation id raises.
    """
    if paths is None:
        from app.config import get_settings

        paths = list(get_settings().module_paths)
    key = _normalize(paths)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached.modules
    modules = [_instance_from_module(importlib.import_module(name)) for name in _BUILTINS]
    for folder in key:
        modules.append(_load_directory(Path(folder)))
    modules.extend(_entry_point_modules())
    loaded = _index(modules)
    _CACHE[key] = loaded
    return loaded.modules


def handler_map(paths: Sequence[str] | None = None) -> dict[str, Callable]:
    """Handler name to the function that runs it. The function takes the job runner as self."""
    load_modules(paths)
    key = _cache_key(paths)
    return dict(_CACHE[key].handlers)


def iter_operations(paths: Sequence[str] | None = None) -> list[dict]:
    """Operation dicts in module order. The catalog sorts built-in ids after this."""
    load_modules(paths)
    key = _cache_key(paths)
    return list(_CACHE[key].operations)


def _cache_key(paths: Sequence[str] | None) -> tuple[str, ...]:
    if paths is None:
        from app.config import get_settings

        paths = list(get_settings().module_paths)
    return _normalize(paths)
