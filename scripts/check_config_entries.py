"""
Fail when a ConfigEntry hardcodes user-facing text instead of authoring it in strings.json.

A ConfigEntry's ``label``/``description``/``action_label`` are localized at serialization from the
owning provider's (or the common) ``strings.json`` via the ``config_entries.<key>`` translation
key, so they must not be passed as literals in code: a hardcoded string never reaches Lokalise and
stays English-only. This is a pre-commit/CI guard that scans the source tree and prints every
offending call so it can be moved into a strings.json.

Both plain string literals and f-strings are flagged: a dynamic label/description must use a
strings.json template (with ``{0}``/``{1}`` placeholders) plus ``translation_params`` rather than
interpolating the value into the string in code.

Template/test providers (``_*`` and ``test``) are skipped, matching ``build_translations.py``.

Usage:
    uv run -m scripts.check_config_entries
"""

from __future__ import annotations

import ast
import os
import sys
from typing import TypeGuard

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_config_entries.py)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(_REPO_ROOT, "music_assistant")
# ConfigEntry text fields that must be authored in strings.json, never hardcoded in code.
LOCALIZED_FIELDS = ("label", "description", "action_label")


def find_violations() -> list[str]:
    """Return a sorted list of ``path:line: message`` for every hardcoded ConfigEntry text field."""
    violations: list[str] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        for node in ast.walk(tree):
            if not _is_config_entry_call(node):
                continue
            for keyword in node.keywords:
                if keyword.arg in LOCALIZED_FIELDS and _is_hardcoded_text(keyword.value):
                    violations.append(
                        f"{rel}:{keyword.value.lineno}: ConfigEntry '{keyword.arg}' is hardcoded; "
                        f"author it in the owner's strings.json (config_entries.<key>.{keyword.arg})"
                    )
    return sorted(violations)


def main() -> int:
    """Print every hardcoded ConfigEntry text field; return 1 when any were found."""
    violations = find_violations()
    if not violations:
        return 0
    print(
        "Hardcoded ConfigEntry strings found. Move them to the owner's strings.json "
        "(under config_entries.<key>) instead of passing them in code:",
        file=sys.stderr,
    )
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


def _iter_python_files() -> list[str]:
    """Return all shipped provider/controller Python files (skipping ``_*``/``test`` templates)."""
    result: list[str] = []
    for root, dirs, files in os.walk(PACKAGE_ROOT):
        parts = root.split(os.sep)
        if "providers" in parts:
            index = parts.index("providers")
            if len(parts) > index + 1 and (
                parts[index + 1].startswith("_") or parts[index + 1] == "test"
            ):
                dirs[:] = []
                continue
        result.extend(os.path.join(root, name) for name in files if name.endswith(".py"))
    return result


def _read(path: str) -> str:
    """Return the text content of a file."""
    with open(path, encoding="utf-8") as file:
        return file.read()


def _is_config_entry_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Return True for a ``ConfigEntry(...)`` constructor call (by name or attribute)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "ConfigEntry"
    return isinstance(func, ast.Attribute) and func.attr == "ConfigEntry"


def _is_hardcoded_text(node: ast.AST) -> bool:
    """Return True for a string literal or an f-string; both hardcode user-facing text in code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    return isinstance(node, ast.JoinedStr)


if __name__ == "__main__":
    raise SystemExit(main())
