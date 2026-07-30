"""
Fail when a provider or controller test lives as a flat file instead of its own folder.

Provider tests must live in ``tests/providers/<name>/`` and controller tests in
``tests/controllers/<name>/`` (mirroring ``music_assistant/providers/<name>/`` and
``music_assistant/controllers/<name>/``) so CI can map a changed area to its tests.
A flat ``tests/providers/test_<name>.py`` / ``tests/controllers/test_<name>.py`` breaks that.
"""

# ruff: noqa: T201

from __future__ import annotations

import sys
from pathlib import Path

# Areas whose tests must be organised in per-<name> folders, not flat files.
FOLDERED_TEST_AREAS = ("tests/providers", "tests/controllers")


def main() -> int:
    """Return non-zero if any flat test file exists directly in a foldered area."""
    flat = sorted(
        str(path) for area in FOLDERED_TEST_AREAS for path in Path(area).glob("test_*.py")
    )
    if flat:
        print("These tests must live in their own <name>/ folder, not as a flat file:")
        for path in flat:
            parent = Path(path).parent
            name = Path(path).stem.removeprefix("test_")
            print(f"  {path}  ->  {parent}/{name}/{Path(path).name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
