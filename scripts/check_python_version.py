"""Verify pyproject.toml Python-version fields match .python-version."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# ruff: noqa: T201


def _emit_warnings(lines: list[str]) -> None:
    """Print warnings; in an interactive terminal, overwrite pre-commit's verbose metadata."""
    block = "\n".join(lines)
    try:
        with Path("/dev/tty").open("r"):
            # Move up past pre-commit's "- hook id", "- duration", blank line, clear,
            # then colorize the block in 256-color orange (xterm palette index 208).
            block = f"\033[3A\033[J\033[38;5;208m{block}\033[0m"
    except OSError:
        pass
    print(block)


def main() -> int:
    """Entry point; returns 0 on success, 1 on any drift."""
    root = Path(__file__).resolve().parent.parent
    pin = (root / ".python-version").read_text().strip()
    parts = pin.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        print(f"ERROR: .python-version has unexpected content: {pin!r}")
        return 1
    major_minor = f"{parts[0]}.{parts[1]}"
    py_target = f"py{parts[0]}{parts[1]}"
    expected_requires = f">={pin}"
    expected_classifier = f"Programming Language :: Python :: {major_minor}"

    with (root / "pyproject.toml").open("rb") as fp:
        data = tomllib.load(fp)

    # Hard errors: runtime/distribution contract — must match .python-version exactly.
    errors: list[str] = []
    # Soft warnings: linter/type-checker compat targets — may intentionally lag the
    # runtime (e.g. pinned to py313 while runtime is 3.14 to keep dev backport-friendly
    # with stable). Surfaced for awareness but not blocking.
    warnings: list[str] = []

    project = data.get("project", {})
    requires = project.get("requires-python", "")
    if requires != expected_requires:
        errors.append(f"project.requires-python is {requires!r}, expected {expected_requires!r}")

    classifiers = project.get("classifiers", [])
    python_classifiers = [
        c for c in classifiers if c.startswith("Programming Language :: Python ::")
    ]
    if python_classifiers != [expected_classifier]:
        errors.append(
            f"project.classifiers Python entries are {python_classifiers!r}, "
            f"expected exactly [{expected_classifier!r}]"
        )

    ruff_target = data.get("tool", {}).get("ruff", {}).get("target-version", "")
    if ruff_target != py_target:
        warnings.append(f"tool.ruff.target-version is {ruff_target!r}, expected {py_target!r}")

    mypy_python = data.get("tool", {}).get("mypy", {}).get("python_version", "")
    if mypy_python != major_minor:
        warnings.append(f"tool.mypy.python_version is {mypy_python!r}, expected {major_minor!r}")

    if warnings:
        _emit_warnings(
            [f"pyproject.toml soft drift against .python-version ({pin}):"]
            + [f"  - WARN: {warn}" for warn in warnings]
        )

    if errors:
        print(f"pyproject.toml drift detected against .python-version ({pin}):")
        for err in errors:
            print(f"  - ERROR: {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
