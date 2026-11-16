"""Offline tests for Last.fm streaming provider eligibility and catalog dispatch."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.lastfm_recommendations import parsers
from music_assistant.providers.lastfm_recommendations.constants import PROVIDER_SEARCH_LIMIT

INSTANCE_ID = "lastfm_recommendations--test"
MEDIA_TYPES = (MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK)
LIBRARY_FEATURES = {
    MediaType.ARTIST: ProviderFeature.LIBRARY_ARTISTS,
    MediaType.ALBUM: ProviderFeature.LIBRARY_ALBUMS,
    MediaType.TRACK: ProviderFeature.LIBRARY_TRACKS,
}


class _CatalogProvider(MusicProvider):
    """Provider explicitly serving catalog types independently of library enumeration."""

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the catalog's supported media types."""
        return set(MEDIA_TYPES)


class _TrackProvider(_CatalogProvider):
    """Catalog containing only tracks."""

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the only supported media type."""
        return {MediaType.TRACK}


class _LocalProvider(_CatalogProvider):
    """Non-streaming catalog."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return False for a local catalog."""
        return False


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("library_support", [False, True])
async def test_catalog_resolution_uses_real_controller_search(
    media_type: MediaType, library_support: bool
) -> None:
    """Both search-only and conventional providers reach inherited controller dispatch."""
    mass = _mass()
    features = {ProviderFeature.SEARCH}
    if library_support:
        features.add(LIBRARY_FEATURES[media_type])
    provider = _provider(mass, MusicProvider if library_support else _CatalogProvider, features)
    mass.music.providers = [provider]
    mapping, result = _items(media_type, provider.instance_id)
    cast("AsyncMock", provider.search).return_value = _results(result)

    assert parsers._get_streaming_providers(mass, mapping, INSTANCE_ID) == [provider]
    assert await parsers._resolve_item(mapping, mass, INSTANCE_ID, "Snow Patrol") is result
    cast("AsyncMock", provider.search).assert_awaited_once_with(
        mapping.name, [media_type], PROVIDER_SEARCH_LIMIT
    )


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
async def test_track_only_provider_respects_media_types(media_type: MediaType) -> None:
    """Track support does not imply artist or album support."""
    mass = _mass()
    provider = _provider(mass, _TrackProvider)
    mass.music.providers = [provider]
    mapping, result = _items(media_type, provider.instance_id)
    cast("AsyncMock", provider.search).return_value = _results(result)

    if media_type == MediaType.TRACK:
        assert await parsers._resolve_item(mapping, mass, INSTANCE_ID, "Snow Patrol") is result
        cast("AsyncMock", provider.search).assert_awaited_once()
    else:
        assert parsers._get_streaming_providers(mass, mapping, INSTANCE_ID) == []
        with pytest.raises(parsers.SearchIncomplete):
            await parsers._resolve_item(mapping, mass, INSTANCE_ID, "Snow Patrol")
        cast("AsyncMock", provider.search).assert_not_awaited()


async def test_non_streaming_provider_is_excluded_but_library_match_is_preserved() -> None:
    """Local catalogs stay excluded while an existing MA library item still resolves."""
    mass = _mass()
    provider = _provider(mass, _LocalProvider)
    mass.music.providers = [provider]
    mapping, _ = _items(MediaType.TRACK, provider.instance_id)
    _, library_item = _items(MediaType.TRACK, "library")

    assert parsers._get_streaming_providers(mass, mapping, INSTANCE_ID) == []
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._resolve_item(mapping, mass, INSTANCE_ID)
    mass.music.tracks.get_library_item_by_external_ids.return_value = library_item
    assert await parsers._resolve_item(mapping, mass, INSTANCE_ID) is library_item
    cast("AsyncMock", provider.search).assert_not_awaited()


@pytest.mark.parametrize("first_matches", [False, True])
async def test_provider_order_self_exclusion_and_early_exit(first_matches: bool) -> None:
    """Filtering preserves preference order and a verified match spares later providers."""
    mass = _mass()
    own = _provider(mass, instance_id=INSTANCE_ID)
    first = _provider(mass, instance_id="catalog--z")
    second = _provider(mass, instance_id="catalog--a")
    direct_only = _provider(mass, features=set(), instance_id="catalog--direct")
    mass.music.providers = [own, direct_only, first, second]
    mapping, result = _items(
        MediaType.TRACK, first.instance_id if first_matches else second.instance_id
    )
    cast("AsyncMock", first.search).return_value = (
        _results(result) if first_matches else SearchResults()
    )
    cast("AsyncMock", second.search).return_value = _results(result)

    assert parsers._get_streaming_providers(mass, mapping, INSTANCE_ID) == [
        direct_only,
        first,
        second,
    ]
    assert await parsers._resolve_item(mapping, mass, INSTANCE_ID, "Snow Patrol") is result
    cast("AsyncMock", own.search).assert_not_awaited()
    cast("AsyncMock", direct_only.search).assert_not_awaited()
    assert [call.args[0] for call in mass.music.get_visible_provider.call_args_list] == (
        [first.instance_id] if first_matches else [first.instance_id, second.instance_id]
    )


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
async def test_search_feature_alone_leaves_inherited_media_types_empty(
    media_type: MediaType,
) -> None:
    """An incomplete provider declaration remains rejected by the shared controller."""
    mass = _mass()
    provider = _provider(mass, MusicProvider)
    mass.music.providers = [provider]
    mapping, _ = _items(media_type, provider.instance_id)
    ctrl = getattr(mass.music, f"{media_type.value}s")

    assert provider.supported_media_types == set()
    assert await ctrl.search(mapping.name, provider.instance_id) == []
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._resolve_item(mapping, mass, INSTANCE_ID)
    cast("AsyncMock", provider.search).assert_not_awaited()


def _mass() -> Mock:
    """Return a minimal server with real media controllers and no external services."""
    mass = Mock()
    mass.get_provider.return_value = None
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    mass.music.providers = []
    mass.music.get_visible_provider.side_effect = lambda instance_id: next(
        (p for p in mass.music.providers if p.instance_id == instance_id), None
    )
    for controller_cls in (ArtistsController, AlbumsController, TracksController):
        ctrl = controller_cls(mass)
        cast("Mock", ctrl).get_library_item_by_external_ids = AsyncMock(return_value=None)
        setattr(mass.music, f"{ctrl.media_type.value}s", ctrl)
    return mass


def _provider(
    mass: Mock,
    provider_cls: type[MusicProvider] = _CatalogProvider,
    features: set[ProviderFeature] | None = None,
    instance_id: str = "catalog--test",
) -> MusicProvider:
    """Construct an available synthetic provider with a mocked catalog response."""
    manifest = Mock(type=ProviderType.MUSIC, domain="catalog")
    manifest.name = "Test Catalog"
    config = Mock(instance_id=instance_id)
    config.name = "Test Catalog"
    config.get_value.return_value = "GLOBAL"
    provider = provider_cls(
        mass, manifest, config, {ProviderFeature.SEARCH} if features is None else features
    )
    provider.available = True
    provider.search = AsyncMock(return_value=SearchResults())  # type: ignore[method-assign]
    return provider


def _items(media_type: MediaType, provider: str) -> tuple[ItemMapping, Artist | Album | Track]:
    """Return a recommendation mapping and matching catalog item."""
    artist = Artist(
        item_id="artist", provider=provider, name="Snow Patrol", provider_mappings=set()
    )
    result: Artist | Album | Track
    if media_type == MediaType.ARTIST:
        result = artist
    elif media_type == MediaType.ALBUM:
        result = Album(
            item_id="album",
            provider=provider,
            name="Chasing Cars",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
    else:
        result = Track(
            item_id="track",
            provider=provider,
            name="Chasing Cars",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
    mapping = ItemMapping(
        media_type=media_type,
        item_id="temp",
        provider=INSTANCE_ID,
        name="Snow Patrol" if media_type == MediaType.ARTIST else "Snow Patrol - Chasing Cars",
    )
    return mapping, result


def _results(item: Artist | Album | Track) -> SearchResults:
    """Wrap a catalog item in its corresponding search result list."""
    results = SearchResults()
    getattr(results, f"{item.media_type.value}s").append(item)
    return results
