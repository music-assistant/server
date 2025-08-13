"""Common fixtures and configuration for nicovideo tests."""

from __future__ import annotations

import pytest

from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager
from tests.providers.nicovideo.fixtures.mappings import (
    FixtureTestMappingRegistry,
)
from tests.providers.nicovideo.helpers import create_converter_manager

from .constants import GENERATED_FIXTURES_DIR
from .fixtures import FixtureManager


@pytest.fixture
def fixture_manager() -> FixtureManager:
    """Provide a FixtureManager instance."""
    return FixtureManager(GENERATED_FIXTURES_DIR)


@pytest.fixture
def converter_manager() -> NicovideoConverterManager:
    """Provide a NicovideoConverterManager instance."""
    return create_converter_manager()


@pytest.fixture
def mapping_registry() -> FixtureTestMappingRegistry:
    """Provide a FixtureTestMappingRegistry."""
    return FixtureTestMappingRegistry()
