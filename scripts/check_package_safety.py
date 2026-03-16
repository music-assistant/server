#!/usr/bin/env python3
"""Check PyPI package metadata for security and supply chain concerns.

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

# OSI-approved and common compatible licenses
COMPATIBLE_LICENSES = {
    "MIT",
    "Apache-2.0",
    "Apache Software License",
    "BSD",
    "BSD-3-Clause",
    "BSD-2-Clause",
    "ISC",
    "Python Software Foundation License",
    "PSF",
    "LGPL",
    "MPL-2.0",
    "Unlicense",
    "CC0",
}

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
    """Check if package name might be typosquatting a popular package.

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


def check_license_compatibility(license_str: str) -> tuple[bool, str]:
    """Check if license is compatible with the project.

    :param license_str: The license string from PyPI.
    """
    if not license_str or license_str == "Unknown":
        return False, "No license information"

    license_upper = license_str.upper()

    # Check against compatible licenses
    for compatible in COMPATIBLE_LICENSES:
        if compatible.upper() in license_upper:
            return True, f"Compatible ({license_str})"

    # Check for problematic licenses
    problematic = ["GPL", "AGPL", "SSPL"]
    for problem in problematic:
        if problem in license_upper and "LGPL" not in license_upper:
            return False, f"Incompatible copyleft license ({license_str})"

    # Unknown license
    return False, f"Unknown/unverified license ({license_str})"


def parse_requirement(line: str) -> str | None:
    """Extract package name from a requirement line.

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
    """Fetch package metadata from PyPI JSON API.

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
    """Check a single package for security concerns.

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
                    except (ValueError, AttributeError):
                        continue

    first_upload = min(upload_times) if upload_times else None
    age_days = (datetime.now(first_upload.tzinfo) - first_upload).days if first_upload else 0

    # Extract metadata
    project_urls = info.get("project_urls") or {}
    homepage = info.get("home_page") or project_urls.get("Homepage")
    source = project_urls.get("Source") or project_urls.get("Repository")

    # Run automated security checks
    typosquat_check = check_typosquatting(package_name)
    license_compatible, license_status = check_license_compatibility(info.get("license", "Unknown"))

    checks = {
        "name": package_name,
        "version": info.get("version", "unknown"),
        "age_days": age_days,
        "total_releases": len(releases),
        "has_homepage": bool(homepage),
        "has_source": bool(source),
        "author": info.get("author") or info.get("maintainer") or "Unknown",
        "license": info.get("license") or "Unknown",
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
    """Format a check result for display.

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


if __name__ == "__main__":
    sys.exit(main())
