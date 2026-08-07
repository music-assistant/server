"""
CI check: decide whether pip-audit findings are ones a pull request introduces.

pip-audit scans the whole installed environment, so on any pull request it also reports
vulnerabilities that already exist on the branch it targets. Those must not gate it: the
author cannot act on them, and the resulting status is not overridable by a maintainer.

Findings are compared against the resolved dependency set of the target branch, so one
that reaches the environment through a transitive dependency gates as well. Without that
set the comparison falls back to the requirement lines the pull request changes, which
covers direct dependencies only.

Prints the status the reporting workflow gates on:
    pass         no known vulnerabilities at all
    preexisting  vulnerabilities exist, but the target branch has all of them too
    fail         vulnerabilities this pull request introduces

Usage:
    python3 scripts/audit_changed_packages.py audit.json new_deps.txt [base_closure.txt]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ruff: noqa: T201

# Package name of a requirement line, plus the version when the line pins one exactly
REQUIREMENT = re.compile(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?:==\s*([^\s;]+))?")


def normalize(name: str) -> str:
    """
    Return the PEP 503 normalized form of a package name.

    :param name: Package name as written in a requirement line or an audit report.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_versions(requirements: str) -> dict[str, set[str]]:
    """
    Return the versions the given requirement lines pin, keyed on normalized package name.

    A package pinned by URL rather than by version maps to an empty set.

    :param requirements: Requirement lines, one per line.
    """
    versions: dict[str, set[str]] = {}
    for raw in requirements.splitlines():
        line = raw.strip()
        # Skip comments and pip options such as --index-url
        if not line or line.startswith(("#", "-")):
            continue
        if match := REQUIREMENT.match(line):
            pinned = versions.setdefault(normalize(match.group(1)), set())
            if match.group(2):
                pinned.add(match.group(2))
    return versions


def changed_packages(requirements: str) -> set[str]:
    """
    Return the normalized names of the packages in the given requirement lines.

    :param requirements: Requirement lines a pull request adds or changes, one per line.
    """
    return set(requirement_versions(requirements))


def vulnerable_packages(audit: str) -> set[tuple[str, str]]:
    """
    Return the packages pip-audit reported vulnerabilities for, as (name, version) pairs.

    :param audit: A pip-audit report in JSON format.
    """
    # Indexed rather than fetched with a default: pip-audit is installed unpinned, so a
    # schema change has to surface as an error instead of an empty, passing result set
    report = json.loads(audit)
    return {
        (normalize(dep["name"]), dep["version"])
        for dep in report["dependencies"]
        if dep.get("vulns")
    }


def introduced_packages(
    findings: set[tuple[str, str]],
    resolved: dict[str, set[str]],
    changed: frozenset[str] | set[str] = frozenset(),
) -> set[tuple[str, str]]:
    """
    Return the findings the given resolved dependency set does not already contain.

    :param findings: Vulnerable packages as (name, version) pairs.
    :param resolved: Versions the target branch resolves to, keyed on package name.
    :param changed: Names of the packages whose requirement the pull request changes.
    """
    introduced = set()
    for name, version in findings:
        pinned = resolved.get(name)
        if pinned is None:
            introduced.add((name, version))
        elif pinned:
            if version not in pinned:
                introduced.add((name, version))
        # A package the target branch pins by URL carries no version to compare against,
        # so only a change to its requirement tells the two branches apart
        elif name in changed:
            introduced.add((name, version))
    return introduced


def audit_status(audit: str, requirements: str, base_closure: str = "") -> str:
    """
    Return `pass`, `preexisting` or `fail` for the given audit report.

    :param audit: A pip-audit report in JSON format.
    :param requirements: Requirement lines the pull request adds or changes, one per line.
    :param base_closure: The target branch's resolved dependency set as pinned requirement
        lines. When empty, only the changed requirement lines are compared.
    """
    findings = vulnerable_packages(audit)
    if not findings:
        return "pass"

    if base_closure.strip():
        resolved = requirement_versions(base_closure)
        # A resolution that cannot be read would mark every finding as introduced
        if not resolved:
            raise ValueError("No packages found in the resolved dependency set")
        changed = changed_packages(requirements)
        return "fail" if introduced_packages(findings, resolved, changed) else "preexisting"

    changed = changed_packages(requirements)
    return "fail" if {name for name, _ in findings} & changed else "preexisting"


def main(argv: list[str] | None = None) -> int:
    """Print the audit status for the given report and dependency sets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="pip-audit report in JSON format.")
    parser.add_argument(
        "requirements", type=Path, help="Requirement lines the pull request adds or changes."
    )
    parser.add_argument(
        "base_closure",
        type=Path,
        nargs="?",
        help="Resolved dependency set of the branch the pull request targets.",
    )
    args = parser.parse_args(argv)

    # The requirements file is only written when the pull request changes dependencies,
    # the resolved set only when resolving the target branch succeeded
    requirements = args.requirements.read_text() if args.requirements.is_file() else ""
    base_closure = (
        args.base_closure.read_text() if args.base_closure and args.base_closure.is_file() else ""
    )
    print(audit_status(args.audit.read_text(), requirements, base_closure))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
