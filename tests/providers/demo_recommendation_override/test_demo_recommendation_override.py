"""The demo music provider's recommendations implementation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.providers._demo_music_provider import (
    SUPPORTED_FEATURES,
    MyDemoMusicprovider,
)


@pytest.fixture
def demo_music_provider() -> MyDemoMusicprovider:
    """Construct a minimal demo provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "demo"
    config = MagicMock()
    config.name = "Demo Provider"
    config.instance_id = "demo_instance"
    config.get_value = MagicMock(return_value="GLOBAL")
    return MyDemoMusicprovider(mass, manifest, config, SUPPORTED_FEATURES)


async def test_get_recommendations_returns_empty_list(
    demo_music_provider: MyDemoMusicprovider,
) -> None:
    """The demo provider's get_recommendations() returns an empty list."""
    assert await demo_music_provider.get_recommendations() == []


async def test_get_recommendation_items_returns_empty_list(
    demo_music_provider: MyDemoMusicprovider,
) -> None:
    """The demo provider's get_recommendation_items() returns an empty list for any id."""
    assert await demo_music_provider.get_recommendation_items("any_row") == []
