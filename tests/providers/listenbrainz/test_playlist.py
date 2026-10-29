"""Tests for ListenBrainz playlist management and resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.parsers import JSPF_TRACK_EXTENSION
from music_assistant.providers.listenbrainz.playlist import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

LIBRARY_ARTIST_MBID = "11111111-1111-4111-8111-111111111111"
UNMATCHED_ARTIST_MBID = "22222222-2222-4222-8222-222222222222"
LIBRARY_RECORDING_MBID = "33333333-3333-4333-8333-333333333333"
UNMATCHED_RECORDING_MBID = "44444444-4444-4444-8444-444444444444"


def _playlist_manager(
    library_artist: Artist,
    library_track: Track | None = None,
    streaming_track: Track | None = None,
    persisted_track: Track | None = None,
) -> ListenBrainzPlaylistManager:
    """Create a playlist manager with optional library and streaming-provider items."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    mass = MagicMock()
    mass.music.tracks.get_library_item_by_external_id = AsyncMock(
        side_effect=lambda mbid, external_id_type: (
            library_track
            if mbid == LIBRARY_RECORDING_MBID and external_id_type == ExternalID.MB_RECORDING
            else None
        )
    )
    mass.music.artists.get_library_item_by_external_id = AsyncMock(
        side_effect=lambda mbid, external_id_type: (
            library_artist
            if mbid == LIBRARY_ARTIST_MBID and external_id_type == ExternalID.MB_ARTIST
            else None
        )
    )
    mass.music.tracks.search = AsyncMock(return_value=[streaming_track] if streaming_track else [])
    mass.cache.get = AsyncMock(return_value=persisted_track)
    mass.cache.set = AsyncMock()
    if streaming_track:
        streaming_provider = MagicMock(
            instance_id="test_music--instance",
            is_streaming_provider=True,
            available=True,
        )
        mass.music.providers = [streaming_provider]
    else:
        mass.music.providers = []
    provider.mass = cast("MusicAssistant", mass)
    return ListenBrainzPlaylistManager(provider)


def _track_data(
    recording_mbid: str, artists: list[tuple[str, str]], title: str = "Test Track"
) -> dict[str, object]:
    """Return a minimal raw ListenBrainz JSPF track."""
    return {
        "album": "Test Album",
        "duration": 180000,
        "extension": {
            JSPF_TRACK_EXTENSION: {
                "additional_metadata": {
                    "artists": [
                        {
                            "artist_credit_name": name,
                            "artist_mbid": mbid,
                            "join_phrase": "",
                        }
                        for name, mbid in artists
                    ]
                }
            }
        },
        "identifier": [f"https://musicbrainz.org/recording/{recording_mbid}"],
        "title": title,
    }


def _streaming_track(title: str = "Test Track", artist_name: str = "Library Artist") -> Track:
    """Return a synthetic provider-backed track."""
    streaming_artist = Artist(
        item_id="provider-artist-id",
        provider="test_music--instance",
        name=artist_name,
        provider_mappings=set(),
    )
    return Track(
        item_id="provider-track-id",
        provider="test_music--instance",
        name=title,
        duration=180,
        artists=UniqueList([streaming_artist]),
        provider_mappings={
            ProviderMapping(
                item_id="provider-track-id",
                provider_domain="test_music",
                provider_instance="test_music--instance",
            )
        },
    )


@pytest.mark.asyncio
async def test_playlist_tracks_link_artists_by_musicbrainz_id() -> None:
    """Known artist MBIDs link to library artists and unknown ones retain their MBIDs."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    manager = _playlist_manager(library_artist)
    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [
                ("Library Artist", LIBRARY_ARTIST_MBID),
                ("Unmatched Artist", UNMATCHED_ARTIST_MBID),
            ],
        ),
        1,
    )

    assert track.artists[0] is library_artist
    assert [artist.name for artist in track.artists] == [
        "Library Artist",
        "Unmatched Artist",
    ]
    assert track.artists[1].get_external_id(ExternalID.MB_ARTIST) == UNMATCHED_ARTIST_MBID


@pytest.mark.asyncio
async def test_playlist_tracks_reuse_artist_resolution_within_playlist() -> None:
    """Repeated resolved artist MBIDs require one library lookup during a playlist load."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    manager = _playlist_manager(library_artist)
    artist_cache: dict[str, Artist] = {}

    first = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
        artist_cache=artist_cache,
    )
    second = await manager._parse_track(
        _track_data(
            LIBRARY_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        2,
        artist_cache=artist_cache,
    )

    artist_lookup = cast(
        "AsyncMock", manager.provider.mass.music.artists.get_library_item_by_external_id
    )
    assert artist_lookup.await_count == 1
    assert first.artists[0] is library_artist
    assert second.artists[0] is library_artist


@pytest.mark.asyncio
async def test_playlist_tracks_do_not_cache_unresolved_artists() -> None:
    """Unavailable artist placeholders are reconsidered for every track."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    manager = _playlist_manager(library_artist)
    artist_cache: dict[str, Artist] = {}

    first = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Unmatched Artist", UNMATCHED_ARTIST_MBID)],
        ),
        1,
        artist_cache=artist_cache,
    )
    second = await manager._parse_track(
        _track_data(
            LIBRARY_RECORDING_MBID,
            [("Unmatched Artist", UNMATCHED_ARTIST_MBID)],
        ),
        2,
        artist_cache=artist_cache,
    )

    artist_lookup = cast(
        "AsyncMock", manager.provider.mass.music.artists.get_library_item_by_external_id
    )
    assert artist_lookup.await_count == 2
    assert first.artists[0] is not second.artists[0]
    assert UNMATCHED_ARTIST_MBID not in artist_cache


@pytest.mark.asyncio
async def test_playlist_tracks_reuse_library_track_by_recording_mbid() -> None:
    """An exact recording MBID match returns the existing playable library track."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    library_track = Track(
        item_id="7",
        provider="library",
        name="Library Track",
        duration=180,
        artists=UniqueList([library_artist]),
        provider_mappings={
            ProviderMapping(
                item_id="provider-track-id",
                provider_domain="test_music",
                provider_instance="test_music--instance",
            )
        },
        external_ids={(ExternalID.MB_RECORDING, LIBRARY_RECORDING_MBID)},
    )
    manager = _playlist_manager(library_artist, library_track)

    track = await manager._parse_track(
        _track_data(
            LIBRARY_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
            title="Library Track",
        ),
        1,
    )

    assert track is library_track
    assert track.position == 1
    cast("AsyncMock", manager.provider.mass.cache.get).assert_not_awaited()
    cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()


@pytest.mark.asyncio
async def test_playlist_tracks_reuse_matching_streaming_provider_track() -> None:
    """A provider search match is returned when the recording is absent from the library."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    streaming_track = _streaming_track()
    manager = _playlist_manager(library_artist, streaming_track=streaming_track)

    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
    )

    assert track is streaming_track
    assert track.position == 1
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_awaited_once_with(
        "Library Artist - Test Track", "test_music--instance", limit=10
    )


@pytest.mark.asyncio
async def test_playlist_tracks_reuse_persisted_streaming_provider_track() -> None:
    """A persisted provider match avoids another streaming-provider search."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    cached_track = _streaming_track()
    manager = _playlist_manager(library_artist, persisted_track=cached_track)

    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
    )

    assert track is cached_track
    assert track.position == 1
    cast("AsyncMock", manager.provider.mass.cache.get).assert_awaited_once_with(
        key=UNMATCHED_RECORDING_MBID,
        category=1,
        provider="listenbrainz--test",
        base_class=Track,
    )
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()


@pytest.mark.asyncio
async def test_playlist_tracks_persist_resolved_streaming_provider_track() -> None:
    """A successful streaming-provider resolution is persisted by recording MBID."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    streaming_track = _streaming_track()
    cached_data = streaming_track.to_dict()
    manager = _playlist_manager(library_artist, streaming_track=streaming_track)

    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
    )

    assert track is streaming_track
    cast("AsyncMock", manager.provider.mass.cache.set).assert_awaited_once_with(
        key=UNMATCHED_RECORDING_MBID,
        data=cached_data,
        category=1,
        provider="listenbrainz--test",
        expiration=60 * 60 * 24 * 30,
    )


@pytest.mark.asyncio
async def test_playlist_tracks_reject_streaming_track_with_different_title() -> None:
    """A provider result with a different title is not used for the playlist entry."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    candidate = _streaming_track(title="Different Track")
    manager = _playlist_manager(library_artist, streaming_track=candidate)

    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
    )

    assert track is not candidate
    assert track.provider == "listenbrainz--test"
    cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()


@pytest.mark.asyncio
async def test_playlist_tracks_reject_streaming_track_with_different_artist() -> None:
    """A same-title provider result by another artist is not used."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    candidate = _streaming_track(artist_name="Different Artist")
    manager = _playlist_manager(library_artist, streaming_track=candidate)

    track = await manager._parse_track(
        _track_data(
            UNMATCHED_RECORDING_MBID,
            [("Library Artist", LIBRARY_ARTIST_MBID)],
        ),
        1,
    )

    assert track is not candidate
    assert track.provider == "listenbrainz--test"


@pytest.mark.asyncio
async def test_playlist_tracks_cache_resolved_streaming_track() -> None:
    """A resolved provider track is reused without searching again."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    streaming_track = _streaming_track()
    manager = _playlist_manager(library_artist, streaming_track=streaming_track)
    item = _track_data(
        UNMATCHED_RECORDING_MBID,
        [("Library Artist", LIBRARY_ARTIST_MBID)],
    )

    first = await manager._parse_track(item, 1)
    second = await manager._parse_track(item, 2)

    assert first is streaming_track
    assert second is streaming_track
    assert cast("AsyncMock", manager.provider.mass.music.tracks.search).await_count == 1


@pytest.mark.asyncio
async def test_playlist_tracks_cache_resolved_library_track() -> None:
    """A resolved library track is reused without looking it up again."""
    library_artist = Artist(
        item_id="42", provider="library", name="Library Artist", provider_mappings=set()
    )
    library_track = Track(
        item_id="7",
        provider="library",
        name="Library Track",
        duration=180,
        artists=UniqueList([library_artist]),
        provider_mappings=set(),
        external_ids={(ExternalID.MB_RECORDING, LIBRARY_RECORDING_MBID)},
    )
    manager = _playlist_manager(library_artist, library_track=library_track)
    item = _track_data(
        LIBRARY_RECORDING_MBID,
        [("Library Artist", LIBRARY_ARTIST_MBID)],
        title="Library Track",
    )

    first = await manager._parse_track(item, 1)
    second = await manager._parse_track(item, 2)

    assert first is library_track
    assert second is library_track
    lookup = cast("AsyncMock", manager.provider.mass.music.tracks.get_library_item_by_external_id)
    assert lookup.await_count == 1
