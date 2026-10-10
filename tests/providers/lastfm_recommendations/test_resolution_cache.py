"""Tests for how the Last.fm Recommendations provider caches item resolution outcomes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature, ProviderStatus
from music_assistant_models.errors import MediaNotFoundError, RetriesExhausted
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track

from music_assistant.providers.lastfm_recommendations import parsers
from music_assistant.providers.lastfm_recommendations.recommendations import (
    LastFMRecommendationManager,
)

INSTANCE_ID = "lastfm_recommendations--test1"
LASTFM_TRACK = {"name": "Chasing Cars", "artist": {"name": "Snow Patrol"}}
MBID = "5a4f2d0e-6e1c-4b7a-9c3d-2f1e8b7a6c5d"
LASTFM_TRACK_WITH_MBID = {**LASTFM_TRACK, "mbid": MBID}
APPLE_MUSIC_MAPPING = ProviderMapping(
    item_id="1", provider_domain="apple_music", provider_instance="apple_music--1"
)


class _FakeCache:
    """In-memory stand-in for the cache controller."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str, **_kwargs: Any) -> Any:
        return self.data.get(key)

    async def set(self, key: str, data: Any, **_kwargs: Any) -> None:
        self.data[key] = data


@pytest.fixture
def cache() -> _FakeCache:
    """Return an empty in-memory cache."""
    return _FakeCache()


@pytest.fixture
def manager(cache: _FakeCache) -> LastFMRecommendationManager:
    """Return a recommendation manager backed by the in-memory cache."""
    provider = Mock()
    provider.instance_id = INSTANCE_ID
    provider.mass.cache = cache
    return LastFMRecommendationManager(provider)


def _track() -> Track:
    return Track(
        item_id="1",
        provider="apple_music",
        name="Chasing Cars",
        provider_mappings=set(),
    )


@pytest.mark.asyncio
async def test_miss_is_remembered(manager: LastFMRecommendationManager, cache: _FakeCache) -> None:
    """An item no provider has is not searched for again on the next refresh."""
    resolve = AsyncMock(return_value=None)
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
    assert resolve.await_count == 1
    assert cache.data["miss_track_Snow Patrol_Chasing Cars"] is True


@pytest.mark.asyncio
async def test_expired_miss_is_searched_again(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """Once the remembered miss expires, the item is searched for again."""
    resolve = AsyncMock(side_effect=[None, _track()])
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        del cache.data["miss_track_Snow Patrol_Chasing Cars"]
        resolved = await manager.get_or_resolve_track(LASTFM_TRACK)
    assert resolved is not None
    assert resolve.await_count == 2


@pytest.mark.asyncio
async def test_persisted_miss_survives_restart(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """A miss remembered before a restart still skips the search afterwards."""
    cache.data["miss_track_Snow Patrol_Chasing Cars"] = True
    resolve = AsyncMock()
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_search_is_retried(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """A search that could not complete is not remembered as a miss."""
    resolve = AsyncMock(side_effect=[parsers.SearchIncomplete("x"), _track()])
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        resolved = await manager.get_or_resolve_track(LASTFM_TRACK)
    assert resolved is not None
    assert resolved.name == "Chasing Cars"
    assert not any(key.startswith("miss_") for key in cache.data)


@pytest.mark.asyncio
async def test_failed_provider_search_makes_resolution_incomplete() -> None:
    """A rate limited provider search does not count as the provider not having the item."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=RetriesExhausted("rate limited"))
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers(ctrl, mapping, [Mock(name="p")], None)


@pytest.mark.asyncio
async def test_empty_provider_searches_are_a_miss() -> None:
    """Providers that answered without a match make a definite miss."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[])
    assert await parsers._search_providers(ctrl, mapping, [Mock(name="p")], None) is None


def _mass(provider_status: ProviderStatus) -> Mock:
    """Return a mass stand-in with one streaming provider that finds nothing."""
    streaming = Mock(
        instance_id="apple_music--1",
        domain="apple_music",
        is_streaming_provider=True,
        supported_features={ProviderFeature.LIBRARY_TRACKS},
    )
    mass = Mock()
    mass.music.providers = [streaming]
    mass.music.tracks.get_library_item_by_external_ids = AsyncMock(return_value=None)
    mass.music.tracks.search = AsyncMock(return_value=[])
    mass.config.get_provider_configs = AsyncMock(
        return_value=[Mock(status=ProviderStatus.LOADED), Mock(status=provider_status)]
    )
    return mass


@pytest.mark.asyncio
async def test_no_match_while_a_provider_loads_is_incomplete() -> None:
    """A provider that has not loaded yet may have the item, so it is not a miss yet."""
    with pytest.raises(parsers.SearchIncomplete):
        await parsers.parse_track(LASTFM_TRACK, _mass(ProviderStatus.LOADING), INSTANCE_ID)


@pytest.mark.asyncio
async def test_no_match_with_all_providers_settled_is_a_miss() -> None:
    """A provider that failed to load does not hold back a miss."""
    assert await parsers.parse_track(LASTFM_TRACK, _mass(ProviderStatus.ERROR), INSTANCE_ID) is None


@pytest.mark.asyncio
async def test_provider_finishing_loading_during_the_search_is_no_miss() -> None:
    """A provider that loads while the others are searched was not searched itself."""
    mass = _mass(ProviderStatus.LOADING)

    async def _search_while_provider_finishes(*_args: Any, **_kwargs: Any) -> list[Track]:
        mass.config.get_provider_configs.return_value = [Mock(status=ProviderStatus.LOADED)]
        return []

    mass.music.tracks.search = AsyncMock(side_effect=_search_while_provider_finishes)
    with pytest.raises(parsers.SearchIncomplete):
        await parsers.parse_track(LASTFM_TRACK, mass, INSTANCE_ID)


@pytest.mark.asyncio
async def test_a_match_in_second_place_is_found() -> None:
    """Every candidate a provider returns is checked, not only the first."""
    mapping = ItemMapping(
        media_type=Track.media_type, item_id="temp", provider="x", name="Chasing Cars"
    )
    other = Track(item_id="2", provider="p", name="Run", provider_mappings=set())
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[other, _track()])
    result = await parsers._search_providers(ctrl, mapping, [Mock(name="p")], None)
    assert result is not None
    assert result.name == "Chasing Cars"


@pytest.mark.asyncio
async def test_unavailable_provider_makes_resolution_incomplete() -> None:
    """A provider that is unavailable was not searched, so finding nothing is no miss."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[])
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers(ctrl, mapping, [Mock(name="p", available=False)], None)
    ctrl.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_unloading_during_the_search_makes_resolution_incomplete() -> None:
    """A provider that went unavailable while searched may have answered for that reason."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    provider = Mock(name="p", available=True)

    async def _search_while_unloading(*_args: Any, **_kwargs: Any) -> list[Track]:
        provider.available = False
        return []

    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=_search_while_unloading)
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers(ctrl, mapping, [provider], None)


def _provider(name: str) -> Mock:
    """Return an available provider stand-in with the given instance id."""
    provider = Mock(name=name, available=True)
    provider.instance_id = name
    return provider


@pytest.mark.asyncio
async def test_match_on_first_provider_skips_the_rest() -> None:
    """A match on the first provider means the next provider is never searched."""
    mapping = ItemMapping(
        media_type=Track.media_type, item_id="temp", provider="x", name="Chasing Cars"
    )
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=[[_track()], [_track()]])
    result = await parsers._search_providers(
        ctrl, mapping, [_provider("first"), _provider("second")], None
    )
    assert result is not None
    assert result.name == "Chasing Cars"
    assert ctrl.search.await_count == 1
    assert ctrl.search.await_args_list[0].args[1] == "first"


@pytest.mark.asyncio
async def test_no_match_on_first_provider_searches_the_next() -> None:
    """When the first provider has no match, the next provider is searched."""
    mapping = ItemMapping(
        media_type=Track.media_type, item_id="temp", provider="x", name="Chasing Cars"
    )
    other = Track(item_id="2", provider="first", name="Run", provider_mappings=set())
    match = Track(item_id="3", provider="second", name="Chasing Cars", provider_mappings=set())
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=[[other], [match]])
    result = await parsers._search_providers(
        ctrl, mapping, [_provider("first"), _provider("second")], None
    )
    assert result is match
    assert [call.args[1] for call in ctrl.search.await_args_list] == ["first", "second"]


@pytest.mark.asyncio
async def test_match_after_a_failed_provider_is_returned() -> None:
    """A match on a later provider wins over an earlier provider that could not be searched."""
    mapping = ItemMapping(
        media_type=Track.media_type, item_id="temp", provider="x", name="Chasing Cars"
    )
    match = Track(item_id="3", provider="second", name="Chasing Cars", provider_mappings=set())
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=[RetriesExhausted("rate limited"), [match]])
    result = await parsers._search_providers(
        ctrl, mapping, [_provider("first"), _provider("second")], None
    )
    assert result is match
    assert ctrl.search.await_count == 2


@pytest.mark.asyncio
async def test_no_match_after_a_failed_provider_is_incomplete() -> None:
    """A clean miss on a later provider does not make up for an earlier provider that failed."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=[RetriesExhausted("rate limited"), []])
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers(
            ctrl, mapping, [_provider("first"), _provider("second")], None
        )
    assert ctrl.search.await_count == 2


def _mass_with_musicbrainz(musicbrainz: Mock) -> Mock:
    """Return a mass stand-in with all providers settled and the given MusicBrainz provider."""
    mass = _mass(ProviderStatus.LOADED)
    mass.get_provider = Mock(return_value=musicbrainz)
    mass.music.tracks.get_provider_item = AsyncMock(return_value=_track())
    return mass


def _musicbrainz() -> Mock:
    """Return a MusicBrainz provider stand-in that knows the recording."""
    musicbrainz = Mock()
    musicbrainz.get_recording_details = AsyncMock(return_value=Mock(relations=[]))
    return musicbrainz


def _linked(mappings: list[ProviderMapping]) -> Any:
    """Patch the link-to-mapping helper to hand out the given mappings."""
    return patch(
        "music_assistant.providers.lastfm_recommendations.parsers.provider_mappings_from_urls",
        AsyncMock(return_value=mappings),
    )


@pytest.mark.asyncio
async def test_musicbrainz_link_is_fetched_instead_of_searched() -> None:
    """A track MusicBrainz links to the user's provider is fetched by id, never searched for."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    with _linked([APPLE_MUSIC_MAPPING]) as linked:
        resolved = await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    assert resolved is not None
    assert resolved.name == "Chasing Cars"
    linked.assert_awaited_once_with(mass, [], MediaType.TRACK, exclude_domains=set())
    mass.music.tracks.get_provider_item.assert_awaited_once_with(
        "1", "apple_music--1", allow_fallback=False, strict_provider_instance=True
    )
    mass.music.tracks.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_linked_item_is_fetched_from_the_users_own_account() -> None:
    """A link mapped to another account of the same service is fetched from the user's own."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    other_account = ProviderMapping(
        item_id="1", provider_domain="apple_music", provider_instance="apple_music--2"
    )
    with _linked([other_account]):
        await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    assert mass.music.tracks.get_provider_item.await_args.args == ("1", "apple_music--1")


@pytest.mark.parametrize(
    ("media_type", "lookup"),
    [
        (MediaType.ARTIST, "get_artist_details"),
        (MediaType.ALBUM, "get_release_details"),
        (MediaType.TRACK, "get_recording_details"),
    ],
)
@pytest.mark.asyncio
async def test_each_media_type_is_looked_up_as_its_musicbrainz_entity(
    media_type: MediaType, lookup: str
) -> None:
    """An artist id names an artist, an album id a release and a track id a recording."""
    mapping = ItemMapping(media_type=media_type, item_id="temp", provider="x", name="a")
    mapping.mbid = MBID
    musicbrainz = Mock(**{lookup: AsyncMock(return_value=Mock(relations=[]))})
    mass = Mock()
    mass.get_provider = Mock(return_value=musicbrainz)
    with _linked([]):
        assert await parsers._resolve_via_musicbrainz(Mock(), mapping, mass, [], None) is None
    getattr(musicbrainz, lookup).assert_awaited_once_with(MBID)


@pytest.mark.asyncio
async def test_no_link_to_the_users_providers_is_searched() -> None:
    """A track MusicBrainz links to none of the user's providers goes to the name search."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    spotify = ProviderMapping(
        item_id="1", provider_domain="spotify", provider_instance="spotify--1"
    )
    with _linked([spotify]):
        await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    mass.music.tracks.get_provider_item.assert_not_awaited()
    mass.music.tracks.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_musicbrainz_trouble_leaves_the_search_to_decide() -> None:
    """A failed MusicBrainz lookup runs the name search, whose clean miss is still a miss."""
    musicbrainz = Mock()
    musicbrainz.get_recording_details = AsyncMock(side_effect=RetriesExhausted("rate limited"))
    mass = _mass_with_musicbrainz(musicbrainz)
    assert await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID) is None
    mass.music.tracks.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_linked_item_the_provider_no_longer_has_is_searched() -> None:
    """A MusicBrainz link whose item is gone from the provider goes to the name search."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    mass.music.tracks.get_provider_item = AsyncMock(side_effect=MediaNotFoundError("gone"))
    with _linked([APPLE_MUSIC_MAPPING]):
        await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    mass.music.tracks.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_linked_item_with_another_name_is_searched() -> None:
    """A MusicBrainz link to an item of another name is not trusted over the name search."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    mass.music.tracks.get_provider_item = AsyncMock(
        return_value=Track(item_id="2", provider="apple_music", name="Run", provider_mappings=set())
    )
    with _linked([APPLE_MUSIC_MAPPING]):
        await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    mass.music.tracks.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_item_fetched_via_musicbrainz_prefers_the_library_copy() -> None:
    """The user's own copy of a track fetched through MusicBrainz wins over the provider item."""
    mass = _mass_with_musicbrainz(_musicbrainz())
    fetched = _track()
    fetched.external_ids = {(ExternalID.ISRC, "GBUM70502337")}
    library_copy = Track(
        item_id="7", provider="library", name="Chasing Cars", provider_mappings=set()
    )
    mass.music.tracks.get_provider_item = AsyncMock(return_value=fetched)
    mass.music.tracks.get_library_item_by_external_ids = AsyncMock(side_effect=[None, library_copy])
    with _linked([APPLE_MUSIC_MAPPING]):
        resolved = await parsers.parse_track(LASTFM_TRACK_WITH_MBID, mass, INSTANCE_ID)
    assert resolved is library_copy
    library_lookups = mass.music.tracks.get_library_item_by_external_ids.await_args_list
    assert library_lookups[-1].args == (fetched.external_ids,)
    mass.music.tracks.search.assert_not_awaited()
