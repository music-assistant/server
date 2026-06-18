"""
Fail when a ConfigEntry or ConfigValueOption hardcodes user-facing text instead of strings.json.

A ConfigEntry's ``label``/``description``/``action_label`` and a ConfigValueOption's ``title`` are
localized at serialization from the owning provider's (or the common) ``strings.json`` — keyed by
``config_entries.<key>`` (option titles by ``config_entries.<key>.options.<value>``). Passing them
as literals in code means the text never reaches Lokalise and stays English-only. This is a
pre-commit/CI guard that scans the source tree and prints every offending call so the text can be
moved into a strings.json.

ConfigEntry text may also be composed in code (an f-string, concatenation, ``.format()``): a
dynamic label must instead use a strings.json template (with ``{0}``/``{1}`` placeholders) plus
``translation_params``. ConfigValueOption has no such mechanism, so a composed title is treated as
a legitimate data-driven value (player names, sample rates, ...) and left alone — only static
titles (a string literal, or a literal picked by a conditional) are flagged.

Template/test providers (``_*`` and ``test``) are skipped, matching ``build_translations.py``.

Usage:
    uv run -m scripts.check_config_entries
"""

from __future__ import annotations

import ast
import os
import sys

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_config_entries.py)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(_REPO_ROOT, "music_assistant")

# constructor name -> (localized text fields, whether an f-string also counts, strings.json target).
# ConfigValueOption titles cannot carry translation_params, so only literal titles are flagged.
_CHECKS: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "ConfigEntry": (("label", "description", "action_label"), True, "config_entries.<key>.<field>"),
    "ConfigValueOption": (("title",), False, "config_entries.<key>.options.<value>"),
}


def find_violations() -> list[str]:
    """Return a sorted list of ``path:line: message`` for every hardcoded config text field."""
    violations: list[str] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            check = _CHECKS.get(_call_name(node))
            if check is None:
                continue
            fields, flag_fstrings, target = check
            for keyword in node.keywords:
                if keyword.arg in fields and _is_hardcoded_text(keyword.value, flag_fstrings):
                    violations.append(
                        f"{rel}:{keyword.value.lineno}: {_call_name(node)} '{keyword.arg}' is "
                        f"hardcoded; author it in the owner's strings.json ({target})"
                    )
    return sorted(violations)


def main() -> int:
    """Print every hardcoded config text field; return 1 when any were found."""
    violations = find_violations()
    if not violations:
        return 0
    print(
        "Hardcoded config strings found. Move them into the owner's strings.json "
        "instead of passing them in code:",
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


def _call_name(node: ast.Call) -> str:
    """Return the called constructor's name (``Name`` id or ``Attribute`` attr), else ``""``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _is_hardcoded_text(node: ast.AST, flag_fstrings: bool) -> bool:
    """
    Return True when the value is, or embeds, user-facing text written in code.

    A plain string literal always counts, as does a literal selected by a conditional expression
    (``"A" if x else "B"``). Dynamically *composed* text — an f-string, string concatenation /
    ``%`` formatting, or a ``.format()``/``.join()`` call — counts only when ``flag_fstrings`` is
    set: ConfigEntry can move such text to a strings.json template with ``translation_params``,
    whereas a ConfigValueOption has no params and a composed title is a legitimate data-driven
    value. Text routed only through a variable is not detected (that needs data-flow analysis).
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.IfExp):
        return _is_hardcoded_text(node.body, flag_fstrings) or _is_hardcoded_text(
            node.orelse, flag_fstrings
        )
    if isinstance(node, ast.JoinedStr):
        return flag_fstrings
    if isinstance(node, ast.BinOp):
        return flag_fstrings and (
            _is_hardcoded_text(node.left, flag_fstrings)
            or _is_hardcoded_text(node.right, flag_fstrings)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("format", "join")
    ):
        return flag_fstrings and _is_hardcoded_text(node.func.value, flag_fstrings)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
