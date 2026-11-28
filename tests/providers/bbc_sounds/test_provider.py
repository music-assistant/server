"""Tests for the BBC Sounds provider's station, playback and on-demand handling."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from auntie_sounds import exceptions
from auntie_sounds.models import (
    Container,
    Duration,
    Menu,
    MenuItem,
    Network,
    Playlist,
    Podcast,
    Progress,
    RadioClip,
    RadioShow,
    Schedule,
    ScheduleItem,
    Synopses,
    Titles,
)
from auntie_sounds.playback import PlayStatus
from auntie_sounds.schedule import catch_up_dates, schedule_dates
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    BrowseFolder,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
)

from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId

from .conftest import RADIO_FOUR_SERVICE_ID

if TYPE_CHECKING:
    from collections.abc import Callable


def _mock(service: object) -> AsyncMock:
    """Return a client service, which the client fixture replaces with a mock, as that mock."""
    assert isinstance(service, AsyncMock)
    return service


def _open_radio_four(provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Resolve any uri to Radio 4 in the library, and return the queue's mocked play_media."""
    station = Radio(
        item_id="1",
        provider="library",
        name="BBC Radio 4",
        provider_mappings={
            ProviderMapping(
                item_id=RADIO_FOUR_SERVICE_ID,
                provider_domain="bbc_sounds",
                provider_instance=provider.instance_id,
            )
        },
    )
    monkeypatch.setattr(provider.mass.music, "get_item_by_uri", AsyncMock(return_value=station))
    play_media = AsyncMock()
    monkeypatch.setattr(provider.mass.player_queues, "play_media", play_media)
    return play_media


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
        ("menu_id", "dates_of"),
        [("catch_up", catch_up_dates), ("schedules", schedule_dates)],
    )
    async def test_station_lists_its_dates(
        self, provider: BBCSoundsProvider, menu_id: str, dates_of: Callable[[], list[Any]]
    ) -> None:
        """Test a station opens a folder for each date, starting from today."""
        path = f"{provider.instance_id}://{menu_id}/{RADIO_FOUR_SERVICE_ID}"

        folders = await provider.browse_manager.browse(path)

        assert all(isinstance(folder, BrowseFolder) for folder in folders)
        paths = [folder.path for folder in folders if isinstance(folder, BrowseFolder)]
        # the dates are worked out now: the tests can be collected on the day before
        assert paths == [f"{path}/{day}" for day in dates_of()]
        assert folders[0].name == "Today"

    @pytest.mark.parametrize("menu_id", ["catch_up", "schedules"])
    async def test_station_date_gets_the_schedule(
        self, provider: BBCSoundsProvider, menu_id: str
    ) -> None:
        """Test a station's date folder opens that day's schedule."""
        _mock(provider.client.schedules).get_schedule.return_value = None

        await provider.browse_manager.browse(
            f"{provider.instance_id}://{menu_id}/{RADIO_FOUR_SERVICE_ID}/2000-01-01"
        )

        _mock(provider.client.schedules).get_schedule.assert_awaited_once_with(
            station_id=RADIO_FOUR_SERVICE_ID, date="2000-01-01"
        )


class TestMenuPlaylists:
    """Playlists in the main menu come without their contents."""

    @pytest.mark.usefixtures("cache_tasks")
    async def test_playlist_in_a_rail_is_filled_in(self, provider: BBCSoundsProvider) -> None:
        """Test a playlist inside a menu rail lists the playlist's contents."""
        playlist = Playlist(id="playlist_pid", title="Playlist")
        provider.browse_manager.menu = Menu(
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

        items = await provider.browse_manager.browse(
            f"{provider.instance_id}://explore/podcasts/playlist_pid"
        )

        _mock(provider.client.content).get_playlist_contents.assert_awaited_once_with(
            pid="playlist_pid"
        )
        assert [item.item_id for item in items] == ["clip_pid"]
        assert isinstance(items[0], Track)


class TestOnDemandEpisodes:
    """Tests for looking up an on-demand episode by its pid."""

    @pytest.mark.usefixtures("cache_tasks")
    async def test_radio_clip_is_returned_as_an_episode(self, provider: BBCSoundsProvider) -> None:
        """Test a clip listed as a podcast episode can be looked up, named with its programme."""
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
    async def test_radio_show_keeps_the_date_it_was_broadcast(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a show played from a schedule is dated with its broadcast time."""

        def show(start: datetime | None = None, end: datetime | None = None) -> RadioShow:
            return RadioShow(
                id="vpid",
                pid="show_pid",
                titles=Titles(primary="Today", secondary="Business news"),
                duration=Duration(label="", value=3600),
                container=Podcast(id="brand_pid", title="Today", titles=Titles(primary="Today")),
                start=start,
                end=end,
            )

        # 05:30 UTC is 06:30 in the UK in October
        start = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
        end = datetime(2026, 10, 12, 6, 30, tzinfo=UTC)
        _mock(provider.client.schedules).get_schedule.return_value = Schedule(
            id="schedule", sub_items=[show(start, end)]
        )
        await provider.browse_manager.browse(
            f"{provider.instance_id}://schedules/{RADIO_FOUR_SERVICE_ID}/2026-10-12"
        )
        aired_show = show()
        _mock(provider.client.content).get_by_pid.return_value = aired_show

        episode = await provider.get_podcast_episode("show_pid")

        assert episode.metadata.release_date == start
        assert (aired_show.start, aired_show.end) == (start, end)

    @pytest.mark.usefixtures("cache_tasks")
    async def test_other_types_are_rejected(self, provider: BBCSoundsProvider) -> None:
        """Test a pid that isn't an episode is reported by its type."""
        _mock(provider.client.content).get_by_pid.return_value = Container(id="pid", title="Brand")

        with pytest.raises(Exception, match="Unexpected type Container for episode pid"):
            await provider.get_podcast_episode("pid")


class TestResumePosition:
    """Tests for reading the signed-in listener's progress through an episode."""

    @pytest.mark.parametrize(
        ("progress", "position_ms"),
        [(Progress(label="11 mins left", value=1011), 1_011_000), (None, 0)],
        ids=["part_played", "not_played"],
    )
    async def test_progress_is_returned_in_milliseconds(
        self, provider: BBCSoundsProvider, progress: Progress | None, position_ms: int
    ) -> None:
        """Test the seconds played from the API are returned as a resume position."""
        _mock(provider.client.content).get_by_pid.return_value = RadioClip(
            id="vpid", pid="clip_pid", titles=Titles(), progress=progress
        )

        result = await provider.get_resume_position("clip_pid", MediaType.PODCAST_EPISODE)

        assert result == (False, position_ms, None)

    @pytest.mark.parametrize(
        ("signed_in", "item_id", "lookup_error"),
        [
            (False, "clip_pid", None),
            (True, LiveProgrammeId(RADIO_FOUR_SERVICE_ID, "m0031hzl", 0, 3600).item_id, None),
            (True, "clip_pid", exceptions.NotFoundError("Not found")),
        ],
        ids=["signed_out", "live_programme", "not_found"],
    )
    async def test_unknown_progress_is_not_implemented(
        self,
        provider: BBCSoundsProvider,
        monkeypatch: pytest.MonkeyPatch,
        signed_in: bool,
        item_id: str,
        lookup_error: exceptions.SoundsException | None,
    ) -> None:
        """Test a position the API can't give raises NotImplementedError, so MA uses its own."""
        monkeypatch.setattr(provider.client, "is_signed_in", signed_in)
        _mock(provider.client.content).get_by_pid.side_effect = lookup_error

        with pytest.raises(NotImplementedError):
            await provider.get_resume_position(item_id, MediaType.PODCAST_EPISODE)


class TestMenuActionsListenFromStart:
    """Tests for the menu action that plays a station's currently on-air programme."""

    async def test_programme_on_air_is_played(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the programme on air is played on the player as a live programme."""
        play_media = _open_radio_four(provider, monkeypatch)
        start = datetime.now(UTC) - timedelta(minutes=30)
        end = start + timedelta(hours=1)
        _mock(provider.client.schedules).on_air.return_value = ScheduleItem(
            id="p0p6m2kp",
            urn="urn:bbc:radio:episode:m0031hzl",
            titles=Titles(primary="Today"),
            network=Network(id=RADIO_FOUR_SERVICE_ID),
            start=start,
            end=end,
        )

        await provider.handle_menu_action("listen_from_start", "library://radio/1", "player")

        _mock(provider.client.schedules).on_air.assert_awaited_once_with(RADIO_FOUR_SERVICE_ID)
        play_media.assert_awaited_once_with(
            "player", ANY, option=QueueOption.PLAY, start_position=0
        )
        assert play_media.await_args is not None
        episode = play_media.await_args.args[1]
        assert isinstance(episode, PodcastEpisode)
        programme = LiveProgrammeId(
            RADIO_FOUR_SERVICE_ID, "m0031hzl", int(start.timestamp()), int(end.timestamp())
        )
        assert episode.item_id == programme.item_id

    async def test_nothing_on_air_is_an_error(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a station with nothing on air raises an error and doesn't play."""
        play_media = _open_radio_four(provider, monkeypatch)
        _mock(provider.client.schedules).on_air.return_value = None

        with pytest.raises(MediaNotFoundError, match="Nothing is on air on BBC Radio 4"):
            await provider.handle_menu_action("listen_from_start", "library://radio/1", "player")

        play_media.assert_not_awaited()
