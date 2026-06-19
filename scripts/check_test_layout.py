"""
Fail when a provider test lives as a flat file instead of in its own folder.

Provider tests must live in ``tests/providers/<provider>/`` (mirroring
``music_assistant/providers/<provider>/``) so CI can map a changed provider to
its tests. A flat ``tests/providers/test_<provider>.py`` breaks that mapping.
"""

# ruff: noqa: T201

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Return non-zero if any flat provider test file exists."""
    flat = sorted(str(path) for path in Path("tests/providers").glob("test_*.py"))
    if flat:
        print("Provider tests must live in their own folder, not as a flat file:")
        for path in flat:
            name = Path(path).stem.removeprefix("test_")
            print(f"  {path}  ->  tests/providers/{name}/{Path(path).name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
