"""Test Nugs search using reduced responses captured from the public catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError, RateLimited
from music_assistant_models.media_items import ItemMapping, Track

from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.nugs import NugsProvider
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _response(name: str) -> dict[str, Any]:
    """Load a reduced live response, with audio preview URLs removed."""
    data = json.loads((Path(__file__).parent / "fixtures" / f"search_{name}.json").read_text())
    assert isinstance(data, dict)
    return data


def _stub_search(provider: NugsProvider, name: str) -> AsyncMock:
    """Stub catalog transport and track cache writes."""
    request = AsyncMock(return_value=_response(name)["Response"])
    provider._get_search_data = request  # type: ignore[method-assign]
    provider._get_catalog_search = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_song_search_preserves_performance_identity(provider: NugsProvider) -> None:
    """Song results identify recordings, carry album context, and need no detail fetches."""
    _stub_search(provider, "song")
    provider.get_album_tracks = AsyncMock()  # type: ignore[method-assign]
    result = await provider.search("Dust in a Baggie", [MediaType.TRACK, MediaType.ARTIST], 2)

    assert len(result.tracks) == 2
    assert not result.artists
    first = result.tracks[0]
    assert isinstance(first, Track)
    assert first.item_id == "504753"
    assert first.name == "Dust In A Baggie"
    assert isinstance(first.album, ItemMapping)
    assert first.album.item_id == "11170"
    assert first.artists[0].item_id == "978"
    assert isinstance(first.artists[0], ItemMapping)
    provider.get_album_tracks.assert_not_awaited()
    writes = cast("AsyncMock", provider.mass.cache.set).call_args_list
    assert writes[0].args[0] == "nugs_search_track_504753"
    assert writes[0].kwargs["persistent"] is False
    assert writes[0].kwargs["expiration"] == 3600 * 24
    assert writes[0].kwargs["provider"] == provider.instance_id


@pytest.mark.asyncio
async def test_track_resolution_hydrates_search_hit(provider: NugsProvider) -> None:
    """Opening a search hit resolves full metadata through its saved album ID."""
    _stub_search(provider, "song")
    result = await provider.search("Dust in a Baggie", [MediaType.TRACK], 1)
    partial = result.tracks[0]
    full = Track.from_dict({**partial.to_dict(), "duration": 150})
    provider.mass.cache.get = AsyncMock(side_effect=[None, partial])  # type: ignore[method-assign]
    provider.get_album_tracks = AsyncMock(return_value=[full])  # type: ignore[method-assign]

    resolved = await provider.get_track(partial.item_id)

    assert resolved.duration == 150
    provider.get_album_tracks.assert_awaited_once_with("11170")
    assert cast("AsyncMock", provider.mass.cache.set).call_args.args[1]["duration"] == 150


@pytest.mark.asyncio
async def test_missing_search_track_raises(provider: NugsProvider) -> None:
    """A removed recording does not resolve to another performance of the same song."""
    _stub_search(provider, "song")
    partial = (await provider.search("Dust in a Baggie", [MediaType.TRACK], 1)).tracks[0]
    provider.mass.cache.get = AsyncMock(side_effect=[None, partial])  # type: ignore[method-assign]
    provider.get_album_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    with pytest.raises(MediaNotFoundError):
        await provider.get_track(partial.item_id)


@pytest.mark.asyncio
async def test_search_track_survives_restart_and_resolves_album(
    provider: NugsProvider, mass_minimal: MusicAssistant
) -> None:
    """The real cache retains search context and full album lookup uses the same IDs."""
    provider.mass = mass_minimal
    await mass_minimal.cache.setup(Mock())
    provider._get_catalog_search = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider._get_search_data = AsyncMock(  # type: ignore[method-assign]
        return_value=_response("song")["Response"]
    )
    result = await provider.search("Dust in a Baggie", [MediaType.TRACK], 1)
    hit = result.tracks[0]
    assert isinstance(hit, Track)
    assert hit.duration == 0
    await mass_minimal.cache.close()
    await mass_minimal.cache.setup(Mock())

    album_data = json.loads((Path(__file__).parent / "fixtures" / "album.json").read_text())
    request = AsyncMock(return_value=album_data)
    provider._get_data = request  # type: ignore[method-assign]
    provider.get_artist = AsyncMock(  # type: ignore[method-assign]
        return_value=provider._parse_artist(album_data["Response"])
    )
    resolved = await provider.get_track(hit.item_id)

    assert resolved.item_id == "504753"
    assert resolved.name == hit.name
    assert resolved.duration == 150
    assert resolved.album is not None
    assert hit.album is not None
    assert resolved.album.item_id == hit.album.item_id == "11170"
    request.assert_awaited_once_with("catalog", "shows/11170")
    cached = await provider.get_track(hit.item_id)
    assert cached.duration == 150
    request.assert_awaited_once()
    await provider.search("Dust in a Baggie", [MediaType.TRACK], 1)
    cached_after_search = await provider.get_track(hit.item_id)
    assert cached_after_search.duration == 150
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_and_unavailable_hits_do_not_consume_limit(provider: NugsProvider) -> None:
    """Repeated hits and preorders cannot displace later available results."""
    request = _stub_search(provider, "song")
    data = request.return_value
    section = data["catalogSearchTypeContainers"][0]["catalogSearchContainers"][0]
    first, second = section["catalogSearchResultItems"]
    section["catalogSearchResultItems"] = [
        {**first, "availability": 2},
        first,
        first,
        second,
    ]
    result = await provider.search("Dust", [MediaType.TRACK], 2)
    assert [track.item_id for track in result.tracks] == [
        str(first["trackID"]),
        str(second["trackID"]),
    ]
    assert not result.albums


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_id", ["trackID", "artistID", "containerID"])
async def test_malformed_recording_ids_do_not_displace_valid_hit(
    provider: NugsProvider, missing_id: str
) -> None:
    """Incomplete catalog rows cannot become playable results or consume the limit."""
    request = _stub_search(provider, "song")
    section = request.return_value["catalogSearchTypeContainers"][0]["catalogSearchContainers"][0]
    first = section["catalogSearchResultItems"][0]
    section["catalogSearchResultItems"] = [{**first, "trackID": 999999, missing_id: None}, first]

    result = await provider.search("Dust", [MediaType.TRACK], 1)

    assert [track.item_id for track in result.tracks] == [str(first["trackID"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK])
async def test_search_respects_requested_type(
    provider: NugsProvider, media_type: MediaType
) -> None:
    """Each category is independently limited and unrequested types remain empty."""
    _stub_search(provider, "song")
    provider._get_catalog_search = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "artists": [{"id": 1125, "name": "Billy Strings"}],
            "albums": [
                {"id": 123, "title": "Test Album", "coverImage": "https://example.com/a.jpg"}
            ],
        }
    )
    result = await provider.search("unmatched", [media_type], 1)
    for item_type, items in (
        (MediaType.ARTIST, result.artists),
        (MediaType.ALBUM, result.albums),
        (MediaType.TRACK, result.tracks),
    ):
        assert len(items) == (1 if item_type == media_type else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "types", "limit"),
    [
        (" ", [MediaType.TRACK], 5),
        ("Billy", [MediaType.PLAYLIST], 5),
        ("Billy", [], 5),
        ("Billy", [MediaType.ARTIST], 0),
        ("Billy", [MediaType.TRACK], -1),
    ],
)
async def test_search_guards(
    provider: NugsProvider, query: str, types: list[MediaType], limit: int
) -> None:
    """Unsupported and empty requests do not touch the service."""
    request = _stub_search(provider, "artist")
    result = await provider.search(query, types, limit)
    assert not result.artists
    assert not result.albums
    assert not result.tracks
    request.assert_not_awaited()
    cast("AsyncMock", provider._get_catalog_search).assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_zero_duration_track_needs_no_lookup(provider: NugsProvider) -> None:
    """Existing complete cache entries are returned even when duration is zero."""
    track = provider._parse_track(
        {"trackID": 123, "songTitle": "Intro"},
        artist=provider._get_item_mapping(MediaType.ARTIST, "1125", "Billy Strings"),
        album=provider._get_item_mapping(MediaType.ALBUM, "11170", "Album"),
    )
    provider.mass.cache.get = AsyncMock(return_value=track)  # type: ignore[method-assign]
    provider.get_album_tracks = AsyncMock()  # type: ignore[method-assign]
    assert await provider.get_track("123") == track
    provider.get_album_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_matches(provider: NugsProvider) -> None:
    """A successful empty response returns empty categories."""
    _stub_search(provider, "empty")
    result = await provider.search("zxqvnonexistent427", [MediaType.ARTIST, MediaType.TRACK])
    assert not result.artists
    assert not result.albums
    assert not result.tracks


def _stub_transport(provider: NugsProvider, payload: Any, status: int = 200) -> MagicMock:
    """Install the HTTP context manager and real cache-decorator task scheduling."""
    response = AsyncMock()
    response.status = status
    response.headers = {"Retry-After": "5"}
    response.raise_for_status = MagicMock()
    response.text.return_value = json.dumps(payload)
    context = AsyncMock()
    context.__aenter__.return_value = response
    request = MagicMock(return_value=context)
    provider.mass.http_session.get = request  # type: ignore[method-assign]
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)
    provider.throttler = ThrottlerManager(rate_limit=1000)
    return request


@pytest.mark.asyncio
async def test_transport_params_and_cache(provider: NugsProvider) -> None:
    """Special characters stay in query parameters and fresh cache hits avoid HTTP."""
    payload = _response("artist")
    request = _stub_transport(provider, payload)
    query = "Umphrey's McGee & Friends"
    first = await provider._get_search_data(query)
    assert request.call_args.kwargs["params"] == {"method": "catalog.search", "searchStr": query}
    cast("AsyncMock", provider.mass.cache.get_with_freshness).return_value = (first, True, True)
    assert await provider._get_search_data(query) == first
    request.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"responseAvailabilityCode": 1, "Response": {}},
        {"responseAvailabilityCode": 0, "Response": None},
        {"responseAvailabilityCode": 0, "Response": {"searchError": 1}},
    ],
)
async def test_transport_rejects_api_errors(provider: NugsProvider, payload: Any) -> None:
    """An API error must not masquerade as a successful empty search."""
    _stub_transport(provider, payload)
    with pytest.raises(InvalidDataError):
        await provider._get_search_data("Billy")
    cast("AsyncMock", provider.mass.cache.set).assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_rate_limit(provider: NugsProvider) -> None:
    """The transport translates Retry-After into the shared throttler's error."""
    _stub_transport(provider, {}, status=429)
    transport = cast("Any", NugsProvider._get_search_data).__wrapped__.__wrapped__
    with pytest.raises(RateLimited) as exc:
        await transport(provider, "Billy")
    assert exc.value.backoff_time == 5


@pytest.mark.asyncio
async def test_core_track_search_capability(provider: NugsProvider) -> None:
    """The real track controller admits Nugs without claiming track library sync."""
    _stub_search(provider, "song")
    provider.manifest.type = ProviderType.MUSIC
    cast("Mock", provider.mass.get_provider).return_value = provider
    controller = TracksController(provider.mass)
    result = await controller.search("Dust in a Baggie", provider.instance_id, limit=1)
    assert len(result) == 1
    assert result[0].item_id == "504753"
    assert MediaType.TRACK in provider.supported_media_types
    assert ProviderFeature.LIBRARY_TRACKS not in provider.supported_features
    assert {MediaType.ARTIST, MediaType.ALBUM, MediaType.PLAYLIST} <= provider.supported_media_types


@pytest.mark.asyncio
async def test_artist_title_fallback_filters_before_limit(provider: NugsProvider) -> None:
    """Core matching queries find the requested artist rather than the first cover."""
    request = _stub_search(provider, "song")
    data = _response("song")["Response"]
    hits = data["catalogSearchTypeContainers"][0]["catalogSearchContainers"][0][
        "catalogSearchResultItems"
    ]
    hits[0]["artistName"] = "Other Artist"
    hits[1]["artistName"] = "Umphrey's McGee"
    request.side_effect = [_response("empty")["Response"], data]
    result = await provider.search("Umphreys McGee - Dust In A Baggie", [MediaType.TRACK], 1)
    assert [track.item_id for track in result.tracks] == [str(hits[1]["trackID"])]
    assert [call.args[0] for call in request.await_args_list] == [
        "Umphreys McGee - Dust In A Baggie",
        "Dust In A Baggie",
    ]


@pytest.mark.asyncio
async def test_literal_song_title_with_separator(provider: NugsProvider) -> None:
    """A successful literal song query is not reinterpreted as artist and title."""
    request = _stub_search(provider, "song")
    result = await provider.search("Song - Part Two", [MediaType.TRACK], 4)
    assert len(result.tracks) == 3
    request.assert_awaited_once_with("Song - Part Two")


@pytest.mark.asyncio
async def test_artist_title_fallback_is_bounded(provider: NugsProvider) -> None:
    """An empty artist-title lookup makes at most one fallback request."""
    request = _stub_search(provider, "empty")
    result = await provider.search("Artist - Song - Part Two", [MediaType.TRACK])
    assert not result.tracks
    assert request.await_count == 2
    assert request.await_args is not None
    assert request.await_args.args[0] == "Song - Part Two"
