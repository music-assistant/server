"""Common fixtures and configuration for nicovideo tests."""

from __future__ import annotations

import pytest

from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager
from tests.providers.nicovideo.constants import GENERATED_FIXTURES_DIR
from tests.providers.nicovideo.fixtures.api_response_converter_mapping import (
    APIResponseConverterMappingRegistry,
)
from tests.providers.nicovideo.fixtures.fixture_loader import FixtureLoader
from tests.providers.nicovideo.helpers import create_converter_manager


@pytest.fixture
def fixture_loader() -> FixtureLoader:
    """Provide a FixtureLoader instance."""
    return FixtureLoader(GENERATED_FIXTURES_DIR)


@pytest.fixture
def converter_manager() -> NicovideoConverterManager:
    """Provide a NicovideoConverterManager instance."""
    return create_converter_manager()


@pytest.fixture
def mapping_registry() -> APIResponseConverterMappingRegistry:
    """Provide an APIResponseConverterMappingRegistry."""
    return APIResponseConverterMappingRegistry()
