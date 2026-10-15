#!/usr/bin/env python3
"""Fail when the implementation could resume the most recent session.

Automatic recovery must always be bound to one explicit session UUID: another
Codex session may start while a recovery waits, so the most-recent-session flag
would resume the wrong conversation. This checks real code, not prose: the flag
is searched for as executable text (string literals outside docstrings, or
script lines that are not comments), so a comment or docstring that documents
the prohibition does not trip it.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FLAG = "--last"
TARGETS = ("src", "bin", "hooks")


def docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def check_python(path: pathlib.Path, failures: list[str]) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        failures.append(f"{path}: could not be parsed: {error}")
        return
    skip = docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            if FLAG in node.value:
                failures.append(f"{path}:{node.lineno}: string literal contains {FLAG}")


def check_script(path: pathlib.Path, failures: list[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        failures.append(f"{path}: could not be read: {error}")
        return
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if FLAG in stripped:
            failures.append(f"{path}:{number}: script line contains {FLAG}")


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    failures: list[str] = []
    for target in TARGETS:
        base = root / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                check_python(path, failures)
            elif target in ("bin", "hooks"):
                check_script(path, failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("no-last: FAILED — recovery must always name the session explicitly")
        return 1
    print("no-last: ok — the implementation never resumes the most recent session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
