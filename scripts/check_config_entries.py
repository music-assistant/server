"""
Guard against invalid ConfigEntry/ConfigValueOption definitions.

Fails when a ConfigEntry or ConfigValueOption hardcodes user-facing text instead of strings.json,
when a ConfigEntry.category value is not defined in any strings.json config_categories section,
or when an entry returned by ``get_config_entries`` is required but has no default value.

A provider instance is created with empty values (setup input is collected by the setup flow into
``setup_data``), and its config is validated at load right after the instance is constructed. An
options entry that is ``required`` without a ``default_value`` therefore has nothing to resolve to
and fails that validation, so the provider can never be added at all.

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

ConfigEntry.category must be a key defined under ``config_categories`` in any strings.json (common
or provider-local). Raw English strings (e.g. ``"Guest Features"``) cannot be looked up and will
never localize.

Template/test providers (``_*`` and ``test``) are skipped, matching ``build_translations.py``.

Usage:
    uv run -m scripts.check_config_entries
"""

from __future__ import annotations

import ast
import json
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

# ConfigEntryType members that never hold a value (models: config_entries.UI_ONLY)
_UI_ONLY_TYPES = frozenset({"LABEL", "DIVIDER", "ACTION", "ALERT", "IMAGE", "URL"})


def find_violations() -> list[str]:
    """Return a sorted list of ``path:line: message`` for every invalid config entry field."""
    violations: list[str] = []
    valid_categories = _load_valid_categories()
    for path in _iter_python_files():
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        options_entries = _options_entry_calls(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            check = _CHECKS.get(name)
            if check is not None:
                fields, flag_fstrings, target = check
                for keyword in node.keywords:
                    if keyword.arg in fields and _is_hardcoded_text(keyword.value, flag_fstrings):
                        violations.append(
                            f"{rel}:{keyword.value.lineno}: {name} '{keyword.arg}' is "
                            f"hardcoded; author it in the owner's strings.json ({target})"
                        )
            if name == "ConfigEntry":
                for keyword in node.keywords:
                    if keyword.arg != "category":
                        continue
                    if not (
                        isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        continue
                    cat = keyword.value.value
                    if cat not in valid_categories:
                        violations.append(
                            f"{rel}:{keyword.value.lineno}: ConfigEntry category={cat!r} is not "
                            f"defined in any strings.json config_categories section"
                        )
                if id(node) in options_entries and _is_required_without_value(node):
                    violations.append(
                        f"{rel}:{node.lineno}: ConfigEntry {_entry_key(node)} is required but has "
                        f"no default_value; an options entry must resolve without user input "
                        f"(collect setup input in the provider's setup_flow instead)"
                    )
    return sorted(violations)


def main() -> int:
    """Print every invalid ConfigEntry/ConfigValueOption definition; return 1 when any were found."""
    violations = find_violations()
    if not violations:
        return 0
    print("Invalid ConfigEntry/ConfigValueOption definitions found:", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


def _load_valid_categories() -> set[str]:
    """Return all category keys defined under config_categories in any strings.json."""
    valid: set[str] = set()
    strings_files = [
        os.path.join(PACKAGE_ROOT, "strings.json"),
        *[
            os.path.join(root, "strings.json")
            for root, _dirs, files in os.walk(PACKAGE_ROOT)
            if "strings.json" in files and root != PACKAGE_ROOT
        ],
    ]
    for path in strings_files:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            valid.update(data.get("config_categories", {}).keys())
        except OSError, json.JSONDecodeError:
            continue
    return valid


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


def _options_entry_calls(tree: ast.Module) -> set[int]:
    """
    Return the node ids of the ConfigEntry calls built inside a ``get_config_entries``.

    Only those entries end up in a config's values and are validated at load; the entries a
    setup flow builds for its forms are input fields and are required without a default by
    design, so they must not be flagged.
    """
    result: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name != "get_config_entries":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _call_name(inner) == "ConfigEntry":
                result.add(id(inner))
    return result


def _is_required_without_value(node: ast.Call) -> bool:
    """Return True when a required ConfigEntry has neither a default nor a preset value."""
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    if (required := keywords.get("required")) is not None:
        if not isinstance(required, ast.Constant):
            # a computed flag can't be judged statically
            return False
        if required.value is not True:
            return False
    elif _entry_type(node) in _UI_ONLY_TYPES:
        # ConfigEntry.required defaults to True, but __post_init__ clears it for UI-only types
        return False
    for arg in ("default_value", "value"):
        if (given := keywords.get(arg)) is None:
            continue
        # a computed default can't be judged statically, so only a literal None counts as absent
        if not (isinstance(given, ast.Constant) and given.value is None):
            return False
    return True


def _entry_type(node: ast.Call) -> str:
    """Return the name of a ConfigEntry's ConfigEntryType member, or ``""`` when not literal."""
    entry_type = {keyword.arg: keyword.value for keyword in node.keywords}.get("type")
    if entry_type is None and len(node.args) > 1:
        entry_type = node.args[1]
    return entry_type.attr if isinstance(entry_type, ast.Attribute) else ""


def _entry_key(node: ast.Call) -> str:
    """Return a readable rendering of a ConfigEntry's key argument for a violation message."""
    key = {keyword.arg: keyword.value for keyword in node.keywords}.get("key")
    if isinstance(key, ast.Constant):
        return repr(key.value)
    if isinstance(key, ast.Name):
        return key.id
    return key.attr if isinstance(key, ast.Attribute) else "<unknown>"


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
