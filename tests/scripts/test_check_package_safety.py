"""Tests for the license checks of the package safety script."""

from __future__ import annotations

from typing import Any

import pytest

from scripts import check_package_safety
from scripts.check_package_safety import (
    check_license_compatibility,
    check_package,
    get_package_license,
)


def pypi_info(**overrides: Any) -> dict[str, Any]:
    """
    Return the `info` section of a PyPI JSON response.

    :param overrides: Fields to set on top of the (empty) license metadata.
    """
    return {"license": None, "license_expression": None, "classifiers": [], **overrides}


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        # PEP 639: the SPDX expression is the only license metadata (chardet 7.6.0)
        (pypi_info(license_expression="0BSD"), "0BSD"),
        (pypi_info(license_expression="MIT OR Apache-2.0"), "MIT OR Apache-2.0"),
        # the SPDX expression wins over the less precise legacy field and classifiers
        (
            pypi_info(
                license_expression="AGPL-3.0-only",
                classifiers=["License :: OSI Approved :: GNU Affero General Public License v3"],
            ),
            "AGPL-3.0-only",
        ),
        # packages without an SPDX expression fall back to those
        (pypi_info(license="Apache-2.0"), "Apache-2.0"),
        (pypi_info(classifiers=["License :: OSI Approved :: MIT License"]), "MIT License"),
        (
            pypi_info(
                license="Apache-2.0",
                classifiers=["License :: OSI Approved :: Apache Software License"],
            ),
            "Apache-2.0",
        ),
        (
            pypi_info(classifiers=["Programming Language :: Python", "License :: OSI Approved"]),
            "Unknown",
        ),
        # several classifiers: the one that fails the check decides, whatever its position
        (
            pypi_info(
                classifiers=[
                    "License :: OSI Approved :: MIT License",
                    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
                ]
            ),
            "GNU General Public License v3 (GPLv3)",
        ),
        (
            pypi_info(
                classifiers=[
                    "License :: OSI Approved :: Apache Software License",
                    "License :: OSI Approved :: MIT License",
                ]
            ),
            "Apache Software License",
        ),
        # nothing at all to go on
        (pypi_info(), "Unknown"),
        (pypi_info(license="   "), "Unknown"),
    ],
)
def test_get_package_license(info: dict[str, Any], expected: str) -> None:
    """Test the license is resolved from any of the fields PyPI exposes it in."""
    assert get_package_license(info) == expected


@pytest.mark.parametrize(
    "license_str",
    [
        # SPDX identifiers as used in a PEP 639 expression
        "0BSD",
        "MIT",
        "MIT-0",
        "Apache-2.0",
        "BSD-3-Clause",
        "MPL-2.0",
        "LGPL-2.1-or-later",
        "MIT OR Apache-2.0",
        "Apache-2.0 OR BSD-3-Clause",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "MPL-2.0 AND (Apache-2.0 OR MIT)",
        "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND MIT",
        # legacy license strings and classifier names keep working
        "BSD",
        "MIT License",
        "Apache Software License",
        "GNU Lesser General Public License v3 (LGPLv3)",
        # spelling variants of the same licenses
        "MPL 2.0",
        "Apache 2.0 License",
        # a custom license alongside one we accept still leaves a usable option
        "MIT OR LicenseRef-Proprietary",
    ],
)
def test_compatible_licenses(license_str: str) -> None:
    """Test permissive licenses are accepted."""
    compatible, status = check_license_compatibility(license_str)
    assert compatible, status


@pytest.mark.parametrize(
    ("license_str", "expected_status"),
    [
        ("GPL-3.0-only", "Incompatible copyleft license (GPL-3.0-only)"),
        ("AGPL-3.0-only", "Incompatible copyleft license (AGPL-3.0-only)"),
        # a permissive term must not mask a copyleft one it is combined with
        ("MIT AND GPL-3.0-only", "Incompatible copyleft license (MIT AND GPL-3.0-only)"),
        ("GNU General Public License v3 (GPLv3)", "Incompatible copyleft license"),
        ("Frobnicate-1.0", "Unknown/unverified license (Frobnicate-1.0)"),
        # a custom license is never pre-approved, not even when its name reads permissive
        (
            "LicenseRef-Proprietary-MIT-Terms",
            "Unknown/unverified license (LicenseRef-Proprietary-MIT-Terms)",
        ),
        ("Unknown", "No license information"),
        ("", "No license information"),
        # nesting deep enough to exhaust the stack is refused, not approved on the name inside
        ("(" * 333 + "MIT" + ")" * 333, "Unknown/unverified license"),
    ],
)
def test_incompatible_licenses(license_str: str, expected_status: str) -> None:
    """Test copyleft and unrecognised licenses are rejected."""
    compatible, status = check_license_compatibility(license_str)
    assert not compatible
    assert status.startswith(expected_status)


def test_check_package_reads_license_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test a package that only declares an SPDX expression passes the license check."""
    metadata = {
        "info": {
            "version": "7.6.0",
            "license": None,
            "license_expression": "0BSD",
            "classifiers": [],
            "author": "Dan Blanchard",
            "summary": "Universal encoding detector",
            "project_urls": {"Homepage": "https://github.com/chardet/chardet"},
        },
        "releases": {
            f"{major}.0.0": [{"upload_time": "2015-01-01T00:00:00"}] for major in range(1, 5)
        },
    }
    monkeypatch.setattr(check_package_safety, "get_pypi_metadata", lambda _: metadata)

    result = check_package("chardet")

    assert result["license"] == "0BSD"
    assert result["automated_checks"]["license_compatible"]
    assert result["check_details"]["license"] == "Compatible (0BSD)"
    assert not [warning for warning in result["warnings"] if "License" in warning]
