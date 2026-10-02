"""Operation modules: one file per operation, plus a folder an operator can add.

The class file lists function files. It does not call them. These tests check
that every built-in catalog handler still resolves, and that an extra folder
shows up without replacing a built-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.modules import (
    DuplicateModuleError,
    ModuleLoadError,
    clear_module_cache,
    handler_map,
    load_modules,
)
from app.modules.order import CATALOG_ORDER
from app.services.catalog import get_operation, get_operation_catalog

ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "examples" / "modules" / "hello"

_RUN_ARGS = dict(
    handler="hello_say",
    op=None,
    job=None,
    env=None,
    log=lambda *_a, **_k: None,
    ctx=None,
    params={},
    deadline=None,
    check_cancel=None,
    dry=True,
    timeout=1,
    gs_root="",
    ans_root="",
    extra_env={},
    ssh_target=None,
    remote_env={},
    executor=None,
    agent_env_id=None,
)


def test_builtin_catalog_order_and_handlers():
    clear_module_cache()
    ops = get_operation_catalog()
    assert [op.id for op in ops] == list(CATALOG_ORDER)
    assert "hello.say" not in {op.id for op in ops}
    hmap = handler_map()
    for op in ops:
        assert op.handler in hmap, op.id
    assert len(hmap) == len({op.handler for op in ops})


def test_hello_module_adds_an_operation(monkeypatch):
    from app.config import get_settings

    clear_module_cache()
    custom = get_settings().model_copy(update={"module_paths": [str(HELLO)]})
    monkeypatch.setattr("app.config.get_settings", lambda: custom)
    op = get_operation("hello.say")
    assert op is not None
    assert op.handler == "hello_say"
    assert op.backend == "internal"
    assert op.required_role == "viewer"
    result = handler_map()["hello_say"](None, **{**_RUN_ARGS, "params": {"name": "ops"}})
    assert result["ok"] is True
    assert result["message"] == "hello ops"
    # Built-ins stay in front. The sample is not one of them, so it sorts last.
    ids = [item.id for item in get_operation_catalog()]
    assert ids[-1] == "hello.say"
    assert ids[:-1] == list(CATALOG_ORDER)


def test_duplicate_handler_is_rejected(tmp_path):
    pkg = tmp_path / "clash"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from app.modules.base import Module\n"
        "class ClashModule(Module):\n"
        "    name = 'clash'\n"
        "    functions = ('again',)\n",
        encoding="utf-8",
    )
    (pkg / "again.py").write_text(
        "HANDLERS = ('internal_health',)\n"
        "def run(self, **_kwargs):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    clear_module_cache()
    with pytest.raises(DuplicateModuleError, match="internal_health"):
        load_modules([str(pkg)])


def test_custom_module_can_use_a_relative_import(tmp_path):
    pkg = tmp_path / "greetmod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from app.modules.base import Module\n"
        "class GreetModule(Module):\n"
        "    name = 'greetmod'\n"
        "    functions = ('greet',)\n",
        encoding="utf-8",
    )
    (pkg / "helper.py").write_text('WORD = "from-helper"\n', encoding="utf-8")
    (pkg / "greet.py").write_text(
        "from .helper import WORD\n"
        "HANDLERS = ('greet_say',)\n"
        "OPERATION = {\n"
        "    'id': 'greet.say',\n"
        "    'name': 'Greet',\n"
        "    'description': 'Say the helper word.',\n"
        "    'required_role': 'viewer',\n"
        "    'backend': 'internal',\n"
        "    'params': [],\n"
        "    'handler': 'greet_say',\n"
        "}\n"
        "def run(self, handler, op, job, env, log, ctx, params, deadline,\n"
        "        check_cancel, dry, timeout, gs_root, ans_root, extra_env,\n"
        "        ssh_target, remote_env, executor, agent_env_id):\n"
        "    return {'ok': True, 'message': WORD}\n",
        encoding="utf-8",
    )
    clear_module_cache()
    load_modules([str(pkg)])
    result = handler_map([str(pkg)])["greet_say"](None, **_RUN_ARGS)
    assert result == {"ok": True, "message": "from-helper"}


def test_missing_init_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    clear_module_cache()
    with pytest.raises(ModuleLoadError, match="__init__.py"):
        load_modules([str(empty)])


def test_module_paths_from_yaml(tmp_path):
    from app.config import Settings, load_settings

    assert Settings().module_paths == []
    one = tmp_path / "mods" / "one"
    two = tmp_path / "mods" / "two"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"modules": {"paths": ["mods/one", str(two)]}}),
        encoding="utf-8",
    )
    loaded = load_settings(cfg)
    assert loaded.module_paths == [str(one.resolve()), str(two.resolve())]

    cfg.write_text("modules:\n  paths: mods/one, mods/two\n", encoding="utf-8")
    loaded = load_settings(cfg)
    assert loaded.module_paths == [str(one.resolve()), str(two.resolve())]
