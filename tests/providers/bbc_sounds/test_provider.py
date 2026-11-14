"""Tests for the BBC Sounds provider's station, playback and on-demand handling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from auntie_sounds.models import (
    Container,
    Menu,
    MenuItem,
    Playlist,
    Podcast,
    RadioClip,
    Segment,
    SegmentOffset,
    Synopses,
    Titles,
)
from auntie_sounds.playback import PlayStatus
from auntie_sounds.schedule import catch_up_dates, schedule_dates
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder, PodcastEpisode, Track
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.bbc_sounds import BBCSoundsProvider

from .conftest import RADIO_FOUR_SERVICE_ID


def _mock(service: object) -> AsyncMock:
    """Return a client service, which the client fixture replaces with a mock, as that mock."""
    assert isinstance(service, AsyncMock)
    return service


def _segment() -> Segment:
    """Build a track that plays for the first minute of a programme."""
    return Segment(
        id="segment",
        segment_type="music",
        uris=[],
        titles=Titles(primary="Artist", secondary="Song"),
        image_url="https://example.com/song.jpg",
        offset=SegmentOffset(start=0, end=60),
    )


class TestOnPlayed:
    """Tests for reporting play progress back to BBC Sounds."""

    async def test_play_status_is_sent_with_the_vpid(
        self, provider: BBCSoundsProvider, cache_tasks: list[asyncio.Future[Any]]
    ) -> None:
        """Test playing an episode sends its pid and version pid."""
        _mock(provider.client.content).get_vpid_from_pid.return_value = "vpid"

        await provider.on_played(
            MediaType.PODCAST_EPISODE, "pid", False, 120, Mock(), is_playing=True
        )

        _mock(provider.client.playback).update_episode_play_status.assert_awaited_once_with(
            pid="pid", vpid="vpid", action=PlayStatus.HEARTBEAT, elapsed_time=120
        )

    @pytest.mark.usefixtures("cache_tasks")
    async def test_play_status_is_skipped_without_a_vpid(self, provider: BBCSoundsProvider) -> None:
        """Test nothing is sent for an episode the API has no version pid for."""
        _mock(provider.client.content).get_vpid_from_pid.return_value = None

        await provider.on_played(
            MediaType.PODCAST_EPISODE, "pid", False, 120, Mock(), is_playing=True
        )

        _mock(provider.client.playback).update_episode_play_status.assert_not_awaited()


class TestStationBrowsing:
    """Tests for the catch-up and schedule menus, which list each station's dates."""

    @pytest.mark.parametrize(
        ("menu_id", "dates"),
        [("catch_up", catch_up_dates()), ("schedules", schedule_dates())],
    )
    async def test_station_lists_its_dates(
        self, provider: BBCSoundsProvider, menu_id: str, dates: list[Any]
    ) -> None:
        """Test a station opens a folder for each date, starting from today."""
        path = f"bbc_sounds://{menu_id}/{RADIO_FOUR_SERVICE_ID}"

        folders = await provider.browse(path)

        assert all(isinstance(folder, BrowseFolder) for folder in folders)
        paths = [folder.path for folder in folders if isinstance(folder, BrowseFolder)]
        assert paths == [f"{path}/{day}" for day in dates]
        assert folders[0].name == "Today"

    @pytest.mark.parametrize("menu_id", ["catch_up", "schedules"])
    async def test_station_date_gets_the_schedule(
        self, provider: BBCSoundsProvider, menu_id: str
    ) -> None:
        """Test a station's date folder opens that day's schedule."""
        _mock(provider.client.schedules).get_schedule.return_value = None

        await provider.browse(f"bbc_sounds://{menu_id}/{RADIO_FOUR_SERVICE_ID}/2000-01-01")

        _mock(provider.client.schedules).get_schedule.assert_awaited_once_with(
            station_id=RADIO_FOUR_SERVICE_ID, date="2000-01-01"
        )


class TestMenuPlaylists:
    """Playlists in the main menu come without their contents."""

    @pytest.mark.usefixtures("cache_tasks")
    async def test_playlist_in_a_rail_is_filled_in(self, provider: BBCSoundsProvider) -> None:
        """Test a playlist inside a menu rail lists the playlist's contents."""
        playlist = Playlist(id="playlist_pid", title="Playlist")
        provider.menu = Menu(
            sub_items=[
                MenuItem(
                    id="explore",
                    title="Explore",
                    sub_items=[MenuItem(id="podcasts", title="Podcasts", sub_items=[playlist])],
                )
            ]
        )
        clip = RadioClip(id="vpid", pid="clip_pid", titles=Titles(entity_title="Clip"))
        _mock(provider.client.content).get_playlist_contents.return_value = [clip]

        items = await provider.browse("bbc_sounds://explore/podcasts/playlist_pid")

        _mock(provider.client.content).get_playlist_contents.assert_awaited_once_with(
            pid="playlist_pid"
        )
        assert [item.item_id for item in items] == ["clip_pid"]
        assert isinstance(items[0], Track)


class TestOnDemandEpisodes:
    """Tests for looking up an on-demand episode by its pid."""

    @pytest.mark.usefixtures("cache_tasks")
    async def test_radio_clip_is_returned_as_an_episode(self, provider: BBCSoundsProvider) -> None:
        """Test a clip listed as a podcast episode can be looked up, keeping its own name."""
        _mock(provider.client.content).get_by_pid.return_value = RadioClip(
            id="vpid",
            pid="clip_pid",
            titles=Titles(entity_title="Clip title"),
            container=Podcast(
                type="brand",
                id="podcast_pid",
                title="Podcast",
                description=None,
                image_url=None,
                synopses=Synopses(),
                titles=Titles(),
                urn="urn:bbc:radio:brand:podcast_pid",
            ),
        )

        episode = await provider.get_podcast_episode("clip_pid")

        assert type(episode) is PodcastEpisode
        assert episode.name == "Clip title"

    @pytest.mark.usefixtures("cache_tasks")
    async def test_other_types_are_rejected(self, provider: BBCSoundsProvider) -> None:
        """Test a pid that isn't an episode is reported by its type."""
        _mock(provider.client.content).get_by_pid.return_value = Container(id="pid", title="Brand")

        with pytest.raises(Exception, match="Unexpected type Container for episode pid"):
            await provider.get_podcast_episode("pid")


class TestProgrammeSegments:
    """Tests for the songs played in an on-demand programme."""

    async def test_cache_miss_returns_and_stores_the_segments(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test the first lookup returns the API's segments as well as caching them."""
        segments = [_segment()]
        _mock(provider.client.content).get_show_segments.return_value = segments
        cache_set = AsyncMock()
        provider.mass.cache.get = AsyncMock(return_value=False)  # type: ignore[method-assign]
        provider.mass.cache.set = cache_set  # type: ignore[method-assign]

        assert await provider._get_programme_segments("vpid") == segments
        assert cache_set.await_args is not None
        cached = cache_set.await_args.kwargs["data"]
        assert [Segment.from_dict(item) for item in cached] == segments

    async def test_first_metadata_update_shows_the_track(self, provider: BBCSoundsProvider) -> None:
        """Test the now-playing track is shown before the segments are cached."""
        _mock(provider.client.content).get_show_segments.return_value = [_segment()]
        provider.mass.cache.get = AsyncMock(return_value=False)  # type: ignore[method-assign]
        provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
        stream_details = Mock(spec=StreamDetails)
        stream_details.data = {"vpid": "vpid", "pid": "pid"}
        stream_details.stream_metadata = StreamMetadata(title="Programme")

        await provider._update_on_demand_stream_metadata(stream_details, elapsed_time=30)

        assert stream_details.stream_metadata.title == "Song"
        assert stream_details.stream_metadata.artist == "Artist"

    async def test_empty_track_list_is_fetched_once_per_playback(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a programme without a track list isn't cached, or fetched every refresh."""
        _mock(provider.client.content).get_show_segments.return_value = []
        cache_set = AsyncMock()
        provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
        provider.mass.cache.set = cache_set  # type: ignore[method-assign]
        stream_details = Mock(spec=StreamDetails)
        stream_details.data = {"vpid": "vpid", "pid": "pid"}
        stream_details.stream_metadata = StreamMetadata(title="Programme")

        await provider._update_on_demand_stream_metadata(stream_details, elapsed_time=30)
        await provider._update_on_demand_stream_metadata(stream_details, elapsed_time=35)

        _mock(provider.client.content).get_show_segments.assert_awaited_once()
        cache_set.assert_not_awaited()
