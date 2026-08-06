"""Tests for scoping pip-audit findings to the dependencies a pull request changes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_changed_packages import (
    audit_status,
    changed_packages,
    main,
    normalize,
    vulnerable_packages,
)


def audit_report(*vulnerable: str, skipped: str | None = None) -> str:
    """
    Return a pip-audit JSON report where the given packages carry a vulnerability.

    :param vulnerable: Package names to report a vulnerability for.
    :param skipped: Package name pip-audit could not audit, if any.
    """
    dependencies = [
        {"name": name, "version": "1.0.0", "vulns": [{"id": "GHSA-test", "fix_versions": []}]}
        for name in vulnerable
    ]
    dependencies.append({"name": "clean-package", "version": "1.0.0", "vulns": []})
    if skipped:
        dependencies.append({"name": skipped, "skip_reason": "not found on PyPI"})
    return json.dumps({"dependencies": dependencies, "fixes": []})


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("aiohttp", "aiohttp"),
        ("Zeroconf", "zeroconf"),
        ("aiohttp_fast_zlib", "aiohttp-fast-zlib"),
        ("zope.interface", "zope-interface"),
        ("a__b", "a-b"),
    ],
)
def test_normalize(name: str, expected: str) -> None:
    """Package names are compared in their PEP 503 normalized form."""
    assert normalize(name) == expected


def test_changed_packages_reads_requirement_lines() -> None:
    """Package names are extracted regardless of the version specifier used."""
    requirements = """
        aiohttp==3.14.1
        music-assistant-models>=1.2
        some_pkg[extra]~=2.0 ; python_version >= '3.14'
        aiolibdatachannel @ git+https://github.com/example/aiolibdatachannel@v1

        # a comment
        --index-url https://example.org/simple
    """
    assert changed_packages(requirements) == {
        "aiohttp",
        "music-assistant-models",
        "some-pkg",
        "aiolibdatachannel",
    }


def test_vulnerable_packages_ignores_clean_and_skipped() -> None:
    """Only packages with findings count; skipped ones carry no vulns key."""
    report = audit_report("aiohttp", skipped="torch")
    assert vulnerable_packages(report) == {"aiohttp"}


def test_no_findings_passes() -> None:
    """A clean audit passes even when the pull request changes dependencies."""
    assert audit_status(audit_report(), "aiohttp==3.14.1") == "pass"


def test_findings_outside_the_changed_packages_are_preexisting() -> None:
    """Vulnerabilities in untouched packages describe the base branch, not the pull request."""
    assert audit_status(audit_report("aiohttp"), "some-pkg==1.0") == "preexisting"


def test_findings_without_dependency_changes_are_preexisting() -> None:
    """A pull request that changes no dependency can never introduce a vulnerability."""
    assert audit_status(audit_report("aiohttp"), "") == "preexisting"


def test_findings_in_a_changed_package_fail() -> None:
    """A vulnerability in a package the pull request touches blocks it."""
    assert audit_status(audit_report("aiohttp"), "aiohttp==3.14.1") == "fail"


def test_findings_match_across_name_spellings() -> None:
    """The comparison survives the underscore/dash spellings both sides use."""
    assert audit_status(audit_report("aiohttp_fast_zlib"), "aiohttp-fast-zlib==0.3.0") == "fail"


def test_main_prints_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The status is printed for the reporting workflow to pick up."""
    audit = tmp_path / "audit.json"
    audit.write_text(audit_report("aiohttp"))
    requirements = tmp_path / "new_deps.txt"
    requirements.write_text("aiohttp==3.14.1\n")

    assert main([str(audit), str(requirements)]) == 0
    assert capsys.readouterr().out.strip() == "fail"


def test_main_without_a_requirements_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The requirements file is absent when the analysis found no dependency changes."""
    audit = tmp_path / "audit.json"
    audit.write_text(audit_report("aiohttp"))

    assert main([str(audit), str(tmp_path / "missing.txt")]) == 0
    assert capsys.readouterr().out.strip() == "preexisting"


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("not json at all", json.JSONDecodeError),
        # A pip-audit release that renames the key must not read as "no findings"
        ('{"deps": [], "fixes": []}', KeyError),
    ],
)
def test_an_unusable_report_raises(tmp_path: Path, report: str, expected: type[Exception]) -> None:
    """An unreadable audit must fail loudly; the workflow gates on a non-zero exit."""
    audit = tmp_path / "audit.json"
    audit.write_text(report)

    with pytest.raises(expected):
        main([str(audit), str(tmp_path / "missing.txt")])


def test_a_missing_report_raises(tmp_path: Path) -> None:
    """pip-audit writing no report at all must fail loudly rather than pass."""
    with pytest.raises(FileNotFoundError):
        main([str(tmp_path / "missing.json"), str(tmp_path / "missing.txt")])
