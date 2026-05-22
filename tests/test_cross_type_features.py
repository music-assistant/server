"""Tests for cross-type feature dispatch on MusicAssistant."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.media.artists import ArtistsController
from music_assistant.controllers.media.tracks import TracksController
from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider


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


async def test_similar_artists_uses_music_provider_first() -> None:
    """Similar artists should prefer a music provider mapped to the artist."""
    mass = Mock()
    music_prov = Mock(spec=MusicProvider)
    music_prov.instance_id = "m_a"
    music_prov.type = ProviderType.MUSIC
    music_prov.available = True
    music_prov.supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    music_prov.get_similar_artists = AsyncMock(return_value=["a1"])
    mass.get_provider.return_value = music_prov
    mass.get_providers_supporting_feature.return_value = []

    ref_item = Mock()
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="artist_123",
            provider_domain="m_a",
            provider_instance="m_a",
            available=True,
        )
    ]

    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = mass
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    result = await controller.similar_artists("artist_123", "m_a", limit=5)

    assert result == ["a1"]
    music_prov.get_similar_artists.assert_awaited_once_with(prov_artist_id="artist_123", limit=5)


async def test_similar_artists_falls_back_to_metadata_provider() -> None:
    """Falls through to metadata-tier provider when music provider doesn't support it."""
    mass = Mock()
    music_prov = Mock(spec=MusicProvider)
    music_prov.instance_id = "m_a"
    music_prov.type = ProviderType.MUSIC
    music_prov.available = True
    music_prov.supported_features = set()
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.instance_id = "meta_a"
    metadata_prov.type = ProviderType.METADATA
    metadata_prov.available = True
    metadata_prov.supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    metadata_prov.priority = 50
    metadata_prov.get_similar_artists = AsyncMock(return_value=["a2"])
    mass.get_provider.return_value = music_prov
    mass.get_providers_supporting_feature.return_value = [metadata_prov]

    ref_item = Mock()
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="artist_123",
            provider_domain="m_a",
            provider_instance="m_a",
            available=True,
        )
    ]

    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = mass
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    result = await controller.similar_artists("artist_123", "m_a", limit=5)

    assert result == ["a2"]
    metadata_prov.get_similar_artists.assert_awaited_once_with(ref_item, limit=5)


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
