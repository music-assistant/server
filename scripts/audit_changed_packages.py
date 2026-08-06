"""
CI check: decide whether pip-audit findings affect the dependencies a pull request changes.

pip-audit scans the whole installed environment, so on any pull request it also reports
vulnerabilities in packages the pull request never touched. Those must not gate it: the
author cannot act on them, and the resulting status is not overridable by a maintainer.

Only direct requirements are compared, so a vulnerability reaching the environment through
a transitive dependency is reported without gating.

Prints the status the reporting workflow gates on:
    pass         no known vulnerabilities at all
    preexisting  vulnerabilities exist, but none in the changed dependencies
    fail         vulnerabilities in dependencies this pull request adds or changes

Usage:
    python3 scripts/audit_changed_packages.py audit.json new_deps.txt
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ruff: noqa: T201

# Leading package name of a requirement line, before any extras, version or marker.
REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9._-]+)")


def normalize(name: str) -> str:
    """
    Return the PEP 503 normalized form of a package name.

    :param name: Package name as written in a requirement line or an audit report.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def changed_packages(requirements: str) -> set[str]:
    """
    Return the normalized names of the packages in the given requirement lines.

    :param requirements: Requirement lines a pull request adds or changes, one per line.
    """
    names = set()
    for raw in requirements.splitlines():
        line = raw.strip()
        # Skip comments and pip options such as --index-url
        if not line or line.startswith(("#", "-")):
            continue
        if match := REQUIREMENT_NAME.match(line):
            names.add(normalize(match.group(1)))
    return names


def vulnerable_packages(audit: str) -> set[str]:
    """
    Return the normalized names of the packages pip-audit reported vulnerabilities for.

    :param audit: A pip-audit report in JSON format.
    """
    # Indexed rather than fetched with a default: pip-audit is installed unpinned, so a
    # schema change has to surface as an error instead of an empty, passing result set
    report = json.loads(audit)
    return {normalize(dep["name"]) for dep in report["dependencies"] if dep.get("vulns")}


def audit_status(audit: str, requirements: str) -> str:
    """
    Return `pass`, `preexisting` or `fail` for the given audit report and changed requirements.

    :param audit: A pip-audit report in JSON format.
    :param requirements: Requirement lines a pull request adds or changes, one per line.
    """
    vulnerable = vulnerable_packages(audit)
    if not vulnerable:
        return "pass"
    return "fail" if vulnerable & changed_packages(requirements) else "preexisting"


def main(argv: list[str] | None = None) -> int:
    """Print the audit status for the given report and changed requirements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="pip-audit report in JSON format.")
    parser.add_argument(
        "requirements", type=Path, help="Requirement lines the pull request adds or changes."
    )
    args = parser.parse_args(argv)

    # The requirements file is only written when the pull request changes dependencies
    requirements = args.requirements.read_text() if args.requirements.is_file() else ""
    print(audit_status(args.audit.read_text(), requirements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
