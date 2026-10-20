"""Test the SiriusXM provider's library, search, browse and streaming behaviour."""

from __future__ import annotations

import time
from itertools import count
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from aiosxm import ArtistStation, NowPlaying, Talent
from aiosxm import SearchResults as SxmSearchResults
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnsupportedFeaturedException
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.siriusxm.constants import (
    BROWSE_CHANNELS,
    BROWSE_GENRES,
    BROWSE_LIBRARY,
    BROWSE_XTRA,
    CACHE_TTL_TRACKS,
)
from music_assistant.providers.siriusxm.parsers import queue_item_id, track_item_id

from .conftest import CHANNEL_ID, STATION_ID, make_channel, make_track

if TYPE_CHECKING:
    from aiosxm import Track as SxmTrack

    from music_assistant.providers.siriusxm.provider import SiriusXMProvider

BASE = "siriusxm--test123://"


async def test_get_library_radios_covers_live_channels_only(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """Only linear channels are radio; stations are dynamic playlists."""
    radios = [radio async for radio in provider.get_library_radios()]

    assert [radio.name for radio in radios] == ["SiriusXM Hits 1"]
    stub_client.get_library_channels.assert_awaited_once()


async def test_get_library_playlists(provider: SiriusXMProvider, stub_client: Mock) -> None:
    """Artist stations surface as dynamic, non-editable playlists."""
    playlists = [pl async for pl in provider.get_library_playlists()]

    assert [pl.name for pl in playlists] == ["Dean Martin"]
    station = playlists[0]
    assert station.is_dynamic is True
    assert station.is_editable is False
    assert station.item_id == queue_item_id("artist-station", STATION_ID)
    stub_client.get_library_artist_stations.assert_awaited_once()


@pytest.mark.usefixtures("stub_client")
async def test_get_playlist_tracks(provider: SiriusXMProvider) -> None:
    """A station yields real Track items with durations and artists."""
    tracks = await provider.get_playlist_tracks(queue_item_id("artist-station", STATION_ID))

    assert [t.name for t in tracks] == ["Volare", "That's Amore", "Sway"]
    assert all(t.duration == 131 for t in tracks)
    assert all(t.artists[0].name == "Dean Martin" for t in tracks)
    assert all(next(iter(t.provider_mappings)).available for t in tracks)


@pytest.mark.usefixtures("stub_client")
async def test_get_playlist_tracks_is_single_batch(provider: SiriusXMProvider) -> None:
    """Paging past the first batch ends it: walking would advance SiriusXM's cursor."""
    assert (
        await provider.get_playlist_tracks(queue_item_id("artist-station", STATION_ID), page=1)
        == []
    )


async def test_get_playlist_resolves_a_station(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """A composite id resolves through the station lookup, not the channel one."""
    playlist = await provider.get_playlist(queue_item_id("artist-station", STATION_ID))

    assert playlist.name == "Dean Martin"
    assert playlist.is_dynamic is True
    stub_client.get_artist_station.assert_awaited_once_with(STATION_ID)


async def test_library_edit_routes_stations(provider: SiriusXMProvider, stub_client: Mock) -> None:
    """Favouriting a station uses its own entity type, not the channel path."""
    item_id = queue_item_id("artist-station", STATION_ID)
    playlist = await provider.get_playlist(item_id)

    assert await provider.library_add(playlist) is True
    stub_client.add_to_library.assert_awaited_once_with("artist-station", STATION_ID)

    assert await provider.library_remove(item_id, MediaType.PLAYLIST) is True
    stub_client.remove_from_library.assert_awaited_once_with("artist-station", STATION_ID)


async def test_get_radio_resolves_catalog_items(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """Any catalog id resolves, not just the ones in the library."""
    radio = await provider.get_radio("rock-1")

    assert radio.name == "Classic Rewind"
    stub_client.get_channels.assert_awaited_once()


@pytest.mark.usefixtures("stub_client")
async def test_get_radio_unknown_id(provider: SiriusXMProvider) -> None:
    """An unknown channel id raises MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("does-not-exist")


async def test_library_add_and_remove(provider: SiriusXMProvider, stub_client: Mock) -> None:
    """Favourites are written back to the SiriusXM account."""
    radio = await provider.get_radio(CHANNEL_ID)

    assert await provider.library_add(radio) is True
    stub_client.add_to_library.assert_awaited_once_with("channel-linear", CHANNEL_ID)

    assert await provider.library_remove(CHANNEL_ID, MediaType.RADIO) is True
    stub_client.remove_from_library.assert_awaited_once_with("channel-linear", CHANNEL_ID)


async def test_library_edit_rejects_other_media_types(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """Only channels and stations can be saved to the account."""
    with pytest.raises(UnsupportedFeaturedException):
        await provider.library_remove("x", MediaType.TRACK)
    stub_client.remove_from_library.assert_not_awaited()


async def test_search(provider: SiriusXMProvider, stub_client: Mock) -> None:
    """Search returns catalog channels, honouring the limit."""
    stub_client.search_all = AsyncMock(
        return_value=SxmSearchResults(
            channels=[make_channel("rock-1", "Classic Rewind"), make_channel("rock-9", "Deep")]
        )
    )

    results = await provider.search("classic", [MediaType.RADIO], limit=1)

    assert [radio.name for radio in results.radio] == ["Classic Rewind"]


async def test_search_ignores_other_media_types(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """A search that doesn't ask for radio does no work."""
    stub_client.search_all = AsyncMock()

    results = await provider.search("classic", [MediaType.TRACK])

    assert not results.radio
    stub_client.search_all.assert_not_awaited()


@pytest.mark.usefixtures("stub_client")
async def test_browse_root(provider: SiriusXMProvider) -> None:
    """The browse root offers channels, genres and xtra channels."""
    items = await provider.browse(BASE)

    folders = [item for item in items if isinstance(item, BrowseFolder)]
    assert len(folders) == len(items)
    # The account's own items lead; the catalogue folders follow for discovery.
    assert [folder.item_id for folder in folders] == [
        BROWSE_LIBRARY,
        BROWSE_CHANNELS,
        BROWSE_XTRA,
        BROWSE_GENRES,
    ]
    assert [folder.translation_key for folder in folders] == [
        "library",
        "channels",
        "xtra_channels",
        "genres",
    ]


async def test_browse_library_is_one_listing(provider: SiriusXMProvider, stub_client: Mock) -> None:
    """Saved channels and stations appear together, sorted, not split by type."""
    items = await provider.browse(f"{BASE}{BROWSE_LIBRARY}")

    assert [item.name for item in items] == ["Dean Martin", "SiriusXM Hits 1"]
    assert {item.media_type.value for item in items} == {"playlist", "radio"}
    # One screen, one library call, not one per listing.
    stub_client.get_library_channels.assert_awaited_once()


@pytest.mark.usefixtures("stub_client")
async def test_browse_channels_excludes_xtra(
    provider: SiriusXMProvider,
) -> None:
    """The channels folder lists linear channels only, and xtra lists the rest."""
    linear = await provider.browse(f"{BASE}{BROWSE_CHANNELS}")
    xtra = await provider.browse(f"{BASE}{BROWSE_XTRA}")

    # Browse listings are numbered and ordered by dial position.
    # Browse listings are numbered and ordered by dial position.
    assert [item.name for item in linear] == [
        "2 - SiriusXM Hits 1",
        "25 - Classic Rewind",
        "40 - Deep Tracks",
    ]
    assert [item.name for item in xtra] == ["1125 - 1st Wave Deep Cuts"]
    # Xtra browses as dynamic playlists, not radio.
    assert all(getattr(item, "is_dynamic", False) for item in xtra)


@pytest.mark.usefixtures("stub_client")
async def test_browse_genres(provider: SiriusXMProvider) -> None:
    """Genres are listed busiest-first and drill down to their channels."""
    genres = await provider.browse(f"{BASE}{BROWSE_GENRES}")

    assert [item.name for item in genres] == ["Rock", "Pop"]

    rock = await provider.browse(f"{BASE}{BROWSE_GENRES}/Rock")
    assert [item.name for item in rock] == [
        "25 - Classic Rewind",
        "40 - Deep Tracks",
        "1125 - 1st Wave Deep Cuts",
    ]


@pytest.mark.usefixtures("stub_client")
async def test_browse_unknown_path(provider: SiriusXMProvider) -> None:
    """An unrecognised path yields nothing rather than raising."""
    assert await provider.browse(f"{BASE}nonsense") == []


@pytest.mark.usefixtures("stub_client")
async def test_get_stream_details(provider: SiriusXMProvider) -> None:
    """Stream details point at the local proxy and carry live metadata."""
    streamdetails = await provider.get_stream_details(CHANNEL_ID, MediaType.RADIO)

    assert streamdetails.stream_type == StreamType.HLS
    assert streamdetails.path == (
        f"http://127.0.0.1:8100/stream/channel-linear/{CHANNEL_ID}/playlist.m3u8?bitrate=256k"
    )
    assert streamdetails.allow_seek is False
    assert streamdetails.can_seek is False
    # Metadata is populated up front so the first frame isn't blank.
    assert streamdetails.stream_metadata is not None
    assert streamdetails.stream_metadata.title == "Le Freak"
    assert streamdetails.stream_metadata_update_callback is not None


@pytest.mark.usefixtures("stub_client")
async def test_get_stream_details_rejects_other_media_types(
    provider: SiriusXMProvider,
) -> None:
    """Only radio can be streamed."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(CHANNEL_ID, MediaType.TRACK)


async def test_update_stream_metadata_survives_errors(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """A failed metadata refresh never interrupts playback."""
    streamdetails = await provider.get_stream_details(CHANNEL_ID, MediaType.RADIO)
    before = streamdetails.stream_metadata
    stub_client.get_now_playing = AsyncMock(side_effect=RuntimeError("boom"))

    await provider.streaming_manager.update_stream_metadata(streamdetails, 30)

    assert streamdetails.stream_metadata is before


async def test_update_stream_metadata_clears_during_ads(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """An ad break drops the last track, so the player falls back to the channel."""
    streamdetails = await provider.get_stream_details(CHANNEL_ID, MediaType.RADIO)
    assert streamdetails.stream_metadata is not None
    stub_client.get_now_playing = AsyncMock(
        return_value=NowPlaying(
            channel_id=CHANNEL_ID, title="Some Advert", artist="Brand", is_ad=True
        )
    )

    await provider.streaming_manager.update_stream_metadata(streamdetails, 30)

    assert streamdetails.stream_metadata is None


@pytest.mark.usefixtures("stub_client")
async def test_library_names_are_not_numbered(provider: SiriusXMProvider) -> None:
    """The number prefix is for browsing; library items keep the plain name."""
    radios = [radio async for radio in provider.get_library_radios()]

    assert [radio.name for radio in radios] == ["SiriusXM Hits 1"]


@pytest.mark.usefixtures("stub_client")
async def test_track_stream_details_are_seekable(provider: SiriusXMProvider) -> None:
    """A station track is a real track: it has a duration and can be seeked."""
    item_id = track_item_id("artist-station", STATION_ID, "t1")
    streamdetails = await provider.get_stream_details(item_id, MediaType.TRACK)

    assert streamdetails.duration == 131
    assert streamdetails.can_seek is True
    assert streamdetails.allow_seek is True
    # A plain media file is fetched directly; only encrypted HLS needs the proxy.
    assert isinstance(streamdetails.path, str)
    assert streamdetails.path.endswith(".mp4")


@pytest.mark.usefixtures("stub_client")
async def test_unknown_track_raises(provider: SiriusXMProvider) -> None:
    """An id that no longer resolves is reported as missing, not played."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(
            track_item_id("artist-station", STATION_ID, "gone"), MediaType.TRACK
        )


@pytest.mark.usefixtures("stub_client")
async def test_listing_is_stable_until_played(provider: SiriusXMProvider) -> None:
    """
    Asking twice shows the same tracks: a listing must not consume the queue.

    SiriusXM's cursor only moves when we pull from it, so handing out a new
    batch per call is what made the displayed tracks differ from the played
    ones.
    """
    item_id = queue_item_id("artist-station", STATION_ID)

    first = await provider.get_playlist_tracks(item_id)
    second = await provider.get_playlist_tracks(item_id)

    assert [t.name for t in first] == ["Volare", "That's Amore", "Sway"]
    assert [t.name for t in second] == [t.name for t in first]


@pytest.mark.usefixtures("stub_client")
async def test_playing_a_track_advances_the_queue(provider: SiriusXMProvider) -> None:
    """Streaming a track moves the station past it, so the next listing is new."""
    item_id = queue_item_id("artist-station", STATION_ID)
    await provider.get_playlist_tracks(item_id)

    # play the last of the three shown tracks
    await provider.get_stream_details(
        track_item_id("artist-station", STATION_ID, "t3"), MediaType.TRACK
    )

    assert [t.name for t in await provider.get_playlist_tracks(item_id)] == [
        "Mambo Italiano",
        "Memories Are Made",
    ]


@pytest.mark.usefixtures("stub_client")
async def test_cached_tracks_do_not_grow_forever(
    provider: SiriusXMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing else empties the track cache, so a long-running server must not climb."""
    item_id = queue_item_id("artist-station", STATION_ID)
    batch = count()
    stream = Mock()
    stream.tracks = []

    async def next_tracks() -> list[SxmTrack]:
        n = next(batch)
        return [make_track(f"g{n}-{i}") for i in range(3)]

    stream.next_tracks = next_tracks
    provider.client.get_stream = AsyncMock(return_value=stream)  # type: ignore[method-assign]

    now = time.time()
    monkeypatch.setattr("music_assistant.providers.siriusxm.provider.time.time", lambda: now)
    for _ in range(20):
        now += CACHE_TTL_TRACKS / 4
        provider._upcoming[item_id] = []
        await provider.get_playlist_tracks(item_id)

    # holds only the batches still inside their signed-url window, not all 60
    assert len(provider._tracks) == 15


async def test_search_resolves_stations_via_matched_artists(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """
    A themed query finds stations through the artists SiriusXM matched.

    Searching stations by name alone barely matches anything for a term like
    "holiday"; the artists in the result set are what lead to their stations.
    """
    stub_client.search_all = AsyncMock(
        return_value=SxmSearchResults(
            channels=[],
            artist_stations=[],
            talent=[
                Talent(id="t1", title="Dean Martin"),
                Talent(id="t2", title="Frank Sinatra"),
            ],
        )
    )

    results = await provider.search("holiday", [MediaType.PLAYLIST], limit=10)

    assert sorted(p.name for p in results.playlists) == ["Dean Martin", "Frank Sinatra"]


async def test_search_uses_stations_from_the_search_response(
    provider: SiriusXMProvider, stub_client: Mock
) -> None:
    """Stations in the search response are enough on their own."""
    stub_client.search_all = AsyncMock(
        return_value=SxmSearchResults(
            channels=[],
            artist_stations=[ArtistStation(id="s-dean", title="Dean Martin")],
            talent=[Talent(id="t1", title="Dean Martin")],
        )
    )
    stub_client.search_artist_stations = AsyncMock(return_value=[])

    results = await provider.search("dean martin", [MediaType.PLAYLIST], limit=1)

    assert [p.name for p in results.playlists] == ["Dean Martin"]
    stub_client.search_artist_stations.assert_not_awaited()
