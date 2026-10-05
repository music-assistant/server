"""Shared fixtures for SiriusXM provider tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from aiosxm import ArtistStation, Channel, Image, NowPlaying, Track

from music_assistant.providers.siriusxm.browse import SiriusXMBrowseManager
from music_assistant.providers.siriusxm.provider import SUPPORTED_FEATURES, SiriusXMProvider
from music_assistant.providers.siriusxm.streaming import SiriusXMStreamingManager
from tests.common import use_real_create_task

CHANNEL_ID = "194adbca-34d6-cb94-b153-3488ee563308"
XTRA_CHANNEL_ID = "8a7b1e2c-0000-4444-9999-1234567890ab"
STATION_ID = "db2788c7-43ac-386b-86ce-ccc9f9b8c76f"


def make_channel(
    channel_id: str = CHANNEL_ID,
    title: str = "SiriusXM Hits 1",
    channel_type: str = "channel-linear",
    genre: str | None = "Pop",
    channel_number: str = "2",
    *,
    off_air: bool = False,
    unentitled: bool = False,
) -> Channel:
    """Build a Channel as aiosxm would return it."""
    return Channel(
        id=channel_id,
        type=channel_type,
        title=title,
        channel_number=channel_number,
        description="Today's hits",
        genre=genre,
        off_air=off_air,
        unentitled=unentitled,
        images=[
            Image(name="tile", aspect_ratio="1x1", key="tile-key"),
            Image(name="background", aspect_ratio="16x9", key="bg-key"),
        ],
    )


@pytest.fixture
def provider() -> SiriusXMProvider:
    """Create a real SiriusXMProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "siriusxm"
    config = Mock()
    config.instance_id = "siriusxm--test123"
    config.name = "SiriusXM Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = SiriusXMProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.client = Mock()
    provider.channels_by_id = {}
    provider._tracks = {}
    provider._streams = {}
    provider._upcoming = {}
    # Treat every call as a cache miss so tests see the real behaviour.
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)
    provider.browse_manager = SiriusXMBrowseManager(provider)
    provider.streaming_manager = SiriusXMStreamingManager(provider)
    provider._proxy_base_url = "http://127.0.0.1:8100"
    return provider


@pytest.fixture
def channels() -> list[Channel]:
    """Return a small catalog covering linear, xtra and off-air channels."""
    return [
        make_channel(),
        make_channel("rock-1", "Classic Rewind", genre="Rock", channel_number="25"),
        make_channel("rock-2", "Deep Tracks", genre="Rock", channel_number="40", off_air=True),
        make_channel(
            XTRA_CHANNEL_ID,
            "1st Wave Deep Cuts",
            "channel-xtra",
            genre="Rock",
            channel_number="1125",
        ),
    ]


@pytest.fixture
def stub_client(provider: SiriusXMProvider, channels: list[Channel]) -> Mock:
    """Attach a client stub returning the fixture catalog."""
    client = Mock()
    client.get_channels = AsyncMock(return_value=channels)
    client.get_library_channels = AsyncMock(return_value=channels[:1])
    client.get_library_artist_stations = AsyncMock(
        return_value=[
            ArtistStation(
                id=STATION_ID,
                title="Dean Martin",
                description="Rat Pack crooning",
                image_key="station-key",
            )
        ]
    )
    client.get_artist_station = AsyncMock(
        return_value=ArtistStation(id=STATION_ID, title="Dean Martin")
    )

    async def search_artist_stations(query: str) -> list[ArtistStation]:
        # The direct query matches nothing; the artists it turned up each have one.
        return {
            "Dean Martin": [ArtistStation(id="s-dean", title="Dean Martin")],
            "Frank Sinatra": [ArtistStation(id="s-frank", title="Frank Sinatra")],
        }.get(query, [])

    client.search_artist_stations = search_artist_stations
    client.get_genres = AsyncMock(return_value={"Rock": 3, "Pop": 1})
    client.add_to_library = AsyncMock()
    client.remove_from_library = AsyncMock()
    client.get_stream = AsyncMock(return_value=_stub_stream())
    client.get_now_playing = AsyncMock(
        return_value=NowPlaying(
            channel_id=CHANNEL_ID,
            title="Le Freak",
            artist="Chic",
            show="70s Hits",
            image_key="art-key",
        )
    )
    provider.client = client
    return client


def make_track(track_id: str = "t1", title: str = "Volare", artist: str = "Dean Martin") -> Track:
    """Build a queued Track as aiosxm would return it."""
    return Track(
        id=track_id,
        title=title,
        artist=artist,
        album="That's Amore",
        duration_ms=131344,
        url=f"https://cdn.example.com/clips/{track_id}.mp4",
        image_key="art-key",
    )


def _stub_stream() -> Mock:
    """Build a stream stub whose cursor advances, as the real one does."""
    stream = Mock()
    stream.is_track_queue = True
    stream.tracks = [
        make_track("t1", "Volare"),
        make_track("t2", "That's Amore"),
        make_track("t3", "Sway"),
    ]
    batches = [
        [make_track("t4", "Mambo Italiano"), make_track("t5", "Memories Are Made")],
        [make_track("t6", "Everybody Loves Somebody")],
    ]

    async def next_tracks() -> list[Track]:
        return batches.pop(0) if batches else []

    stream.next_tracks = next_tracks
    return stream
