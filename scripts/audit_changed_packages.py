"""
CI check: decide whether pip-audit findings are ones a pull request introduces.

pip-audit scans the whole installed environment, so on any pull request it also reports
vulnerabilities that already exist on the branch it targets. Those must not gate it: the
author cannot act on them, and the resulting status is not overridable by a maintainer.

Findings are compared against the resolved dependency set of the target branch, so one
that reaches the environment through a transitive dependency gates as well. Without that
set the comparison falls back to the requirement lines the pull request changes, which
covers direct dependencies only. The pull request's own resolved set is taken into account
as well: the environment is installed before either branch is resolved, so a release
published in between must not read as a finding the pull request brings in.

Prints the status the reporting workflow gates on:
    pass         no known vulnerabilities at all
    preexisting  vulnerabilities exist, but the target branch has all of them too
    fail         vulnerabilities this pull request introduces

Usage:
    python3 scripts/audit_changed_packages.py audit.json new_deps.txt \
        [base_closure.txt] [head_closure.txt]
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

    A package maps to an empty set when no line pins it to an exact version, as a URL
    or a range requirement does not.

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


def resolved_findings(
    findings: set[tuple[str, str]], resolved: dict[str, set[str]]
) -> set[tuple[str, str]]:
    """
    Return the findings the given resolved dependency set installs.

    :param findings: Vulnerable packages as (name, version) pairs.
    :param resolved: Versions the dependency set resolves to, keyed on package name.
    """
    return {
        (name, version)
        for name, version in findings
        # A URL requirement carries no version to compare against, so its presence is
        # all that can be matched on
        if (pinned := resolved.get(name)) is not None and (not pinned or version in pinned)
    }


def audit_status(
    audit: str, requirements: str, base_closure: str = "", head_closure: str = ""
) -> str:
    """
    Return `pass`, `preexisting` or `fail` for the given audit report.

    :param audit: A pip-audit report in JSON format.
    :param requirements: Requirement lines the pull request adds or changes, one per line.
    :param base_closure: The target branch's resolved dependency set as pinned requirement
        lines. When empty, only the changed requirement lines are compared.
    :param head_closure: The pull request's own resolved dependency set as pinned requirement
        lines. When empty, every introduced finding gates.
    """
    findings = vulnerable_packages(audit)
    if not findings:
        return "pass"

    if base_closure.strip():
        changed = changed_packages(requirements)
        introduced = introduced_packages(findings, _resolution(base_closure), changed)
        # The environment is installed before either branch is resolved, so a release
        # published in between reads as introduced. Gate only on the findings the pull
        # request's own resolution carries as well.
        if head_closure.strip():
            introduced = resolved_findings(introduced, _resolution(head_closure))
        return "fail" if introduced else "preexisting"

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
    parser.add_argument(
        "head_closure",
        type=Path,
        nargs="?",
        help="Resolved dependency set of the pull request itself.",
    )
    args = parser.parse_args(argv)

    # The requirements file is only written when the pull request changes dependencies,
    # the resolved sets only when resolving them succeeded
    print(
        audit_status(
            args.audit.read_text(),
            _read_optional(args.requirements),
            _read_optional(args.base_closure),
            _read_optional(args.head_closure),
        )
    )
    return 0


def _read_optional(path: Path | None) -> str:
    """Return the contents of a file the workflow writes conditionally."""
    return path.read_text() if path and path.is_file() else ""


def _resolution(closure: str) -> dict[str, set[str]]:
    """Return the versions the given resolved dependency set pins."""
    resolved = requirement_versions(closure)
    # A resolution that cannot be read would silently reclassify every finding
    if not resolved:
        raise ValueError("No packages found in the resolved dependency set")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
