"""The demo plugin provider's recommendation method stubs."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import UniqueList

from music_assistant.providers._demo_plugin_provider import (
    SUPPORTED_FEATURES,
    MyDemoPluginprovider,
)


@pytest.fixture
def demo_plugin_provider() -> MyDemoPluginprovider:
    """Construct a minimal demo plugin provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.PLUGIN
    manifest.domain = "demo_plugin"
    config = MagicMock()
    config.name = "Demo Plugin Provider"
    config.instance_id = "demo_plugin_instance"
    config.get_value = MagicMock(return_value="GLOBAL")
    return MyDemoPluginprovider(mass, manifest, config, SUPPORTED_FEATURES)


async def test_get_recommendations_returns_no_rows(
    demo_plugin_provider: MyDemoPluginprovider,
) -> None:
    """The template's get_recommendations() returns no rows."""
    assert await demo_plugin_provider.get_recommendations() == []


async def test_get_recommendation_items_returns_empty(
    demo_plugin_provider: MyDemoPluginprovider,
) -> None:
    """The template's get_recommendation_items() returns an empty UniqueList."""
    items = await demo_plugin_provider.get_recommendation_items("unknown_row")
    assert isinstance(items, UniqueList)
    assert items == []
