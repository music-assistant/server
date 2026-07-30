"""
Fail when a browse/recommendation folder hardcodes a display name instead of localizing it.

``BrowseFolder`` / ``RecommendationFolder`` render section titles in the UI. A static literal
``name="Top Charts"`` ships English-only; the name must be localized with a ``translation_key`` (the
literal stays as the English fallback) backed by an entry in the provider's ``strings.json``. Folder
names built from data (a variable, attribute or f-string — e.g. an artist or directory name) are
left alone: they are real content, not UI labels.

This complements ``check_config_entries`` (which guards ``ConfigEntry``/``ConfigValueOption`` text).
Template/test providers (``_*`` and ``test``) are skipped, matching ``build_translations``.

When invoked with file or folder arguments (as pre-commit does for the changed files) only those
provider files are scanned; with no arguments every shipped provider file is scanned.

Usage:
    uv run -m scripts.check_translatable_labels [PATH ...]
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_translatable_labels.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_PATH = REPO_ROOT / "music_assistant" / "providers"

# Media-item constructors whose ``name`` is a UI label that must carry a ``translation_key``.
TRANSLATABLE_CONSTRUCTORS = frozenset({"BrowseFolder", "RecommendationFolder"})


def find_violations(paths: Iterable[Path] | None = None) -> list[str]:
    """
    Return a sorted list of ``path:line: message`` for every hardcoded folder label.

    :param paths: Specific provider Python files to scan; when omitted every shipped provider
        file is scanned.
    """
    files = _iter_provider_files() if paths is None else sorted(paths)
    violations: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_hardcoded_label(node):
                violations.append(
                    f"{rel}:{node.lineno}: {_call_name(node)} has a hardcoded 'name'; add a "
                    "translation_key and author the label in the provider's strings.json"
                )
    return sorted(violations)


def main(argv: Sequence[str] | None = None) -> int:
    """Print every hardcoded folder label; return 1 when any were found."""
    args = list(sys.argv[1:] if argv is None else argv)
    violations = find_violations(_resolve_python_files(args) if args else None)
    if not violations:
        return 0
    print("Hardcoded browse/recommendation folder labels found:", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


def _iter_provider_files() -> list[Path]:
    """Return shipped provider Python files (skipping ``_*``/``test`` templates)."""
    return sorted(p for p in PROVIDERS_PATH.rglob("*.py") if _is_shipped_provider_file(p))


def _resolve_python_files(args: Sequence[str]) -> list[Path]:
    """
    Map file/folder arguments to the shipped provider Python files they cover.

    A folder contributes every ``*.py`` beneath it; ``_*``/``test`` templates and paths outside
    ``music_assistant/providers`` are skipped.

    :param args: Raw path arguments (e.g. the files pre-commit passes for a commit).
    """
    files: set[Path] = set()
    for raw in args:
        path = Path(raw).resolve()
        if path.is_dir():
            files.update(path.rglob("*.py"))
        elif path.suffix == ".py" and path.is_file():
            files.add(path)
    return sorted(p for p in files if _is_shipped_provider_file(p))


def _is_shipped_provider_file(path: Path) -> bool:
    """Return True when ``path`` is a shipped provider file (not a ``_*``/``test`` template)."""
    try:
        parts = path.resolve().relative_to(PROVIDERS_PATH).parts
    except ValueError:
        return False
    return bool(parts[:-1]) and not (parts[0].startswith("_") or parts[0] == "test")


def _is_hardcoded_label(node: ast.Call) -> bool:
    """Return True when a translatable folder is built with a literal ``name`` and no key."""
    if _call_name(node) not in TRANSLATABLE_CONSTRUCTORS:
        return False
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    if "translation_key" in keywords:
        return False
    name = keywords.get("name")
    return isinstance(name, ast.Constant) and isinstance(name.value, str)


def _call_name(node: ast.Call) -> str:
    """Return the called constructor's name (``Name`` id or ``Attribute`` attr), else ``""``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


if __name__ == "__main__":
    raise SystemExit(main())
