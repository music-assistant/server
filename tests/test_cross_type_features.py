"""Tests for cross-type feature dispatch on MusicAssistant."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import (
    MediaType,
    ProviderFeature,
    ProviderSearchStatus,
    ProviderType,
)
from music_assistant_models.media_items import Artist, ProviderMapping, SearchResults

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.mass import MusicAssistant
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider


def _make_prov(
    instance_id: str,
    prov_type: ProviderType,
    features: set[ProviderFeature],
    priority: int = 50,
    available: bool = True,
) -> Mock:
    prov = Mock()
    prov.instance_id = instance_id
    prov.type = prov_type
    prov.available = available
    prov.supported_features = features
    prov.priority = priority
    return prov


def test_get_providers_supporting_feature_orders_by_type_then_priority() -> None:
    """Providers should be grouped by type tier then sorted by provider priority inside each tier."""
    mass = Mock(spec=MusicAssistant)
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=20)
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=10)
    meta_a = _make_prov("meta_a", ProviderType.METADATA, {ProviderFeature.SIMILAR_TRACKS})
    plugin_a = _make_prov("plug_a", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    unrelated = _make_prov("u", ProviderType.MUSIC, {ProviderFeature.SEARCH})
    mass.get_providers.return_value = [music_a, music_b, meta_a, plugin_a, unrelated]

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["m_b", "m_a", "meta_a", "plug_a"]


def test_get_providers_supporting_feature_skips_unavailable() -> None:
    """Unavailable providers should be skipped."""
    mass = Mock(spec=MusicAssistant)
    alive = _make_prov("a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    dead = _make_prov("d", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, available=False)
    mass.get_providers.return_value = [alive, dead]

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["a"]


def test_get_providers_supporting_feature_respects_custom_priority() -> None:
    """Caller can restrict / re-order tiers with the priority argument."""
    mass = Mock(spec=MusicAssistant)
    music = _make_prov("m", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    plugin = _make_prov("p", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    mass.get_providers.return_value = [music, plugin]

    result = MusicAssistant.get_providers_supporting_feature(
        mass,
        ProviderFeature.SIMILAR_TRACKS,
        priority=(ProviderType.PLUGIN,),
    )

    assert [p.instance_id for p in result] == ["p"]


async def test_similar_tracks_falls_back_to_metadata_provider() -> None:
    """When the music provider doesn't support SIMILAR_TRACKS, try metadata providers."""
    mass = Mock()
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.instance_id = "meta_a"
    metadata_prov.type = ProviderType.METADATA
    metadata_prov.available = True
    metadata_prov.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    metadata_prov.priority = 50
    metadata_prov.get_similar_tracks = AsyncMock(return_value=["t1", "t2"])
    music_prov = _make_prov("m_a", ProviderType.MUSIC, set())

    def get_provider(instance_id: str, **_: object) -> Mock | None:
        return {"m_a": music_prov, "meta_a": metadata_prov}.get(instance_id)

    mass.get_provider.side_effect = get_provider
    mass.get_providers_supporting_feature.return_value = [metadata_prov]

    ref_item = Mock()
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="abc",
            provider_domain="m_a",
            provider_instance="m_a",
            available=True,
        )
    ]

    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    result = await controller.similar_tracks("abc", "m_a", limit=5)

    assert result == ["t1", "t2"]
    metadata_prov.get_similar_tracks.assert_awaited_once()
    call_kwargs = metadata_prov.get_similar_tracks.await_args.kwargs
    assert call_kwargs.get("limit") == 5


def _artist(item_id: str, name: str, instance: str) -> Artist:
    """Build a minimal Artist for the given provider instance."""
    return Artist(
        item_id=item_id,
        provider=instance,
        name=name,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=instance, provider_instance=instance)
        },
    )


async def test_similar_artists_aggregates_across_providers() -> None:
    """Similar artists for a library artist aggregate (and dedupe) across all its providers."""
    mass = Mock()
    music_prov = Mock(spec=MusicProvider)
    music_prov.available = True
    music_prov.supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    music_prov.supports_feature = lambda feature: feature in music_prov.supported_features
    music_prov.get_similar_artists = AsyncMock(
        return_value=[_artist("a", "Artist A", "m_a"), _artist("b", "Artist B", "m_a")]
    )
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.instance_id = "meta_a"
    metadata_prov.get_similar_artists = AsyncMock(
        return_value=[_artist("b2", "Artist B", "meta_a"), _artist("c", "Artist C", "meta_a")]
    )
    mass.get_provider.return_value = music_prov
    mass.get_providers_supporting_feature.return_value = [metadata_prov]

    ref_item = Mock()
    ref_item.provider_mappings = [
        ProviderMapping(item_id="artist_123", provider_domain="m_a", provider_instance="m_a")
    ]

    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = mass
    controller.get_library_item = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    # keep provider items (not resolved to a library equivalent)
    controller.get_library_item_by_prov_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await controller.similar_artists("1", "library", limit=5)

    # interleaved by rank, deduped on name ("Artist B" appears in both providers)
    assert [artist.name for artist in result] == ["Artist A", "Artist B", "Artist C"]
    music_prov.get_similar_artists.assert_awaited_once_with("artist_123", limit=5)
    metadata_prov.get_similar_artists.assert_awaited_once_with(ref_item, limit=5)


async def test_similar_artists_provider_queries_named_provider() -> None:
    """Similar artists for a provider artist should query only that provider."""
    mass = Mock()
    music_prov = Mock(spec=MusicProvider)
    music_prov.name = "Music Provider A"
    music_prov.available = True
    music_prov.supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    music_prov.supports_feature = lambda feature: feature in music_prov.supported_features
    music_prov.get_similar_artists = AsyncMock(return_value=[_artist("x", "Artist X", "m_a")])
    mass.get_provider.return_value = music_prov

    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = mass
    # keep provider items (not resolved to a library equivalent)
    controller.get_library_item_by_prov_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await controller.similar_artists("artist_123", "m_a", limit=5)

    assert [artist.name for artist in result] == ["Artist X"]
    music_prov.get_similar_artists.assert_awaited_once_with("artist_123", limit=5)


async def test_browse_root_includes_non_music_providers() -> None:
    """Root browse should list every provider declaring BROWSE, regardless of type."""
    mass = Mock()
    music_prov = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_prov.domain = "music_a"
    music_prov.name = "Music A"
    plugin_prov = _make_prov("p_a", ProviderType.PLUGIN, {ProviderFeature.BROWSE})
    plugin_prov.domain = "plugin_a"
    plugin_prov.name = "Plugin A"

    # browse queries get_providers_supporting_feature twice: once for BROWSE, once
    # for AUDIO_SOURCE (to decide whether to inject the Live Inputs root entry).
    def _supports(feature: ProviderFeature) -> list[Mock]:
        if feature == ProviderFeature.BROWSE:
            return [music_prov, plugin_prov]
        return []

    mass.get_providers_supporting_feature.side_effect = _supports

    controller = MusicController.__new__(MusicController)
    controller.mass = mass

    result = await controller.browse(path=None)

    assert [folder.path for folder in result] == ["m_a://", "p_a://"]  # type: ignore[union-attr]
    calls = [
        c.args[0] if c.args else c.kwargs["feature"]
        for c in mass.get_providers_supporting_feature.call_args_list
    ]
    assert ProviderFeature.BROWSE in calls


async def test_global_search_includes_plugin_providers_with_search() -> None:
    """Plugins declaring SEARCH should be queried alongside music providers."""
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock(return_value=None)
    plugin_prov = _make_prov("plug_a", ProviderType.PLUGIN, {ProviderFeature.SEARCH})
    mass.get_providers_supporting_feature.return_value = [plugin_prov]

    controller = MusicController.__new__(MusicController)
    controller.mass = mass
    controller.domain = "music"
    controller.get_unique_providers = Mock(return_value=["m_a"])  # type: ignore[method-assign]
    controller.search_library = AsyncMock(return_value=SearchResults())  # type: ignore[method-assign]
    controller._search_provider = AsyncMock(  # type: ignore[method-assign]
        return_value=(SearchResults(), ProviderSearchStatus.COMPLETE)
    )

    with patch(
        "music_assistant.controllers.music.controller.sort_search_result",
        side_effect=lambda _query, items: items,
    ):
        await controller.search("foo", media_types=[MediaType.TRACK], limit=5)

    queried_provider_ids = sorted(
        call.args[1] for call in controller._search_provider.await_args_list
    )
    assert queried_provider_ids == ["m_a", "plug_a"]
    args, kwargs = mass.get_providers_supporting_feature.call_args
    assert (args[0] if args else kwargs["feature"]) == ProviderFeature.SEARCH
    assert kwargs.get("priority") == (ProviderType.PLUGIN,)


async def test_plugin_search_default_stub_contract() -> None:
    """PluginProvider.search returns empty unless SEARCH is declared, then raises NotImplementedError."""

    class _StubPlugin(PluginProvider):
        _features: set[ProviderFeature]

        @property
        def supported_features(self) -> set[ProviderFeature]:
            return self._features

    plugin = _StubPlugin.__new__(_StubPlugin)
    plugin._features = set()

    result = await plugin.search("foo", [MediaType.TRACK], limit=5)
    assert isinstance(result, SearchResults)
    assert not result.tracks
    assert not result.artists
    assert not result.albums

    plugin._features = {ProviderFeature.SEARCH}
    try:
        await plugin.search("foo", [MediaType.TRACK], limit=5)
    except NotImplementedError:
        pass
    else:
        msg = "expected NotImplementedError when SEARCH is declared but not overridden"
        raise AssertionError(msg)
