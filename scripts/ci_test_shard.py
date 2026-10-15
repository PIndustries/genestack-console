#!/usr/bin/env python3
"""Split tests/test_*.py across CI shards.

One pytest process shares a session-scoped database, so shards are separate
runners, not pytest-xdist. Weight is the static test count (parametrize
lists included) so a shard stays near the same size.

    python3 scripts/ci_test_shard.py SHARD TOTAL
    python3 scripts/ci_test_shard.py --check-split TOTAL
    SHARD_RESULT=success python3 scripts/ci_test_shard.py --require-shards TOTAL

SHARD is 1-based. --check-split checks the split covers every test file
exactly once. --require-shards does that and also exits non-zero unless
SHARD_RESULT is success.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"


def _assign_len(node: ast.AST) -> int | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    return None


def _names(tree: ast.AST) -> dict[str, int]:
    found: dict[str, int] = {}
    for node in tree.body:
        value = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets = [node.target]
        length = _assign_len(value) if value is not None else None
        if length is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = length
    return found


def _param_factor(decorators: list[ast.expr], names: dict[str, int]) -> int:
    factor = 1
    for dec in decorators:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        attr = func.attr if isinstance(func, ast.Attribute) else ""
        ident = func.id if isinstance(func, ast.Name) else ""
        if attr != "parametrize" and ident != "parametrize":
            continue
        if not dec.args:
            continue
        arg = dec.args[-1]
        length = _assign_len(arg)
        if length is None and isinstance(arg, ast.Name):
            length = names.get(arg.id)
        if length:
            factor *= length
    return factor


def _weight(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _names(tree)
    total = 0

    def add(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        nonlocal total
        if node.name.startswith("test_"):
            total += _param_factor(node.decorator_list, names)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node)
        elif isinstance(node, ast.ClassDef):
            class_factor = _param_factor(node.decorator_list, names)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("test_"):
                        total += class_factor * _param_factor(item.decorator_list, names)
    return max(total, 1)


def test_files() -> list[tuple[str, int]]:
    files = []
    for path in sorted(TEST_DIR.glob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        files.append((rel, _weight(path)))
    if not files:
        raise SystemExit("no tests/test_*.py files")
    return files


def shard_groups(total: int) -> list[list[str]]:
    if total < 1:
        raise SystemExit("shard total must be >= 1")
    groups: list[list[str]] = [[] for _ in range(total)]
    loads = [0] * total
    ranked = sorted(test_files(), key=lambda item: (-item[1], item[0]))
    for rel, weight in ranked:
        index = min(range(total), key=lambda i: (loads[i], i))
        groups[index].append(rel)
        loads[index] += weight
    for group in groups:
        group.sort()
    return groups


def _parse_total(raw: str) -> int:
    try:
        total = int(raw)
    except ValueError as exc:
        raise SystemExit(f"not an integer: {raw}") from exc
    if total < 1:
        raise SystemExit("shard total must be >= 1")
    return total


def require_shards(total: int, *, check_result: bool) -> int:
    result = os.environ.get("SHARD_RESULT", "")
    groups = shard_groups(total)
    seen: dict[str, int] = {}
    problems: list[str] = []
    if check_result and result != "success":
        problems.append(f"shard job result is {result or 'missing'}, want success")
    for number, group in enumerate(groups, start=1):
        weight = 0
        files = dict(test_files())
        for rel in group:
            weight += files[rel]
            seen[rel] = seen.get(rel, 0) + 1
        print(f"shard {number}/{total}: {len(group)} files, weight {weight}")
        if not group:
            problems.append(f"shard {number} has no test files")
    all_files = [rel for rel, _weight in test_files()]
    missing = [rel for rel in all_files if seen.get(rel, 0) == 0]
    dupes = [rel for rel, count in sorted(seen.items()) if count != 1]
    if missing:
        problems.append("missing from every shard: " + ", ".join(missing))
    if dupes:
        problems.append("not in exactly one shard: " + ", ".join(dupes))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"split covers {len(all_files)} test files")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] in ("--require-shards", "--check-split"):
        if len(argv) != 3:
            print("usage: ci_test_shard.py --require-shards TOTAL", file=sys.stderr)
            print("       ci_test_shard.py --check-split TOTAL", file=sys.stderr)
            return 2
        return require_shards(
            _parse_total(argv[2]),
            check_result=argv[1] == "--require-shards",
        )
    if len(argv) != 3:
        print("usage: ci_test_shard.py SHARD TOTAL", file=sys.stderr)
        return 2
    total = _parse_total(argv[2])
    try:
        shard = int(argv[1])
    except ValueError as exc:
        raise SystemExit(f"not an integer: {argv[1]}") from exc
    if shard < 1 or shard > total:
        raise SystemExit(f"shard {shard} is outside 1..{total}")
    group = shard_groups(total)[shard - 1]
    if not group:
        raise SystemExit(f"shard {shard} has no test files")
    for rel in group:
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
