"""Tests for the capability mixins shared by the provider base classes."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import Artist

from music_assistant.models.media_capabilities import (
    AudioStreamMixin,
    MediaCatalogMixin,
    MusicDiscoveryMixin,
    RecommendationsMixin,
)
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider
from music_assistant.models.provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

ALL_MIXINS = {MediaCatalogMixin, RecommendationsMixin, MusicDiscoveryMixin, AudioStreamMixin}


def _make_provider[ProviderT: Provider](
    provider_cls: type[ProviderT],
    provider_type: ProviderType,
    supported_features: set[ProviderFeature] | None = None,
) -> ProviderT:
    """Construct a minimal provider instance of the given base class."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = provider_type
    manifest.domain = "test_provider"
    manifest.name = "Test Provider"
    config = MagicMock()
    config.name = "Test Provider"
    config.instance_id = "test_provider"
    config.get_value.return_value = "GLOBAL"
    return provider_cls(mass, manifest, config, supported_features)


@pytest.mark.parametrize(
    ("provider_cls", "expected"),
    [
        (MusicProvider, {MediaCatalogMixin, RecommendationsMixin, AudioStreamMixin}),
        (PluginProvider, ALL_MIXINS),
        (MetadataProvider, {RecommendationsMixin, MusicDiscoveryMixin}),
        (PlayerProvider, set()),
    ],
)
def test_base_classes_carry_their_capabilities(
    provider_cls: type[Provider], expected: set[type]
) -> None:
    """Each provider base class is an instance of exactly the mixins of its capabilities."""
    assert {mixin for mixin in ALL_MIXINS if issubclass(provider_cls, mixin)} == expected


async def test_discovery_defaults_follow_the_declared_features() -> None:
    """A related-items lookup is inert until its feature is declared, then it must be implemented."""
    artist = Artist(item_id="1", provider="test_provider", name="Artist", provider_mappings=set())
    plugin = _make_provider(PluginProvider, ProviderType.PLUGIN)
    assert await plugin.get_artist_toptracks(artist) == []
    assert await plugin.get_similar_artists(artist) == []

    plugin = _make_provider(PluginProvider, ProviderType.PLUGIN, {ProviderFeature.ARTIST_TOPTRACKS})
    with pytest.raises(NotImplementedError):
        await plugin.get_artist_toptracks(artist)


async def test_resolve_image_is_available_on_every_provider() -> None:
    """The image resolver lives on the base class and returns the path untouched by default."""
    player_provider = _make_provider(PlayerProvider, ProviderType.PLAYER)
    assert await player_provider.resolve_image("artwork/1.jpg") == "artwork/1.jpg"


def test_music_provider_keeps_its_own_browse() -> None:
    """The music provider's library-driven browse wins over the catalog stub."""
    assert "browse" in MusicProvider.__dict__


async def test_gated_defaults_on_a_music_provider() -> None:
    """A feature-gated method is inert until its feature is declared."""
    provider = _make_provider(MusicProvider, ProviderType.MUSIC)
    assert not (await provider.search("query", [MediaType.TRACK])).tracks
    assert await provider.get_recommendations() == []

    provider = _make_provider(MusicProvider, ProviderType.MUSIC, {ProviderFeature.SEARCH})
    with pytest.raises(NotImplementedError):
        await provider.search("query", [MediaType.TRACK])


async def test_audio_stream_stub_raises_before_yielding() -> None:
    """The stream stub stays an async generator and fails without emitting a chunk."""
    assert inspect.isasyncgenfunction(AudioStreamMixin.get_audio_stream)
    provider = _make_provider(PluginProvider, ProviderType.PLUGIN)
    stream = provider.get_audio_stream(cast("StreamDetails", MagicMock()))
    with pytest.raises(NotImplementedError):
        await anext(stream)
