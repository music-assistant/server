"""
Fail when async code makes a known-blocking, module-qualified call that ruff's ASYNC rules miss.

Music Assistant is fully async: blocking IO inside a coroutine stalls the event loop and every other
task on it. Ruff's flake8-async already flags the obvious offenders (``open()``, ``time.sleep()``,
``subprocess`` …); this check complements it with high-confidence library calls it does not cover
(``requests``, ``urllib.request``, ``shutil``, blocking ``os`` filesystem calls, ``socket``,
``soco`` …) made directly inside an ``async def``. Wrap such work in ``asyncio.to_thread`` /
``run_in_executor`` (pass the function reference rather than calling it inline) or use an async
client. Only calls are matched, so a blocking attribute read (``soco.volume``) slips through.

The call sites that predate this check are grandfathered through
``scripts/lint_baselines/blocking_io_in_async.txt``.

Usage:
    uv run -m scripts.check_blocking_io
    uv run -m scripts.check_blocking_io --update-baseline   # after moving calls off the loop
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.lint_baseline import diff_baseline, load_baseline, render_baseline

# ruff: noqa: T201

# repo paths (this file lives at <repo>/scripts/check_blocking_io.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "music_assistant"
BASELINE_PATH = REPO_ROOT / "scripts" / "lint_baselines" / "blocking_io_in_async.txt"

# A SoCo device and the UPnP service proxies it exposes: every method on these is a SOAP
# request over synchronous HTTP. ``zone_group_state`` is deliberately absent, its calls only
# touch an in-memory cache.
SOCO_OBJECTS = frozenset(
    {
        "soco",
        "alarmClock",
        "audioIn",
        "avTransport",
        "contentDirectory",
        "deviceProperties",
        "groupRenderingControl",
        "musicServices",
        "renderingControl",
        "systemProperties",
        "zoneGroupTopology",
    }
)

# Blocking, module-qualified calls ruff's ASYNC rules don't catch, keyed by the immediate module
# name in the attribute chain (``module.method(...)``). An empty value set means "any attribute".
BLOCKING_CALLS: dict[str, frozenset[str]] = {
    "requests": frozenset({"get", "post", "put", "delete", "patch", "head", "request"}),
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "move", "rmtree"}),
    "os": frozenset(
        {
            "listdir",
            "walk",
            "scandir",
            "makedirs",
            "mkdir",
            "removedirs",
            "rmdir",
            "remove",
            "rename",
            "replace",
            "unlink",
        }
    ),
    "socket": frozenset({"create_connection"}),
    **dict.fromkeys(SOCO_OBJECTS, frozenset()),
}

# urllib.request.urlopen(...) / .urlretrieve(...): matched anywhere a ``urllib`` chain ends in these.
URLLIB_METHODS = frozenset({"urlopen", "urlretrieve"})

_BASELINE_HEADER = (
    "Blocking, module-qualified calls inside async functions that predate this check.\n"
    "New async code must offload these (asyncio.to_thread / run_in_executor) or use an async client.\n"
    "Regenerate with: uv run -m scripts.check_blocking_io --update-baseline"
)


def find_violations() -> dict[str, list[str]]:
    """Return ``{repo-relative path: [messages]}`` for blocking calls made inside ``async def``."""
    violations: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, call in _BlockingCallVisitor().collect(tree):
            violations.setdefault(rel, []).append(
                f"{rel}:{lineno}: {call}() blocks the event loop; offload it or use an async client"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Report new blocking calls in async code; return 1 when any were found."""
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
        print("Blocking IO found inside async functions:")
        for path in sorted(regressions):
            for message in violations[path]:
                print(f"  {message}")
    if improvements:
        print(
            "These files have fewer blocking calls than the baseline; update it with "
            "`uv run -m scripts.check_blocking_io --update-baseline`:"
        )
        for path in sorted(improvements):
            print(f"  {path}")
    return 1


class _BlockingCallVisitor(ast.NodeVisitor):
    """Collect blocking-call locations whose nearest enclosing function is an ``async def``."""

    def __init__(self) -> None:
        self._async_stack: list[bool] = []
        self._found: list[tuple[int, str]] = []

    def collect(self, tree: ast.AST) -> list[tuple[int, str]]:
        """Return ``(lineno, dotted_call)`` for every blocking call inside an async function."""
        self.visit(tree)
        return self._found

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Track a synchronous scope (blocking calls here are fine, e.g. a to_thread target)."""
        self._async_stack.append(False)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Track an asynchronous scope (blocking calls here stall the loop)."""
        self._async_stack.append(True)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        """Record the call when it is blocking and its nearest enclosing function is async."""
        if self._async_stack and self._async_stack[-1] and (call := _blocking_call(node)):
            self._found.append((node.lineno, call))
        self.generic_visit(node)


def _blocking_call(node: ast.Call) -> str | None:
    """Return the dotted call name when a call matches a known blocking pattern, else None."""
    if not isinstance(node.func, ast.Attribute):
        return None
    chain = _attr_chain(node.func)
    if len(chain) < 2:
        return None
    method, module = chain[-1], chain[-2]
    allowed = BLOCKING_CALLS.get(module)
    if allowed is not None and (not allowed or method in allowed):
        return ".".join(chain)
    if method in URLLIB_METHODS and "urllib" in chain[:-1]:
        return ".".join(chain)
    return None


def _attr_chain(node: ast.Attribute) -> list[str]:
    """Return the dotted attribute chain of a node (e.g. ``urllib.request.urlopen``) as names."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return parts[::-1]


if __name__ == "__main__":
    raise SystemExit(main())
