"""Verify pyproject.toml Python-version fields match .python-version."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# ruff: noqa: T201


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

    errors: list[str] = []

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
        errors.append(f"tool.ruff.target-version is {ruff_target!r}, expected {py_target!r}")

    mypy_python = data.get("tool", {}).get("mypy", {}).get("python_version", "")
    if mypy_python != major_minor:
        errors.append(f"tool.mypy.python_version is {mypy_python!r}, expected {major_minor!r}")

    if errors:
        print(f"pyproject.toml drift detected against .python-version ({pin}):")
        for err in errors:
            print(f"  - {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
