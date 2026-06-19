"""
Decide which pytest targets a CI run should execute based on the changed files.

Reads the changed file paths (one per line) from stdin and writes ``mode`` and
``test_paths`` to ``$GITHUB_OUTPUT`` (and stdout). ``mode`` is one of:

- ``full``: run the entire suite (a shared/core file, dependency or the CI itself changed)
- ``partial``: run only the listed ``test_paths`` (one or more changed providers)
- ``skip``: nothing testable changed (docs, unrelated workflows, ...)
"""

# ruff: noqa: T201

from __future__ import annotations

import os
import sys
from pathlib import Path

# A change to any of these forces the full suite: dependencies and the runtime
# pin (everything is reinstalled/retested) plus the selector and workflow itself.
FULL_TRIGGER_FILES = {
    "pyproject.toml",
    "requirements_all.txt",
    "uv.lock",
    ".python-version",
    ".github/workflows/test.yml",
    "scripts/ci_test_scope.py",
}


def provider_name(path: str) -> str | None:
    """Return the provider a path belongs to, or None if it is not provider-scoped."""
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] in ("music_assistant", "tests") and parts[1] == "providers":
        return parts[2]
    return None


def decide(changed: list[str], repo_root: Path) -> tuple[str, list[str]]:
    """
    Decide the test scope for a set of changed files.

    :param changed: Changed file paths, relative to the repo root.
    :param repo_root: Repository root, used to confirm a provider has a test dir.
    """
    providers: set[str] = set()
    for path in changed:
        provider = provider_name(path)
        if provider is not None:
            providers.add(provider)
        elif path in FULL_TRIGGER_FILES or path.startswith(("music_assistant/", "tests/")):
            # Shared/core code changed -> the change can reach anything.
            return "full", []

    if not providers:
        return "skip", []

    # Only providers changed: target their test dirs. If any changed provider has
    # no tests we cannot get a targeted signal, so fall back to the full suite.
    paths: list[str] = []
    for name in sorted(providers):
        if not (repo_root / "tests" / "providers" / name).is_dir():
            return "full", []
        paths.append(f"tests/providers/{name}")
    return "partial", paths


def main() -> None:
    """Read changed files from stdin and emit the resolved scope."""
    changed = [line.strip() for line in sys.stdin if line.strip()]
    mode, paths = decide(changed, Path.cwd())
    lines = [f"mode={mode}", f"test_paths={' '.join(paths)}"]
    if github_output := os.environ.get("GITHUB_OUTPUT"):
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
