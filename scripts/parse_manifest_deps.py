#!/usr/bin/env python3
"""Parse manifest.json files to extract dependency changes.

This script compares old and new versions of manifest.json files
to identify changes in the requirements field.
"""

# ruff: noqa: T201
import json
import sys


def parse_requirements(manifest_content: str) -> list[str]:
    """Extract requirements from manifest JSON content.

    :param manifest_content: JSON string content of manifest file.
    """
    try:
        data = json.loads(manifest_content)
        return data.get("requirements", [])
    except (json.JSONDecodeError, KeyError):
        return []


def main() -> int:
    """Parse manifest dependency changes."""
    if len(sys.argv) != 3:
        print("Usage: parse_manifest_deps.py <old_manifest> <new_manifest>")
        return 1

    old_file = sys.argv[1]
    new_file = sys.argv[2]

    try:
        with open(old_file) as f:
            old_reqs = parse_requirements(f.read())
    except FileNotFoundError:
        old_reqs = []

    try:
        with open(new_file) as f:
            new_reqs = parse_requirements(f.read())
    except FileNotFoundError:
        print("Error: New manifest file not found")
        return 1

    # Find added, removed, and unchanged requirements
    old_set = set(old_reqs)
    new_set = set(new_reqs)

    added = new_set - old_set
    removed = old_set - new_set
    unchanged = old_set & new_set

    if not added and not removed:
        print("No dependency changes")
        return 0

    # Output in markdown format
    if added:
        print("**Added:**")
        for req in sorted(added):
            print(f"- ✅ `{req}`")
        print()

    if removed:
        print("**Removed:**")
        for req in sorted(removed):
            print(f"- ❌ `{req}`")
        print()

    if unchanged and (added or removed):
        print("<details>")
        print("<summary>Unchanged dependencies</summary>")
        print()
        for req in sorted(unchanged):
            print(f"- `{req}`")
        print()
        print("</details>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
