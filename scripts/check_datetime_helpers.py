"""
Fail when code calls ``datetime.now()`` / ``datetime.utcnow()`` instead of the shared helpers.

``music_assistant.helpers.datetime`` centralizes "current time" handling (``utc()``, ``now()``,
``utc_timestamp()``, ...) so the whole codebase agrees on timezone-awareness and is trivially
mockable in tests. Reaching for ``datetime.datetime.now(...)`` / ``.utcnow()`` directly bypasses
that. New code must use the helpers; the call sites that predate this check are grandfathered
through ``scripts/lint_baselines/naive_datetime_usage.txt`` and are cleaned up separately.

Usage:
    uv run -m scripts.check_datetime_helpers
    uv run -m scripts.check_datetime_helpers --update-baseline   # after migrating call sites
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.lint_baseline import diff_baseline, load_baseline, render_baseline

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_datetime_helpers.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "music_assistant"
BASELINE_PATH = REPO_ROOT / "scripts" / "lint_baselines" / "naive_datetime_usage.txt"

# The helper module itself is the one legitimate place to call datetime.now()/utcnow().
EXCLUDED_FILES = frozenset(
    {
        PACKAGE_ROOT / "helpers" / "datetime.py",
    }
)

_BASELINE_HEADER = (
    "Direct datetime.now()/utcnow() call sites that predate the helpers check.\n"
    "New code must use music_assistant.helpers.datetime (utc(), now(), ...).\n"
    "Regenerate with: uv run -m scripts.check_datetime_helpers --update-baseline"
)


def find_violations() -> dict[str, list[str]]:
    """Return ``{repo-relative path: [messages]}`` for every direct ``now()``/``utcnow()`` call."""
    violations: dict[str, list[str]] = {}
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (call := _naive_now_call(node)) is not None:
                violations.setdefault(rel, []).append(
                    f"{rel}:{node.lineno}: {call}() — use music_assistant.helpers.datetime instead"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Report new direct datetime call sites; return 1 when any were found."""
    argv = sys.argv[1:] if argv is None else argv
    violations = find_violations()
    counts = {path: len(messages) for path, messages in violations.items()}
    if "--update-baseline" in argv:
        BASELINE_PATH.write_text(render_baseline(counts, _BASELINE_HEADER), encoding="utf-8")
        print(f"Wrote {sum(counts.values())} call sites to {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0
    regressions, improvements = diff_baseline(counts, load_baseline(BASELINE_PATH))
    if not regressions and not improvements:
        return 0
    if regressions:
        print("Use music_assistant.helpers.datetime instead of calling datetime directly:")
        for path in sorted(regressions):
            for message in violations[path]:
                print(f"  {message}")
    if improvements:
        print(
            "These files have fewer direct datetime calls than the baseline; update it with "
            "`uv run -m scripts.check_datetime_helpers --update-baseline`:"
        )
        for path in sorted(improvements):
            print(f"  {path}")
    return 1


def _iter_python_files() -> list[Path]:
    """Return all shipped Python files except the datetime helper implementation itself."""
    return [path for path in sorted(PACKAGE_ROOT.rglob("*.py")) if path not in EXCLUDED_FILES]


def _naive_now_call(node: ast.Call) -> str | None:
    """
    Return the dotted call name when a node is a direct ``datetime`` now/utcnow call, else None.

    Matches ``datetime.now``, ``datetime.utcnow``, ``datetime.datetime.now`` and
    ``datetime.datetime.utcnow`` (the common shapes), regardless of arguments.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("now", "utcnow"):
        return None
    base = func.value
    if isinstance(base, ast.Name) and base.id == "datetime":
        return f"datetime.{func.attr}"
    if isinstance(base, ast.Attribute) and base.attr == "datetime":
        return f"datetime.datetime.{func.attr}"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
