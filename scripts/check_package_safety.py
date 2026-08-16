#!/usr/bin/env python3
"""
Check PyPI package metadata for security and supply chain concerns.

This script checks new or updated Python dependencies for suspicious indicators
that might suggest supply chain attacks or unmaintained packages.
"""

# ruff: noqa: T201, S310, RUF001, PLR0915
import json
import re
import sys
import urllib.request
from datetime import datetime
from typing import Any

# Complete spellings of compatible licenses, as packages write them in the free-form license
# field and as PyPI names them in its `License ::` classifiers. A value has to be one of these
# in full: a name occurring somewhere inside it says nothing about the terms around it.
COMPATIBLE_LICENSE_NAMES = {
    # names of the PyPI classifiers, which are a closed vocabulary
    "Apache Software License",
    "Boost Software License 1.0 (BSL-1.0)",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
    "CMU License (MIT-CMU)",
    "GNU Lesser General Public License v2 (LGPLv2)",
    "GNU Lesser General Public License v2 or later (LGPLv2+)",
    "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU Lesser General Public License v3 or later (LGPLv3+)",
    "GNU Library or Lesser General Public License (LGPL)",
    "Historical Permission Notice and Disclaimer (HPND)",
    "ISC License (ISCL)",
    "MIT No Attribution License (MIT-0)",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "Public Domain",
    "Python Software Foundation License",
    "The Unlicense (Unlicense)",
    "Universal Permissive License (UPL)",
    "Zero-Clause BSD (0BSD)",
    "zlib/libpng License",
    # spellings of the same licenses used in the free-form field. Only versioned names of the
    # licenses whose versions differ in what they allow, so that an unversioned "Apache" cannot
    # stand in for Apache-1.1 or an unversioned "MPL" for the reciprocal MPL-1.1
    "2-Clause BSD",
    "3-Clause BSD",
    "Apache 2",
    "Apache 2.0",
    "Apache License 2.0",
    "Apache License, Version 2.0",
    "Apache Software License 2.0",
    "BSD",
    "BSD-2",
    "BSD-2-Clause",
    "BSD-3",
    "BSD-3-Clause",
    "CC0",
    "CC0 1.0",
    "CC0 1.0 Universal",
    "Expat",
    "ISC",
    "ISCL",
    "LGPL",
    "LGPLv2",
    "LGPLv3",
    "MIT",
    "MPL 2.0",
    "Modified BSD",
    "New BSD",
    "PSF",
    "PSFL",
    "Simplified BSD",
    "The MIT License (MIT)",
    "Unlicense",
    "Zlib",
}

# The grant each permissive license spells out, used to recognise packages that put their whole
# license text in the license field. The grant is what the license actually gives, so it
# identifies the text where the heading above it would only be a name in prose. Each is quoted
# far enough to cover the permission itself, so that a text restricting it reads differently.
LICENSE_TEXT_GRANTS = {
    # MIT
    "Permission is hereby granted, free of charge, to any person obtaining a copy of this"
    " software and associated documentation files",
    # ISC
    "Permission to use, copy, modify, and/or distribute this software for any purpose with or"
    " without fee is hereby granted",
    # BSD, 2-clause and 3-clause alike
    "Redistribution and use in source and binary forms, with or without modification, are"
    " permitted provided that the following conditions are met",
    # Apache-2.0
    'Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file'
    " except in compliance with the License",
    # MPL-2.0
    "This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a"
    " copy of the MPL was not distributed with this file",
    # Unlicense
    "This is free and unencumbered software released into the public domain. Anyone is free to"
    " copy, modify, publish, use, compile, sell, or distribute this software",
    # Zlib
    "Permission is granted to anyone to use this software for any purpose, including commercial"
    " applications, and to alter it and redistribute it freely",
}

# SPDX identifiers accepted in a PEP 639 `license_expression`
COMPATIBLE_SPDX_LICENSES = {
    "0BSD",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "BSL-1.0",
    "CC0-1.0",
    "ISC",
    "LGPL-2.0",
    "LGPL-2.0-ONLY",
    "LGPL-2.0-OR-LATER",
    "LGPL-2.1",
    "LGPL-2.1-ONLY",
    "LGPL-2.1-OR-LATER",
    "LGPL-3.0",
    "LGPL-3.0-ONLY",
    "LGPL-3.0-OR-LATER",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "MPL-2.0",
    "PSF-2.0",
    "PYTHON-2.0",
    "UNLICENSE",
    "ZLIB",
}

# License families that are incompatible with the project (LGPL excluded)
PROBLEMATIC_LICENSES = ("GPL", "AGPL", "SSPL")

# Deepest group nesting accepted in an SPDX expression
MAX_SPDX_NESTING = 10

# Common packages to check for typosquatting (popular Python packages)
POPULAR_PACKAGES = {
    "requests",
    "urllib3",
    "setuptools",
    "certifi",
    "pip",
    "numpy",
    "pandas",
    "boto3",
    "botocore",
    "awscli",
    "django",
    "flask",
    "sqlalchemy",
    "pytest",
    "pydantic",
    "aiohttp",
    "fastapi",
}


def check_typosquatting(package_name: str) -> str | None:
    """
    Check if package name might be typosquatting a popular package.

    :param package_name: The package name to check.
    """
    package_lower = package_name.lower().replace("-", "").replace("_", "")

    for popular in POPULAR_PACKAGES:
        popular_normalized = popular.lower().replace("-", "").replace("_", "")

        # Check for common typosquatting techniques
        if package_lower == popular_normalized:
            continue  # Exact match is fine

        # Check edit distance (1-2 character changes)
        if len(package_lower) == len(popular_normalized):
            differences = sum(
                c1 != c2 for c1, c2 in zip(package_lower, popular_normalized, strict=True)
            )
            if differences == 1:
                return f"Suspicious: Very similar to popular package '{popular}'"

        # Check for common substitutions
        substitutions = [
            ("0", "o"),
            ("1", "l"),
            ("1", "i"),
        ]
        for old, new in substitutions:
            if old in package_lower:
                test_name = package_lower.replace(old, new)
                if test_name == popular_normalized:
                    return f"Suspicious: Character substitution of popular package '{popular}'"

    return None


def get_package_license(info: dict[str, Any]) -> tuple[str, bool]:
    """
    Resolve the license of a package from its PyPI metadata.

    Returns the license and whether it is a PEP 639 SPDX expression.

    :param info: The `info` section of the PyPI JSON response.
    """
    # packages that adopted PEP 639 declare an SPDX expression, which is more precise than the
    # free-form field and the classifiers, and is usually the only license metadata they carry
    if license_expression := (info.get("license_expression") or "").strip():
        return license_expression, True

    if license_str := (info.get("license") or "").strip():
        return license_str, False

    license_classifiers = []
    for classifier in info.get("classifiers") or []:
        # the license itself is the last segment, e.g. "License :: OSI Approved :: MIT License",
        # except for "License :: OSI Approved", which names no license at all
        parts = [part.strip() for part in classifier.split("::")]
        if parts[0] == "License" and len(parts) > 1 and parts[-1] != "OSI Approved":
            license_classifiers.append(parts[-1])

    if license_classifiers:
        # classifiers do not say how they relate to each other, so report the one that fails the
        # compatibility check rather than letting a permissive entry hide a copyleft one
        return next(
            (name for name in license_classifiers if not check_license_compatibility(name)[0]),
            license_classifiers[0],
        ), False

    return "Unknown", False


def check_license_compatibility(
    license_str: str, spdx_expression: bool = False
) -> tuple[bool, str]:
    """
    Check if license is compatible with the project.

    :param license_str: The license string from PyPI.
    :param spdx_expression: Whether the string is a PEP 639 SPDX expression.
    """
    if not license_str or license_str == "Unknown":
        return False, "No license information"

    license_upper = license_str.upper()
    spdx_compatible = _evaluate_spdx_expression(license_str)

    if spdx_compatible:
        return True, f"Compatible ({license_str})"

    copyleft = _is_copyleft(license_upper)

    # whatever the evaluator did not accept in an expression names a license that is not on the
    # allow list, so reading such a value further would only weaken the check. A free-form field
    # is all we have to go on, but what we recognise in it must not end up approving a copyleft
    # license standing next to it, and a "LicenseRef-" value names a custom license whatever
    # wording is packed into the identifier
    custom = "LICENSEREF" in license_upper
    if spdx_compatible is None and not copyleft and not custom and not spdx_expression:
        # a free-form field holds either the name of the license or the text of it; a name says
        # nothing about terms written around it, and a text is read on the grant it spells out
        if _names_license(license_str) or _quotes_license_text(license_str):
            return True, f"Compatible ({license_str})"

    if copyleft:
        return False, f"Incompatible copyleft license ({license_str})"

    # Unknown license
    return False, f"Unknown/unverified license ({license_str})"


def parse_requirement(line: str) -> str | None:
    """
    Extract package name from a requirement line.

    :param line: A line from requirements.txt (e.g., "package==1.0.0" or "package>=1.0")
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Handle various requirement formats
    # package==1.0.0, package>=1.0, package[extra]>=1.0, etc.
    match = re.match(r"^([a-zA-Z0-9_-]+)", line)
    if match:
        return match.group(1).lower()
    return None


def get_pypi_metadata(package_name: str) -> dict[str, Any] | None:
    """
    Fetch package metadata from PyPI JSON API.

    :param package_name: The name of the package to check.
    """
    url = f"https://pypi.org/pypi/{package_name}/json"

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            print(f"❌ Package '{package_name}' not found on PyPI")
        else:
            print(f"⚠️  Error fetching metadata for '{package_name}': {err}")
        return None
    except Exception as err:
        print(f"⚠️  Error fetching metadata for '{package_name}': {err}")
        return None


def check_package(package_name: str) -> dict[str, Any]:
    """
    Check a single package for security concerns.

    :param package_name: The name of the package to check.
    """
    data = get_pypi_metadata(package_name)

    if not data:
        return {
            "name": package_name,
            "error": "Could not fetch package metadata",
            "risk_level": "unknown",
            "warnings": [],
        }

    info = data.get("info", {})
    releases = data.get("releases", {})

    # Get package age
    upload_times = []
    for release_files in releases.values():
        if release_files:
            for file_info in release_files:
                if "upload_time" in file_info:
                    try:
                        upload_time_str = file_info["upload_time"]
                        # Handle both formats: with 'Z' suffix or with timezone
                        if upload_time_str.endswith("Z"):
                            upload_time_str = upload_time_str[:-1] + "+00:00"
                        upload_time = datetime.fromisoformat(upload_time_str)
                        upload_times.append(upload_time)
                    except ValueError, AttributeError:
                        continue

    first_upload = min(upload_times) if upload_times else None
    age_days = (datetime.now(first_upload.tzinfo) - first_upload).days if first_upload else 0

    # Extract metadata
    project_urls = info.get("project_urls") or {}
    homepage = info.get("home_page") or project_urls.get("Homepage")
    source = project_urls.get("Source") or project_urls.get("Repository")

    # Run automated security checks
    typosquat_check = check_typosquatting(package_name)
    package_license, spdx_expression = get_package_license(info)
    license_compatible, license_status = check_license_compatibility(
        package_license, spdx_expression
    )

    checks = {
        "name": package_name,
        "version": info.get("version", "unknown"),
        "age_days": age_days,
        "total_releases": len(releases),
        "has_homepage": bool(homepage),
        "has_source": bool(source),
        "author": info.get("author") or info.get("maintainer") or "Unknown",
        "license": package_license,
        "summary": info.get("summary", "No description"),
        "warnings": [],
        "info_items": [],
        "risk_level": "low",
        "automated_checks": {
            "trusted_source": bool(source),
            "typosquatting": typosquat_check is None,
            "license_compatible": license_compatible,
        },
        "check_details": {
            "typosquatting": typosquat_check or "✓ No typosquatting detected",
            "license": license_status,
        },
    }

    # Check for suspicious indicators
    risk_score = 0

    # Typosquatting check
    if typosquat_check:
        checks["warnings"].append(typosquat_check)
        risk_score += 5  # High risk

    # License check
    if not license_compatible:
        checks["warnings"].append(f"License issue: {license_status}")
        risk_score += 2

    if age_days < 30:
        checks["warnings"].append(f"Very new package (only {age_days} days old)")
        risk_score += 3
    elif age_days < 90:
        checks["warnings"].append(f"Relatively new package ({age_days} days old)")
        risk_score += 1

    if checks["total_releases"] < 3:
        checks["warnings"].append(f"Very few releases (only {checks['total_releases']})")
        risk_score += 2

    if not source:
        checks["warnings"].append("No source repository linked")
        risk_score += 2

    if not homepage and not source:
        checks["warnings"].append("No homepage or source repository")
        risk_score += 1

    if checks["author"] == "Unknown":
        checks["warnings"].append("No author information available")
        risk_score += 1

    # Add informational items
    checks["info_items"].append(f"Age: {age_days} days")
    checks["info_items"].append(f"Releases: {checks['total_releases']}")
    checks["info_items"].append(f"Author: {checks['author']}")
    checks["info_items"].append(f"License: {checks['license']}")
    if source:
        checks["info_items"].append(f"Source: {source}")

    # Determine risk level
    if risk_score >= 5:
        checks["risk_level"] = "high"
    elif risk_score >= 3:
        checks["risk_level"] = "medium"
    else:
        checks["risk_level"] = "low"

    return checks


def format_check_result(result: dict[str, Any]) -> str:
    """
    Format a check result for display.

    :param result: The check result dictionary.
    """
    risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢", "unknown": "⚪"}
    version = result.get("version", "unknown")

    lines = [f"\n{risk_emoji[result['risk_level']]} **{result['name']}** (v{version})"]

    if result.get("error"):
        lines.append(f"  ❌ {result['error']}")
        return "\n".join(lines)

    if result.get("summary"):
        lines.append(f"  📝 {result['summary']}")

    if result.get("info_items"):
        for item in result["info_items"]:
            lines.append(f"  ℹ️  {item}")

    if result.get("warnings"):
        for warning in result["warnings"]:
            lines.append(f"  ⚠️  {warning}")

    return "\n".join(lines)


def main() -> int:
    """Run the package safety check."""
    if len(sys.argv) < 2:
        print("Usage: check_package_safety.py <requirements_file_or_package_name>")
        print("  Or: check_package_safety.py package1 package2 package3")
        return 1

    packages = []

    # Check if first argument is a file
    if len(sys.argv) == 2 and sys.argv[1].endswith(".txt"):
        try:
            with open(sys.argv[1]) as f:
                for line in f:
                    package = parse_requirement(line)
                    if package:
                        packages.append(package)
        except FileNotFoundError:
            print(f"Error: File '{sys.argv[1]}' not found")
            return 1
    else:
        # Treat arguments as package names
        packages = [arg.lower() for arg in sys.argv[1:]]

    if not packages:
        print("No packages to check")
        return 0

    print(f"Checking {len(packages)} package(s)...\n")
    print("=" * 80)

    results = []
    for package in packages:
        result = check_package(package)
        results.append(result)
        print(format_check_result(result))

    print("\n" + "=" * 80)

    # Automated checks summary
    all_trusted = all(r.get("automated_checks", {}).get("trusted_source", False) for r in results)
    all_no_typosquat = all(
        r.get("automated_checks", {}).get("typosquatting", False) for r in results
    )
    all_license_ok = all(
        r.get("automated_checks", {}).get("license_compatible", False) for r in results
    )

    print("\n🤖 Automated Security Checks:")
    trusted_msg = (
        "All packages have source repositories"
        if all_trusted
        else "Some packages missing source info"
    )
    print(f"  {'✅' if all_trusted else '❌'} Trusted Sources: {trusted_msg}")

    typosquat_msg = (
        "No suspicious package names detected"
        if all_no_typosquat
        else "Possible typosquatting detected!"
    )
    print(f"  {'✅' if all_no_typosquat else '❌'} Typosquatting: {typosquat_msg}")

    license_msg = (
        "All licenses are compatible" if all_license_ok else "Some license issues detected"
    )
    print(f"  {'✅' if all_license_ok else '⚠️ '} License Compatibility: {license_msg}")

    # Summary
    high_risk = sum(1 for r in results if r["risk_level"] == "high")
    medium_risk = sum(1 for r in results if r["risk_level"] == "medium")
    low_risk = sum(1 for r in results if r["risk_level"] == "low")

    print(f"\n📊 Summary: {len(results)} packages checked")
    if high_risk:
        print(f"  🔴 High risk: {high_risk}")
    if medium_risk:
        print(f"  🟡 Medium risk: {medium_risk}")
    print(f"  🟢 Low risk: {low_risk}")

    if high_risk > 0:
        print("\n⚠️  High-risk packages detected! Manual review strongly recommended.")
        return 2
    if medium_risk > 0:
        print("\n⚠️  Medium-risk packages detected. Please review before merging.")
        return 1

    print("\n✅ All packages passed basic safety checks.")
    return 0


class _SpdxSyntaxError(Exception):
    """Raised when a string does not follow the SPDX expression grammar."""


def _evaluate_spdx_expression(license_str: str) -> bool | None:
    """
    Check an SPDX license expression (PEP 639) against the allow list.

    Returns whether the expression is compatible, which is None when it names a license that is
    neither known-compatible nor known-problematic or when the string is not an expression at all.

    :param license_str: The license string to evaluate, e.g. "MIT OR Apache-2.0".
    """
    tokens = re.findall(r"\(|\)|[^\s()]+", license_str)
    if not tokens:
        return None
    # real expressions nest a group or two at most; refuse anything deeper rather than recursing
    # into it, and refuse outright as the fallback would match on a name nested inside
    if _max_group_depth(tokens) > MAX_SPDX_NESTING:
        return False

    remaining = list(tokens)
    try:
        result = _evaluate_spdx_tokens(remaining)
        if remaining:
            # anything left over means we did not understand the string as a whole
            raise _SpdxSyntaxError
    except _SpdxSyntaxError:
        result = None

    return result


def _evaluate_spdx_tokens(tokens: list[str]) -> bool | None:
    """
    Evaluate the leading SPDX expression, consuming the tokens it covers.

    Any alternative that is compatible makes the whole expression compatible.

    :param tokens: The remaining tokens of the expression.
    """
    result = _evaluate_spdx_term(tokens)
    while tokens and tokens[0].upper() == "OR":
        tokens.pop(0)
        term = _evaluate_spdx_term(tokens)
        if True in (result, term):
            result = True
        elif None in (result, term):
            result = None
    return result


def _evaluate_spdx_term(tokens: list[str]) -> bool | None:
    """
    Evaluate the leading "AND" sequence, which binds tighter than "OR" in SPDX.

    Every operand of the sequence has to be compatible on its own.

    :param tokens: The remaining tokens of the expression.
    """
    result = _evaluate_spdx_operand(tokens)
    while tokens and tokens[0].upper() == "AND":
        tokens.pop(0)
        operand = _evaluate_spdx_operand(tokens)
        if False in (result, operand):
            result = False
        elif None in (result, operand):
            result = None
    return result


def _evaluate_spdx_operand(tokens: list[str]) -> bool | None:
    """
    Evaluate a single SPDX operand: a parenthesised expression or a license identifier.

    :param tokens: The remaining tokens of the expression.
    """
    if not tokens or _is_spdx_operator(tokens[0]):
        raise _SpdxSyntaxError

    if tokens[0] == "(":
        tokens.pop(0)
        result = _evaluate_spdx_tokens(tokens)
        if not tokens or tokens.pop(0) != ")":
            raise _SpdxSyntaxError
        return result

    # a single trailing "+" is the deprecated "or later" marker and does not change the license
    identifier = tokens.pop(0).upper().removesuffix("+")
    # a "WITH <exception>" suffix only grants extra permissions, so the identifier decides
    if tokens and tokens[0].upper() == "WITH":
        tokens.pop(0)
        if not tokens or _is_spdx_operator(tokens[0]):
            raise _SpdxSyntaxError
        # ...but a license we refuse is not an exception, whatever it is written behind
        if _is_copyleft(tokens.pop(0).upper()):
            return False

    return True if identifier in COMPATIBLE_SPDX_LICENSES else None


def _names_license(license_str: str) -> bool:
    """
    Return whether a license string is, as a whole, one or more licenses we accept.

    :param license_str: The license string to read, e.g. "MIT License".
    """
    known = {_license_words(name) for name in COMPATIBLE_LICENSE_NAMES}
    if _license_words(license_str) in known:
        return True

    # a value naming several licenses, whether it combines them or offers a choice, is accepted
    # only when every one of them is. The whole value is matched first, so that a name holding a
    # separator is not taken apart into pieces that name nothing
    names = [
        _license_words(name) for name in re.split(r"[,/]|\bor\b", license_str, flags=re.IGNORECASE)
    ]
    return known.issuperset(names)


def _quotes_license_text(license_str: str) -> bool:
    """
    Return whether a license string spells out the text of a license we accept.

    :param license_str: The license string to read.
    """
    words = f" {_license_words(license_str)} "
    return any(f" {_license_words(grant)} " in words for grant in LICENSE_TEXT_GRANTS)


def _license_words(value: str) -> str:
    """
    Return the words of a license string as one key, so spelling variants compare equal.

    :param value: The license string to normalise.
    """
    # separators are spelled inconsistently ("MPL 2.0" for "MPL-2.0"), "licence" and "license"
    # are the same word, and both a leading "The" and a trailing "License" are noise the same
    # name is written with and without. Words in any script count, so that a term we cannot read
    # keeps the value from matching a name rather than dropping out of it
    words = re.findall(r"[^\W_]+", value.upper().replace("LICENCE", "LICENSE"))
    if words[:1] == ["THE"]:
        del words[0]
    if words[-1:] == ["LICENSE"]:
        del words[-1]
    return " ".join(words)


def _is_spdx_operator(token: str) -> bool:
    """
    Return whether a token joins or closes expressions instead of naming a license.

    :param token: The token to inspect.
    """
    return token == ")" or token.upper() in ("AND", "OR", "WITH")


def _is_copyleft(license_upper: str) -> bool:
    """
    Return whether an upper-cased license string names a copyleft family we do not accept.

    :param license_upper: The upper-cased license string to inspect.
    """
    # drop the LGPL mentions first, so that a GPL term next to an LGPL one is still spotted
    without_lgpl = license_upper.replace("LGPL", "")
    return any(problem in without_lgpl for problem in PROBLEMATIC_LICENSES)


def _max_group_depth(tokens: list[str]) -> int:
    """
    Return how deeply the parentheses in an expression nest.

    :param tokens: The tokens of the expression.
    """
    depth = 0
    deepest = 0
    for token in tokens:
        if token == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif token == ")":
            depth = max(0, depth - 1)
    return deepest


if __name__ == "__main__":
    sys.exit(main())
