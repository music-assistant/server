"""
Fail when a class defines a public (or dunder) method below a private one.

``AGENTS.md`` requires private methods to live at the bottom of a class and public methods at the
top. Concretely: once a private (single/double-underscore, non-dunder) method appears in a class
body, every later method must also be private — a public or dunder method below a private one is a
violation. New code must follow this ordering; the classes that predate this check are grandfathered
through ``scripts/lint_baselines/private_methods_not_last.txt`` and are reordered separately.

Usage:
    uv run -m scripts.check_method_order
    uv run -m scripts.check_method_order --update-baseline   # after reordering offending classes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.lint_baseline import diff_baseline, load_baseline, render_baseline

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_method_order.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "music_assistant"
BASELINE_PATH = REPO_ROOT / "scripts" / "lint_baselines" / "private_methods_not_last.txt"

_BASELINE_HEADER = (
    "Classes that define a public/dunder method below a private one, predating this check.\n"
    "Private methods must live at the bottom of the class (see AGENTS.md).\n"
    "Regenerate with: uv run -m scripts.check_method_order --update-baseline"
)


def find_violations() -> dict[str, list[str]]:
    """
    Return ``{repo-relative path: [messages]}`` for every misplaced public/dunder method.

    A message is recorded for each public or dunder method that is defined after a private method
    within the same class body.
    """
    violations: dict[str, list[str]] = {}
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for lineno, name in _class_violations(node):
                violations.setdefault(rel, []).append(
                    f"{rel}:{lineno}: '{name}' is defined below a private method in class "
                    f"'{node.name}' — move private methods to the bottom of the class"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    """
    Report classes whose method ordering regressed beyond the baseline; return 1 when any are found.

    :param argv: Optional argument vector; pass ``--update-baseline`` to rewrite the baseline file.
    """
    argv = sys.argv[1:] if argv is None else argv
    violations = find_violations()
    counts = {path: len(messages) for path, messages in violations.items()}
    if "--update-baseline" in argv:
        BASELINE_PATH.write_text(render_baseline(counts, _BASELINE_HEADER), encoding="utf-8")
        print(f"Wrote {sum(counts.values())} method(s) to {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0
    regressions, improvements = diff_baseline(counts, load_baseline(BASELINE_PATH))
    if not regressions and not improvements:
        return 0
    if regressions:
        print("Private methods must live at the bottom of the class (see AGENTS.md):")
        for path in sorted(regressions):
            for message in violations[path]:
                print(f"  {message}")
    if improvements:
        print(
            "These files have fewer misplaced methods than the baseline; update it with "
            "`uv run -m scripts.check_method_order --update-baseline`:"
        )
        for path in sorted(improvements):
            print(f"  {path}")
    return 1


def _iter_python_files() -> list[Path]:
    """Return all shipped Python files under the package."""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _class_violations(cls: ast.ClassDef) -> list[tuple[int, str]]:
    """
    Return ``(lineno, name)`` for each public/dunder method defined below a private one.

    :param cls: The class definition whose direct method children are inspected, in source order.
    """
    bad: list[tuple[int, str]] = []
    seen_private = False
    for child in cls.body:
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if _is_private(child.name):
            seen_private = True
        elif seen_private:
            bad.append((child.lineno, child.name))
    return bad


def _is_private(name: str) -> bool:
    """Return whether a method name is private (underscore-prefixed but not a dunder)."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


if __name__ == "__main__":
    raise SystemExit(main())
