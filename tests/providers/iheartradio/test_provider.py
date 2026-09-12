"""Tests for the iHeartRadio provider."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.controllers.streams.constants import STREAMDETAILS_INBAND_TITLE_KEY
from music_assistant.providers.iheartradio.constants import (
    PATH_CATALOG_ALBUM,
    PATH_LIVE_STATION,
    PATH_NOW_PLAYING,
    PATH_PODCAST,
    PATH_PODCAST_CATEGORIES,
    PATH_PODCAST_EPISODES,
    PATH_SEARCH,
)
from music_assistant.providers.iheartradio.provider import IHeartRadioProvider

from .conftest import EPISODE, NOW_PLAYING, PODCAST, STATION, FakeApi

STATION_ID = str(STATION["id"])
PODCAST_ID = str(PODCAST["id"])


async def test_search(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """Search asks only for the requested types and caps every type at the limit."""
    api.responses[PATH_SEARCH] = {
        "results": {
            "stations": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
            "podcasts": [{"id": 3, "title": "Three"}],
        }
    }
    results = await provider.search("kiis", [MediaType.RADIO], limit=1)
    assert [radio.name for radio in results.radio] == ["One"]
    assert results.podcasts == []
    _, params = api.calls[-1]
    assert params["station"] == "true"
    assert params["podcast"] == "false"
    assert (await provider.search("kiis", [MediaType.TRACK])).radio == []


async def test_podcast_episodes_newest_has_highest_position(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """Episodes are fetched across pages and ranked so the newest holds the highest position."""
    path = PATH_PODCAST_EPISODES.format(podcast_id=PODCAST_ID)
    newest = {**EPISODE, "id": 3, "startDate": 3000}
    middle = {**EPISODE, "id": 2, "startDate": 2000}
    oldest = {**EPISODE, "id": 1, "startDate": 1000}
    api.pages[path] = [
        {"data": [newest, middle], "links": {"next": "cursor-2"}},
        {"data": [oldest], "links": {}},
    ]
    api.responses[PATH_PODCAST.format(podcast_id=PODCAST_ID)] = PODCAST
    episodes = [episode async for episode in provider.get_podcast_episodes(PODCAST_ID)]
    assert [(episode.item_id, episode.position) for episode in episodes] == [
        (f"{PODCAST_ID}:3", 3),
        (f"{PODCAST_ID}:2", 2),
        (f"{PODCAST_ID}:1", 1),
    ]
    assert api.calls[-1][1]["pageKey"] == "cursor-2"


async def test_station_stream_with_now_playing(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """A station that reports track metadata owns the stream metadata and refreshes it."""
    api.responses[PATH_LIVE_STATION.format(station_id=STATION_ID)] = {"hits": [STATION]}
    api.responses[PATH_NOW_PLAYING.format(station_id=STATION_ID)] = NOW_PLAYING
    details = await provider.get_stream_details(STATION_ID, MediaType.RADIO)
    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://example.com/kiis.m3u8"
    assert details.stream_metadata_update_callback is not None
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Red Rocks"
    # between tracks the station answers with an empty body, the in-band title takes over
    api.responses[PATH_NOW_PLAYING.format(station_id=STATION_ID)] = None
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "KIIS - Ad break"
    await details.stream_metadata_update_callback(details, 30)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "KIIS - Ad break"
    assert details.stream_metadata.image_url == STATION["logo"]


async def test_station_stream_without_now_playing(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """A station that publishes no track metadata leaves the metadata to the stream itself."""
    api.responses[PATH_LIVE_STATION.format(station_id=STATION_ID)] = {"hits": [STATION]}
    api.responses[PATH_NOW_PLAYING.format(station_id=STATION_ID)] = MediaNotFoundError(
        "No meta data"
    )
    details = await provider.get_stream_details(STATION_ID, MediaType.RADIO)
    assert details.stream_metadata_update_callback is None
    assert details.stream_metadata is None


async def test_unknown_station(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """A station the API does not know raises MediaNotFoundError."""
    api.responses[PATH_LIVE_STATION.format(station_id="1")] = {"hits": []}
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("1")


async def test_podcast_categories_skip_placeholder_artwork(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """A category whose artwork encodes a bare host gets no image, a real one keeps it."""
    api.responses[PATH_PODCAST_CATEGORIES] = {
        "categories": [
            {
                "id": 76,
                "name": "Featured",
                "image": "https://i.iheart.com/v3/url/aHR0cDovL2NvbnRlbnQuaWhlYXJ0LmNvbQ",
            },
            {
                "id": 2,
                "name": "Business",
                "image": "https://i.iheart.com/v3/url/aHR0cDovL2NvbnRlbnQuaWhlYXJ0LmNvbS90YWxrL2pwZy8yLmpwZw",
            },
        ]
    }
    folders = await provider.browse("iheartradio--test123://podcasts")
    assert [(folder.name, folder.image is not None) for folder in folders] == [
        ("Featured", False),
        ("Business", True),
    ]


async def test_album_tracks_are_listed_but_unavailable(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """An album lists its tracks with the album's artwork, none of them playable."""
    api.responses[PATH_CATALOG_ALBUM.format(album_id="607279")] = {
        "albumId": 607279,
        "title": "Full Moon Fever",
        "artistId": 1805,
        "artistName": "Tom Petty",
        "image": "http://image.iheart.com/full-moon-fever.jpg",
        "tracks": [{"id": 607283, "title": "Free Fallin'", "trackNumber": 1, "duration": 254}],
    }
    tracks = await provider.get_album_tracks("607279")
    assert [(track.name, track.track_number, track.available) for track in tracks] == [
        ("Free Fallin'", 1, False)
    ]
    assert tracks[0].album is not None
    assert tracks[0].album.item_id == "607279"
    assert tracks[0].image is not None
    assert tracks[0].image.path == "http://image.iheart.com/full-moon-fever.jpg"
