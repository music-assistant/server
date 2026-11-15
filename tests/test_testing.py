"""Tests for the pytest plugin a community provider tests itself with."""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

pytest_plugins = ["pytester"]

DOMAIN = "sample_plugin"
PROVIDER_CODE = """
from music_assistant.models.plugin import PluginProvider


async def setup(mass, manifest, config):
    return PluginProvider(mass, manifest, config)
"""
TEST_CODE = f"""
from music_assistant_models.enums import ProviderTier


async def test_provider_loads(mass):
    assert mass.get_provider_manifest("{DOMAIN}").tier == ProviderTier.COMMUNITY
"""


def test_provider_repository(pytester: pytest.Pytester) -> None:
    """A provider repository that enables the plugin tests its provider in a real server."""
    provider_dir = pytester.path / DOMAIN
    provider_dir.mkdir()
    manifest = {
        "type": "plugin",
        "domain": DOMAIN,
        "name": "Sample plugin",
        "description": "test provider",
        "codeowners": [],
        "requirements": [],
    }
    (provider_dir / "manifest.json").write_text(json.dumps(manifest))
    (provider_dir / "__init__.py").write_text(PROVIDER_CODE)
    pytester.makepyprojecttoml(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\ntestpaths = ["tests"]\n'
    )
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "conftest.py").write_text('pytest_plugins = ["music_assistant.testing"]\n')
    (tests_dir / "test_provider.py").write_text(TEST_CODE)

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
