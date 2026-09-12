"""Repository-level consistency tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if not (PROJECT_ROOT / "pyproject.toml").is_file():
    pytest.skip("standalone provider metadata is not synced upstream", allow_module_level=True)

EXPECTED_RUNTIME_REQUIREMENTS = {
    "segno==1.6.6",
    "ya-passport-auth[ma]==2.0.1",
}


def test_runtime_requirements_match_manifest() -> None:
    """Package and provider metadata declare the same runtime dependencies."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    with (PROJECT_ROOT / "provider" / "manifest.json").open(encoding="utf-8") as file:
        manifest = json.load(file)
    with (PROJECT_ROOT / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)

    assert set(project["project"]["dependencies"]) == EXPECTED_RUNTIME_REQUIREMENTS
    assert set(manifest["requirements"]) == EXPECTED_RUNTIME_REQUIREMENTS
    locked_versions = {
        package["name"]: package["version"] for package in lock["package"] if "version" in package
    }
    assert locked_versions["segno"] == "1.6.6"
    assert locked_versions["ya-passport-auth"] == "2.0.1"
