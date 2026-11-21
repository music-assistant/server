"""Tests for cross-type feature dispatch on MusicAssistant."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientConnectionError
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Artist,
    AudioSource,
    BrowseFolder,
    ProviderMapping,
    SearchResults,
    Track,
)

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.constants import SIMILAR_ITEMS_LIMIT
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.controllers.players import PlayerController
from music_assistant.mass import MusicAssistant
from music_assistant.models.media_capabilities import AudioSourceMixin
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider
from music_assistant.models.provider import Provider
from tests.common import as_user, set_music_source_access


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


def _similar_track(item_id: str) -> Track:
    """Return a track as a provider lists it among the similar tracks."""
    return Track(item_id=item_id, provider="m_a", name=item_id, provider_mappings=set())


def _mass_with_empty_cache() -> Mock:
    """Return a mocked server whose cache holds nothing, as a listing reads it first."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mass.music.resolve_visible_provider = mass.get_provider
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return mass


def test_get_providers_supporting_feature_orders_by_type_then_priority() -> None:
    """Providers should be grouped by type tier then sorted by provider priority inside each tier."""
    mass = Mock(spec=MusicAssistant)
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=20)
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=10)
    meta_a = _make_prov("meta_a", ProviderType.METADATA, {ProviderFeature.SIMILAR_TRACKS})
    plugin_a = _make_prov("plug_a", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    unrelated = _make_prov("u", ProviderType.MUSIC, {ProviderFeature.SEARCH})
    mass.providers = [music_a, music_b, meta_a, plugin_a, unrelated]

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["m_b", "m_a", "meta_a", "plug_a"]


def test_get_providers_supporting_feature_skips_unavailable() -> None:
    """Unavailable providers should be skipped."""
    mass = Mock(spec=MusicAssistant)
    alive = _make_prov("a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    dead = _make_prov("d", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, available=False)
    mass.providers = [alive, dead]

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["a"]


def test_get_providers_supporting_feature_respects_custom_priority() -> None:
    """Caller can restrict / re-order tiers with the priority argument."""
    mass = Mock(spec=MusicAssistant)
    music = _make_prov("m", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    plugin = _make_prov("p", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    mass.providers = [music, plugin]

    result = MusicAssistant.get_providers_supporting_feature(
        mass,
        ProviderFeature.SIMILAR_TRACKS,
        priority=(ProviderType.PLUGIN,),
    )

    assert [p.instance_id for p in result] == ["p"]


async def test_similar_tracks_falls_back_to_metadata_provider() -> None:
    """When the music provider doesn't support SIMILAR_TRACKS, try metadata providers."""
    mass = _mass_with_empty_cache()
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.instance_id = "meta_a"
    metadata_prov.type = ProviderType.METADATA
    metadata_prov.available = True
    metadata_prov.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    metadata_prov.priority = 50
    metadata_prov.get_similar_tracks = AsyncMock(
        return_value=[_similar_track("t1"), _similar_track("t2")]
    )
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

    assert [track.item_id for track in result] == ["t1", "t2"]
    metadata_prov.get_similar_tracks.assert_awaited_once()
    call_kwargs = metadata_prov.get_similar_tracks.await_args.kwargs
    # the limit pages the listing, which holds what the provider is asked for
    assert call_kwargs.get("limit") == SIMILAR_ITEMS_LIMIT


def _cross_type_similar_tracks_controller(music_prov: Mock, cross_prov: Mock) -> TracksController:
    """Return a controller for a track mapped onto music_prov, with cross_prov registered."""
    mass = _mass_with_empty_cache()
    mass.get_provider.side_effect = lambda instance_id, **_: {"m_a": music_prov}.get(instance_id)
    mass.get_providers_supporting_feature.side_effect = lambda _feature, priority: [
        prov for prov in (cross_prov,) if prov.type in priority
    ]
    ref_item = Mock()
    ref_item.provider_mappings = [
        ProviderMapping(item_id="abc", provider_domain="m_a", provider_instance="m_a")
    ]
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    return controller


def _similar_tracks_music_prov() -> Mock:
    """Return a music provider that answers with similar track m1."""
    music_prov = Mock(spec=MusicProvider)
    music_prov.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    music_prov.get_similar_tracks = AsyncMock(return_value=[_similar_track("m1")])
    return music_prov


async def test_similar_tracks_prefers_plugin_over_music_provider() -> None:
    """A similarity plugin answers before the track's own music provider."""
    music_prov = _similar_tracks_music_prov()
    plugin_prov = Mock(spec=PluginProvider)
    plugin_prov.type = ProviderType.PLUGIN
    plugin_prov.get_similar_tracks = AsyncMock(return_value=[_similar_track("p1")])
    controller = _cross_type_similar_tracks_controller(music_prov, plugin_prov)

    result = await controller.similar_tracks("abc", "m_a")

    assert [track.item_id for track in result] == ["p1"]
    music_prov.get_similar_tracks.assert_not_awaited()


async def test_similar_tracks_prefers_music_provider_over_metadata_provider() -> None:
    """The track's own music provider answers before a metadata provider."""
    music_prov = _similar_tracks_music_prov()
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.type = ProviderType.METADATA
    metadata_prov.get_similar_tracks = AsyncMock(return_value=[_similar_track("x1")])
    controller = _cross_type_similar_tracks_controller(music_prov, metadata_prov)

    result = await controller.similar_tracks("abc", "m_a")

    assert [track.item_id for track in result] == ["m1"]
    metadata_prov.get_similar_tracks.assert_not_awaited()


async def test_similar_tracks_skips_failed_provider_mapping() -> None:
    """A transport failure from one mapping does not prevent trying the next provider."""
    mass = _mass_with_empty_cache()
    failed_prov = Mock(spec=MusicProvider)
    failed_prov.name = "Failed"
    failed_prov.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    failed_prov.get_similar_tracks = AsyncMock(side_effect=ClientConnectionError("offline"))
    working_prov = Mock(spec=MusicProvider)
    working_prov.name = "Working"
    working_prov.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    working_prov.get_similar_tracks = AsyncMock(
        return_value=[_similar_track("t1"), _similar_track("t2")]
    )
    mass.get_provider.side_effect = lambda instance_id: {
        "failed": failed_prov,
        "working": working_prov,
    }.get(instance_id)
    mass.get_providers_supporting_feature.return_value = []

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="failed_track",
            provider_domain="failed",
            provider_instance="failed",
        ),
        ProviderMapping(
            item_id="working_track",
            provider_domain="working",
            provider_instance="working",
        ),
    ]
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    result = await controller.similar_tracks("seed", "library")

    assert [track.item_id for track in result] == ["t1", "t2"]
    failed_prov.get_similar_tracks.assert_awaited_once()
    working_prov.get_similar_tracks.assert_awaited_once()


async def test_similar_tracks_normalizes_provider_transport_failure() -> None:
    """An all-provider transport failure is exposed as a typed MA provider error."""
    mass = _mass_with_empty_cache()
    provider = Mock(spec=MusicProvider)
    provider.name = "Failed"
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    client_error = ClientConnectionError("offline")
    provider.get_similar_tracks = AsyncMock(side_effect=client_error)
    mass.get_provider.return_value = provider
    mass.get_providers_supporting_feature.return_value = []

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="failed_track",
            provider_domain="failed",
            provider_instance="failed",
        )
    ]
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError) as raised:
        await controller.similar_tracks("seed", "library")

    assert raised.value.__cause__ is client_error


async def test_similar_tracks_preserves_typed_provider_error() -> None:
    """A typed provider failure is re-raised unchanged when no provider responds."""
    mass = _mass_with_empty_cache()
    provider = Mock(spec=MusicProvider)
    provider.name = "Failed"
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    provider_error = InvalidDataError("invalid response")
    provider.get_similar_tracks = AsyncMock(side_effect=provider_error)
    mass.get_provider.return_value = provider
    mass.get_providers_supporting_feature.return_value = []

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = [
        ProviderMapping(
            item_id="failed_track",
            provider_domain="failed",
            provider_instance="failed",
        )
    ]
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    with pytest.raises(InvalidDataError) as raised:
        await controller.similar_tracks("seed", "library")

    assert raised.value is provider_error


async def test_similar_tracks_lookup_failure_after_empty_response_returns_empty() -> None:
    """A failed cross-provider lookup does not override an earlier valid empty response."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mapped_provider = Mock(spec=MusicProvider)
    mapped_provider.name = "Mapped"
    mapped_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    mapped_provider.get_similar_tracks = AsyncMock(return_value=[])
    lookup_provider = Mock(spec=MusicProvider)
    lookup_provider.name = "Lookup"
    lookup_provider.instance_id = "lookup"
    lookup_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    lookup_provider.get_similar_tracks = AsyncMock(side_effect=ClientConnectionError("offline"))
    mass.get_provider.return_value = mapped_provider
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = [lookup_provider]

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider = "mapped"
    ref_item.provider_mappings = {
        ProviderMapping(
            item_id="mapped_track",
            provider_domain="mapped",
            provider_instance="mapped",
        )
    }
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    controller.match_provider = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ProviderMapping(
                item_id="lookup_track",
                provider_domain="lookup",
                provider_instance="lookup",
            )
        ]
    )

    result = await controller.similar_tracks("seed", "mapped", allow_lookup=True)

    assert result == []
    lookup_provider.get_similar_tracks.assert_awaited_once_with("lookup_track", limit=25)


async def test_similar_tracks_normalizes_provider_matching_failure() -> None:
    """A transport failure while matching a lookup provider is normalized."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    lookup_provider = Mock(spec=MusicProvider)
    lookup_provider.name = "Lookup"
    lookup_provider.instance_id = "lookup"
    lookup_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = [lookup_provider]

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = set()
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    client_error = ClientConnectionError("offline")
    controller.match_provider = AsyncMock(  # type: ignore[method-assign]
        side_effect=client_error
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        await controller.similar_tracks("seed", "library", allow_lookup=True)

    assert raised.value.__cause__ is client_error


async def test_similar_tracks_preserves_failure_when_lookup_is_not_implemented() -> None:
    """An unsupported matched lookup does not discard an earlier provider failure."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mapped_provider = Mock(spec=MusicProvider)
    mapped_provider.name = "Mapped"
    mapped_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    client_error = ClientConnectionError("offline")
    mapped_provider.get_similar_tracks = AsyncMock(side_effect=client_error)
    lookup_provider = Mock(spec=MusicProvider)
    lookup_provider.name = "Lookup"
    lookup_provider.instance_id = "lookup"
    lookup_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    lookup_provider.get_similar_tracks = AsyncMock(side_effect=NotImplementedError)
    mass.get_provider.return_value = mapped_provider
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = [lookup_provider]

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider = "mapped"
    ref_item.provider_mappings = {
        ProviderMapping(
            item_id="mapped_track",
            provider_domain="mapped",
            provider_instance="mapped",
        )
    }
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    controller.match_provider = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ProviderMapping(
                item_id="lookup_track",
                provider_domain="lookup",
                provider_instance="lookup",
            )
        ]
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        await controller.similar_tracks("seed", "mapped", allow_lookup=True)

    assert raised.value.__cause__ is client_error


async def test_similar_tracks_lookup_skips_mapped_provider() -> None:
    """Cross-provider lookup does not retry an instance already mapped to the track."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mapped_provider = Mock(spec=MusicProvider)
    mapped_provider.name = "Mapped"
    mapped_provider.instance_id = "mapped"
    mapped_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    mapped_provider.get_similar_tracks = AsyncMock(return_value=[])
    mass.get_provider.return_value = mapped_provider
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = [mapped_provider]

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = {
        ProviderMapping(
            item_id="mapped_track",
            provider_domain="mapped",
            provider_instance="mapped",
        )
    }
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]
    controller.match_provider = AsyncMock()  # type: ignore[method-assign]

    result = await controller.similar_tracks("seed", "mapped", allow_lookup=True)

    assert result == []
    controller.match_provider.assert_not_awaited()


async def test_similar_tracks_empty_response_wins_without_lookup_provider() -> None:
    """A valid empty response is preserved when no lookup provider is available."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mapped_provider = Mock(spec=MusicProvider)
    mapped_provider.name = "Mapped"
    mapped_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    mapped_provider.get_similar_tracks = AsyncMock(return_value=[])
    mass.get_provider.return_value = mapped_provider
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = []

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = {
        ProviderMapping(
            item_id="mapped_track",
            provider_domain="mapped",
            provider_instance="mapped",
        )
    }
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    result = await controller.similar_tracks("seed", "mapped", allow_lookup=True)

    assert result == []


async def test_similar_tracks_failure_wins_without_lookup_provider() -> None:
    """A provider failure is preserved when no lookup provider is available."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    mapped_provider = Mock(spec=MusicProvider)
    mapped_provider.name = "Mapped"
    mapped_provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    client_error = ClientConnectionError("offline")
    mapped_provider.get_similar_tracks = AsyncMock(side_effect=client_error)
    mass.get_provider.return_value = mapped_provider
    mass.get_providers_supporting_feature.return_value = []
    mass.music.providers = []

    ref_item = Mock()
    ref_item.name = "Seed"
    ref_item.provider_mappings = {
        ProviderMapping(
            item_id="mapped_track",
            provider_domain="mapped",
            provider_instance="mapped",
        )
    }
    controller = TracksController.__new__(TracksController)
    controller.mass = mass
    controller.logger = Mock()
    controller.get = AsyncMock(return_value=ref_item)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError) as raised:
        await controller.similar_tracks("seed", "mapped", allow_lookup=True)

    assert raised.value.__cause__ is client_error


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


def _track(item_id: str, name: str, instance: str) -> Track:
    """Build a minimal Track for the given provider instance."""
    return Track(
        item_id=item_id,
        provider=instance,
        name=name,
        duration=200,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=instance, provider_instance=instance)
        },
    )


# per listing: (feature, item builder, provider method, provider call kwargs)
_ARTIST_LISTINGS: dict[
    str, tuple[ProviderFeature, Callable[[str, str, str], Any], str, dict[str, int]]
] = {
    "similar_artists": (
        ProviderFeature.SIMILAR_ARTISTS,
        _artist,
        "get_similar_artists",
        # the providers are asked for the whole listing, whatever page is asked for
        {"limit": SIMILAR_ITEMS_LIMIT},
    ),
    "top_tracks": (ProviderFeature.ARTIST_TOPTRACKS, _track, "get_artist_toptracks", {}),
}


async def _library_artist_listing(
    controller: ArtistsController, kind: str, provider_filter: str | None
) -> list[Any]:
    """Fetch the given listing for library artist 1."""
    if kind == "similar_artists":
        return await controller.similar_artists(
            "1", "library", provider_filter=provider_filter, limit=5
        )
    return await controller.top_tracks("1", "library", provider_filter=provider_filter)


def _artist_listing_setup(
    kind: str, music_listing: list[Any] | Exception
) -> tuple[ArtistsController, Mock, Mock, Mock]:
    """Build an artists controller with one music and one metadata provider for a listing."""
    feature, build, method, _kwargs = _ARTIST_LISTINGS[kind]
    mass = _mass_with_empty_cache()
    music_prov = Mock(spec=MusicProvider)
    music_prov.available = True
    music_prov.domain = "m_a"
    music_prov.is_streaming_provider = False
    music_prov.supported_features = {feature}
    music_prov.supports_feature = lambda f: f in music_prov.supported_features
    music_mock = (
        AsyncMock(side_effect=music_listing)
        if isinstance(music_listing, Exception)
        else AsyncMock(return_value=music_listing)
    )
    setattr(music_prov, method, music_mock)
    metadata_prov = Mock(spec=MetadataProvider)
    metadata_prov.instance_id = "meta_a"
    setattr(
        metadata_prov,
        method,
        AsyncMock(return_value=[build("c", "Item C", "meta_a"), build("d", "Item D", "meta_a")]),
    )
    mass.get_provider.return_value = music_prov
    mass.get_providers_supporting_feature.return_value = [metadata_prov]
    # keep provider tracks (not resolved to a library equivalent)
    mass.music.tracks.get_visible_library_item_by_prov_id = AsyncMock(return_value=None)

    ref_item = Mock()
    ref_item.name = "Ref Artist"
    ref_item.provider_mappings = [
        ProviderMapping(item_id="artist_123", provider_domain="m_a", provider_instance="m_a")
    ]

    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = mass
    controller.logger = Mock()
    # the library artist itself resolves, provider artists stay unresolved
    controller.get_library_item_by_prov_id = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _item_id, provider: ref_item if provider == "library" else None
    )
    return controller, music_prov, metadata_prov, ref_item


@pytest.mark.parametrize("kind", list(_ARTIST_LISTINGS))
async def test_library_artist_listing_skips_metadata_when_music_provider_has_results(
    kind: str,
) -> None:
    """Metadata providers are not consulted when the artist's music providers return items."""
    _feature, build, method, kwargs = _ARTIST_LISTINGS[kind]
    controller, music_prov, metadata_prov, _ref_item = _artist_listing_setup(
        kind, [build("a", "Item A", "m_a"), build("b", "Item B", "m_a")]
    )

    result = await _library_artist_listing(controller, kind, None)

    assert [item.name for item in result] == ["Item A", "Item B"]
    getattr(music_prov, method).assert_awaited_once_with("artist_123", **kwargs)
    getattr(metadata_prov, method).assert_not_awaited()


@pytest.mark.parametrize("kind", list(_ARTIST_LISTINGS))
async def test_library_artist_listing_falls_back_to_metadata_provider(kind: str) -> None:
    """Metadata providers are consulted when the artist's music providers all return nothing."""
    _feature, _build, method, kwargs = _ARTIST_LISTINGS[kind]
    controller, music_prov, metadata_prov, ref_item = _artist_listing_setup(kind, [])

    result = await _library_artist_listing(controller, kind, None)

    assert [item.name for item in result] == ["Item C", "Item D"]
    getattr(music_prov, method).assert_awaited_once_with("artist_123", **kwargs)
    getattr(metadata_prov, method).assert_awaited_once_with(ref_item, **kwargs)


@pytest.mark.parametrize("kind", list(_ARTIST_LISTINGS))
async def test_library_artist_listing_does_not_fall_back_after_a_failed_music_provider(
    kind: str,
) -> None:
    """A failed music provider (e.g. rate limited) never turns into metadata provider requests."""
    _feature, _build, method, kwargs = _ARTIST_LISTINGS[kind]
    controller, music_prov, metadata_prov, _ref_item = _artist_listing_setup(
        kind, InvalidDataError("boom")
    )

    result = await _library_artist_listing(controller, kind, None)

    assert result == []
    getattr(music_prov, method).assert_awaited_once_with("artist_123", **kwargs)
    getattr(metadata_prov, method).assert_not_awaited()


@pytest.mark.parametrize("kind", list(_ARTIST_LISTINGS))
async def test_library_artist_listing_filtered_to_metadata_provider(kind: str) -> None:
    """A provider filter naming a metadata provider queries only that provider."""
    _feature, build, method, kwargs = _ARTIST_LISTINGS[kind]
    controller, music_prov, metadata_prov, ref_item = _artist_listing_setup(
        kind, [build("a", "Item A", "m_a")]
    )

    result = await _library_artist_listing(controller, kind, "meta_a")

    assert [item.name for item in result] == ["Item C", "Item D"]
    getattr(music_prov, method).assert_not_awaited()
    getattr(metadata_prov, method).assert_awaited_once_with(ref_item, **kwargs)


@pytest.mark.parametrize("kind", list(_ARTIST_LISTINGS))
async def test_library_artist_listing_filtered_to_music_provider_never_falls_back(
    kind: str,
) -> None:
    """A provider filter naming a music provider keeps metadata providers out of the fallback."""
    _feature, _build, method, _kwargs = _ARTIST_LISTINGS[kind]
    controller, music_prov, metadata_prov, _ref_item = _artist_listing_setup(kind, [])

    result = await _library_artist_listing(controller, kind, "m_a")

    assert result == []
    getattr(music_prov, method).assert_awaited_once()
    getattr(metadata_prov, method).assert_not_awaited()


async def test_library_artist_listing_dedupes_across_music_providers() -> None:
    """Listings of several music providers are interleaved by rank and deduplicated."""
    controller, music_prov, _metadata_prov, ref_item = _artist_listing_setup(
        "similar_artists", [_artist("a", "Artist A", "m_a"), _artist("b", "Artist B", "m_a")]
    )
    other_prov = Mock(spec=MusicProvider)
    other_prov.available = True
    other_prov.domain = "m_b"
    other_prov.is_streaming_provider = False
    other_prov.supported_features = {ProviderFeature.SIMILAR_ARTISTS}
    other_prov.supports_feature = lambda f: f in other_prov.supported_features
    other_prov.get_similar_artists = AsyncMock(
        return_value=[_artist("b2", "Artist B", "m_b"), _artist("c", "Artist C", "m_b")]
    )
    cast("Mock", controller.mass).get_provider.side_effect = lambda instance, **_kwargs: (
        music_prov if instance == "m_a" else other_prov
    )
    ref_item.provider_mappings.append(
        ProviderMapping(item_id="artist_456", provider_domain="m_b", provider_instance="m_b")
    )

    result = await _library_artist_listing(controller, "similar_artists", None)

    # interleaved by rank, deduped on name ("Artist B" comes from both providers)
    assert [artist.name for artist in result] == ["Artist A", "Artist B", "Artist C"]


async def test_similar_artists_provider_queries_named_provider() -> None:
    """Similar artists for a provider artist should query only that provider."""
    mass = _mass_with_empty_cache()
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
    music_prov.get_similar_artists.assert_awaited_once_with("artist_123", limit=SIMILAR_ITEMS_LIMIT)


async def test_browse_root_includes_non_music_providers() -> None:
    """Root browse should list every provider declaring BROWSE, regardless of type."""
    mass = Mock()
    mass.music.get_visible_provider = mass.get_provider
    music_prov = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_prov.domain = "music_a"
    music_prov.name = "Music A"
    plugin_prov = _make_prov("p_a", ProviderType.PLUGIN, {ProviderFeature.BROWSE})
    plugin_prov.domain = "plugin_a"
    plugin_prov.name = "Plugin A"

    # browse queries get_providers_supporting_feature for BROWSE; the audio source
    # providers come from the players controller
    def _supports(feature: ProviderFeature) -> list[Mock]:
        if feature == ProviderFeature.BROWSE:
            return [music_prov, plugin_prov]
        return []

    mass.get_providers_supporting_feature.side_effect = _supports
    mass.players.get_audio_source_providers.return_value = []

    controller = MusicController.__new__(MusicController)
    controller.mass = mass

    result = await controller.browse(path=None)

    assert [folder.path for folder in result] == ["m_a://", "p_a://"]  # type: ignore[union-attr]
    calls = [
        c.args[0] if c.args else c.kwargs["feature"]
        for c in mass.get_providers_supporting_feature.call_args_list
    ]
    assert ProviderFeature.BROWSE in calls


def _audio_source_item(
    provider: str, item_id: str, *, can_initiate: bool = True, owner: str | None = None
) -> AudioSource:
    """Return an AudioSource as a plugin would declare it, belonging to the given player."""
    return AudioSource(
        item_id=item_id,
        provider=provider,
        name=f"Source {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
        can_initiate=can_initiate,
        owner_player_id=owner,
    )


class _AudioSourcePlugin(PluginProvider):
    """Plugin stub exposing AudioSources, optionally bound per player."""

    def __init__(
        self,
        instance_id: str,
        sources: list[AudioSource],
        bound_players: dict[str, list[AudioSource]] | None = None,
        player_inputs: dict[str, list[AudioSource]] | None = None,
    ) -> None:
        self.manifest = Mock(type=ProviderType.PLUGIN)
        self.manifest.domain = instance_id
        self.config = Mock(instance_id=instance_id)
        self.config.name = instance_id
        self._supported_features = {ProviderFeature.AUDIO_SOURCE}
        self._sources = sources
        self._bound_players = bound_players
        self._player_inputs = player_inputs or {}

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return all sources this plugin exposes."""
        return self._sources

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource] | None:
        """Return the sources bound to the given player, or None when not player-bound."""
        if self._bound_players is None:
            return None
        return self._bound_players.get(player_id, [])

    def get_player_input_sources(self, player_id: str) -> list[AudioSource]:
        """Return the sources that are inputs of the given player's own device."""
        return self._player_inputs.get(player_id, [])


def _player(player_id: str, *protocol_ids: str, private: bool = False) -> Mock:
    """Return a player stub with the given protocols attached, private client ones if asked."""
    player = Mock(
        player_id=player_id,
        protocol_ids=set(protocol_ids),
        attached_protocols={protocol_id: Mock(private=private) for protocol_id in protocol_ids},
    )
    player.name = player_id.title()
    return player


def _set_players(mass: Mock, *players: Mock) -> None:
    """Make the given players the ones the players controller knows, also by their protocols."""
    by_id = {player.player_id: player for player in players}
    by_protocol = {protocol_id: x for x in players for protocol_id in x.protocol_ids}
    mass.players.iter_players.return_value = list(players)
    mass.players.get_player.side_effect = by_id.get
    mass.players.get_audio_source_owner.side_effect = lambda source: by_id.get(
        source.owner_player_id, by_protocol.get(source.owner_player_id)
    )
    mass.players.get_audio_source_name.side_effect = partial(
        PlayerController.get_audio_source_name, mass.players
    )


def _audio_source_browse_controller() -> tuple[MusicController, PluginProvider, PluginProvider]:
    """Build a browse controller with one player-bound and one unbound audio source plugin."""
    kitchen_source = _audio_source_item("connect", "kitchen", owner="kitchen")
    office_source = _audio_source_item("connect", "office", owner="office")
    bound_plugin = _AudioSourcePlugin(
        "connect",
        [kitchen_source, office_source],
        bound_players={"kitchen": [kitchen_source], "office": [office_source]},
    )
    unbound_plugin = _AudioSourcePlugin("vban", [_audio_source_item("vban", "input1")])

    mass = Mock()
    mass.get_providers_supporting_feature.return_value = []
    mass.players.get_audio_source_providers.return_value = [bound_plugin, unbound_plugin]
    _set_players(mass, _player("kitchen"), _player("office"))
    mass.get_provider.side_effect = lambda instance_id: {
        "connect": bound_plugin,
        "vban": unbound_plugin,
    }.get(instance_id)
    # no music sources at all, so nothing narrows this listing but the player filter
    set_music_source_access(mass, {})

    controller = MusicController.__new__(MusicController)
    controller.mass = mass
    return controller, bound_plugin, unbound_plugin


async def test_browse_root_lists_one_live_sources_folder() -> None:
    """The root lists one folder that holds the live sources of every provider."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path=None)

    assert len(result) == 1
    folder = result[0]
    assert isinstance(folder, BrowseFolder)
    assert (folder.path, folder.name, folder.translation_key) == (
        "live_sources://",
        "Live sources",
        "live_sources",
    )


async def test_browse_root_lists_no_live_sources_folder_without_a_source_to_start() -> None:
    """Without a live source the user can start, the root has no folder for them."""
    controller, _bound, _unbound = _audio_source_browse_controller()
    passive = _audio_source_item("airplay", "kitchen", can_initiate=False, owner="kitchen")
    cast("Mock", controller.mass).players.get_audio_source_providers.return_value = [
        _AudioSourcePlugin("airplay", [passive], bound_players={"kitchen": [passive]})
    ]

    assert await controller.browse(path=None) == []
    assert await controller.browse(path=None, player_id="kitchen") == []


async def test_browse_live_sources_folder_scopes_audio_sources_to_player() -> None:
    """With a player scope, the folder holds that player's bound sources and the unbound ones."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path="live_sources://", player_id="kitchen")

    assert _audio_source_item("connect", "kitchen") in result
    assert _audio_source_item("connect", "office") not in result
    assert _audio_source_item("vban", "input1") in result


async def test_browse_live_sources_folder_hides_bound_sources_of_other_players() -> None:
    """A player-bound plugin has nothing in the folder for a player it has no source for."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path="live_sources://", player_id="livingroom")

    assert _audio_source_item("connect", "kitchen") not in result
    assert _audio_source_item("connect", "office") not in result
    assert _audio_source_item("vban", "input1") in result


async def test_browse_live_sources_folder_names_each_source_with_its_player() -> None:
    """Without a player scope the folder holds every source, named with its player, after a way back."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path="live_sources://")

    back = result[0]
    assert isinstance(back, BrowseFolder)
    assert (back.path, back.name) == ("root", "..")
    assert [item.name for item in result[1:]] == [
        "Source kitchen (Kitchen)",
        "Source office (Office)",
        "Source input1",
    ]


async def test_browse_provider_level_scopes_audio_sources_to_player() -> None:
    """The provider-level source listing honors the player scope too."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path="connect://", player_id="kitchen")

    assert _audio_source_item("connect", "kitchen") in result
    assert _audio_source_item("connect", "office") not in result


async def test_browse_provider_level_without_player_scope_lists_all_sources() -> None:
    """Without a player scope the provider-level listing keeps every source."""
    controller, _bound, _unbound = _audio_source_browse_controller()

    result = await controller.browse(path="connect://")

    assert _audio_source_item("connect", "kitchen") in result
    assert _audio_source_item("connect", "office") in result


async def test_browse_bound_sources_honor_the_user_player_filter() -> None:
    """A restricted user only sees bound sources of players their filter allows."""
    controller, _bound, _unbound = _audio_source_browse_controller()
    restricted_user = Mock(player_filter=["office"])

    with patch(
        "music_assistant.controllers.music.controller.get_current_user",
        return_value=restricted_user,
    ):
        unscoped = await controller.browse(path="live_sources://")
        hidden_scope = await controller.browse(path="live_sources://", player_id="kitchen")

    assert _audio_source_item("connect", "office") in unscoped
    assert _audio_source_item("connect", "kitchen") not in unscoped
    assert _audio_source_item("vban", "input1") in unscoped
    # scoping to a player outside the filter yields no bound sources
    assert _audio_source_item("connect", "kitchen") not in hidden_scope
    assert _audio_source_item("vban", "input1") in hidden_scope


def _line_in_controller() -> tuple[MusicController, PluginProvider]:
    """Build a browse controller that also lists line-ins of two players and of no player."""
    controller, bound_plugin, unbound_plugin = _audio_source_browse_controller()
    kitchen_input = _audio_source_item("sendspin", "kitchen-device", owner="kitchen-sendspin")
    office_input = _audio_source_item("sendspin", "office-device", owner="office")
    # the den player is disabled, so the players controller no longer holds it
    den_input = _audio_source_item("sendspin", "den-device", owner="den")
    # the patio device is held by another server: without a connection it reports no input
    patio_input = _audio_source_item("sendspin", "patio-device", owner="patio-sendspin")
    line_in = _AudioSourcePlugin(
        "sendspin",
        [
            kitchen_input,
            office_input,
            den_input,
            patio_input,
            _audio_source_item("sendspin", "mixer"),
        ],
        # the kitchen device is known by the id of its protocol, the office one by the player
        player_inputs={
            "kitchen-sendspin": [kitchen_input],
            "office": [office_input],
            "den": [den_input],
        },
    )
    mass = cast("Mock", controller.mass)
    mass.players.get_audio_source_providers.return_value = [bound_plugin, unbound_plugin, line_in]
    mass.get_provider.side_effect = {
        "connect": bound_plugin,
        "vban": unbound_plugin,
        "sendspin": line_in,
    }.get
    _set_players(
        mass,
        _player("kitchen", "kitchen-sendspin"),
        _player("office"),
        _player("patio", "patio-sendspin"),
    )
    return controller, line_in


async def test_browse_line_in_sources_follow_the_user_player_filter() -> None:
    """A restricted user only sees the line-ins of player devices their filter allows."""
    controller, _line_in = _line_in_controller()

    with as_user(["office"]):
        unscoped = await controller.browse(path="sendspin://")
        scoped = await controller.browse(path="sendspin://", player_id="office")

    for result in (unscoped, scoped):
        assert _audio_source_item("sendspin", "office-device") in result
        assert _audio_source_item("sendspin", "kitchen-device") not in result
        # a source of no player stays available to everyone
        assert _audio_source_item("sendspin", "mixer") in result


async def test_browse_line_in_sources_unrestricted_user_sees_all() -> None:
    """A user without a player filter sees every line-in."""
    controller, _line_in = _line_in_controller()

    result = await controller.browse(path="sendspin://")

    assert _audio_source_item("sendspin", "office-device") in result
    assert _audio_source_item("sendspin", "kitchen-device") in result
    assert _audio_source_item("sendspin", "mixer") in result


@pytest.mark.parametrize(
    "item_id", ["den-device", "patio-device"], ids=["disabled_player", "held_by_another_server"]
)
async def test_line_in_of_a_player_hidden_from_the_user_stays_hidden(item_id: str) -> None:
    """
    The line-in of a player the user may not use stays hidden, also when no input names it.

    A disabled player is gone from the players, and a device held by another server reports
    no input of its player while it is not connected here.
    """
    controller, _line_in = _line_in_controller()

    with as_user(["office"]):
        listed = await controller.browse(path="sendspin://")
        with pytest.raises(MediaNotFoundError):
            await controller.get_item(MediaType.AUDIO_SOURCE, item_id, "sendspin")

    assert _audio_source_item("sendspin", item_id) not in listed
    assert _audio_source_item("sendspin", "office-device") in listed


async def test_audio_source_lookup_follows_the_user_player_filter() -> None:
    """A restricted user can only look up the sources of players their filter allows."""
    controller, _line_in = _line_in_controller()

    with as_user(["office"]):
        office = await controller.get_item(MediaType.AUDIO_SOURCE, "office-device", "sendspin")
        mixer = await controller.get_item(MediaType.AUDIO_SOURCE, "mixer", "sendspin")
        for provider, item_id in (("sendspin", "kitchen-device"), ("connect", "kitchen")):
            with pytest.raises(MediaNotFoundError):
                await controller.get_item(MediaType.AUDIO_SOURCE, item_id, provider)

    assert office == _audio_source_item("sendspin", "office-device")
    assert mixer == _audio_source_item("sendspin", "mixer")
    assert await controller.get_item(
        MediaType.AUDIO_SOURCE, "kitchen-device", "sendspin"
    ) == _audio_source_item("sendspin", "kitchen-device")


async def test_audio_source_uri_resolves_only_for_the_user_player_filter() -> None:
    """Selecting or playing a source by its uri only finds sources the user may use."""
    music, line_in = _line_in_controller()
    music.mass.music = music
    players = PlayerController.__new__(PlayerController)
    players.mass = music.mass
    players.logger = Mock()

    with as_user(["office"]):
        allowed = await players._resolve_audio_source_uri("sendspin://audio_source/office-device")
        hidden = await players._resolve_audio_source_uri("sendspin://audio_source/kitchen-device")

    assert allowed == (_audio_source_item("sendspin", "office-device"), line_in)
    assert hidden is None


async def test_bound_sources_of_the_own_client_player_stay_available() -> None:
    """A restricted user keeps the sources bound to the private client player they connected on."""
    controller, _bound, _unbound = _audio_source_browse_controller()
    phone_source = _audio_source_item("connect", "phone", owner="phone")
    connect = _AudioSourcePlugin("connect", [phone_source], bound_players={"phone": [phone_source]})
    mass = cast("Mock", controller.mass)
    mass.players.get_audio_source_providers.return_value = [connect]
    mass.get_provider.side_effect = {"connect": connect}.get
    _set_players(mass, _player("office"), _player("phone", "phone-sendspin", private=True))

    with as_user(["office"], client_player_id="phone-sendspin"):
        looked_up = await controller.get_item(MediaType.AUDIO_SOURCE, "phone", "connect")
        scoped = await controller.browse(path="connect://", player_id="phone")
    # connected on another client, the same user may not use that player
    with as_user(["office"]), pytest.raises(MediaNotFoundError):
        await controller.get_item(MediaType.AUDIO_SOURCE, "phone", "connect")

    assert looked_up == phone_source
    assert phone_source in scoped


class _LineInPlayerProvider(AudioSourceMixin, PlayerProvider):
    """Player provider stub that delivers AudioSources of its own."""


def _audio_source_provider[ProviderT: Provider](
    provider_cls: type[ProviderT],
    instance_id: str,
    provider_type: ProviderType,
    features: set[ProviderFeature],
    available: bool = True,
) -> ProviderT:
    """Construct a provider of the given class with the given features."""
    manifest = Mock(type=provider_type, domain=instance_id)
    config = Mock(instance_id=instance_id)
    config.name = instance_id
    config.get_value.return_value = "GLOBAL"
    provider = provider_cls(Mock(), manifest, config, features)
    provider.available = available
    return provider


def test_audio_source_providers_span_provider_types() -> None:
    """Every available provider delivering AudioSources is returned, whatever its type."""
    plugin = _audio_source_provider(
        PluginProvider, "connect", ProviderType.PLUGIN, {ProviderFeature.AUDIO_SOURCE}
    )
    player_provider = _audio_source_provider(
        _LineInPlayerProvider, "sendspin", ProviderType.PLAYER, {ProviderFeature.AUDIO_SOURCE}
    )
    without_feature = _audio_source_provider(
        _LineInPlayerProvider, "other", ProviderType.PLAYER, set()
    )
    unavailable = _audio_source_provider(
        PluginProvider,
        "vban",
        ProviderType.PLUGIN,
        {ProviderFeature.AUDIO_SOURCE},
        available=False,
    )
    mass = Mock(spec=MusicAssistant)
    mass.providers = [player_provider, without_feature, unavailable, plugin]
    mass.get_providers_supporting_feature = partial(
        MusicAssistant.get_providers_supporting_feature, mass
    )
    controller = PlayerController.__new__(PlayerController)
    controller.mass = mass

    assert controller.get_audio_source_providers() == [plugin, player_provider]


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
    controller._search_provider = AsyncMock(return_value=SearchResults())  # type: ignore[method-assign]

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
