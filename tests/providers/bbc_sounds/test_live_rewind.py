"""Tests for playing a BBC programme that is on air now from the station's rewind window."""

import logging
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from music_assistant_models.enums import MediaType, QueueOption, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items import PodcastEpisode as MAPodcastEpisode
from music_assistant_models.streamdetails import StreamDetails
from sounds import Podcast
from sounds.models import LiveStation, Network, Schedule, ScheduleItem

from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.live_rewind import (
    PROGRAMME_DATE_OFFSET,
    LiveProgrammeId,
    parse_timeline,
    rewind_playlist_url,
    stream_programme,
)
from tests.common import use_real_create_task

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://example.com/live/bbc_radio_one/bbc_radio_one.isml/"
REWIND_URL = f"{BASE_URL}bbc_radio_one-audio%3d320000.m3u8"
LIVE_URL = f"{BASE_URL}bbc_radio_one-audio%3d320000.norewind.m3u8"
WINDOW_START = datetime(2026, 9, 24, 5, 0, tzinfo=UTC).timestamp()
PROGRAMME = LiveProgrammeId("bbc_radio_one", "m0031hzl", 1790229600, 1790242200)


def _playlist(first_sequence: int, count: int, ended: bool = False) -> str:
    """Build a live playlist of 6 second segments, the first starting at WINDOW_START."""
    start = datetime.fromtimestamp(_segment_time(first_sequence) + PROGRAMME_DATE_OFFSET, tz=UTC)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-MEDIA-SEQUENCE:{first_sequence}",
        "#EXT-X-TARGETDURATION:6",
        f"#EXT-X-PROGRAM-DATE-TIME:{start.isoformat().replace('+00:00', 'Z')}",
    ]
    for sequence in range(first_sequence, first_sequence + count):
        lines += ["#EXTINF:6.0, no desc", f"seg-{sequence}.ts"]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def _segment_time(sequence: int) -> float:
    return WINDOW_START + (sequence - 100) * 6


async def _collect(
    fetch_text: AsyncMock,
    start: float,
    end: float,
    on_start: Callable[[float], None] | None = None,
    is_superseded: Callable[[], bool] | None = None,
) -> list[bytes]:
    async def fetch_bytes(url: str) -> bytes:
        return url.rsplit("/", 1)[1].encode()

    return [
        chunk
        async for chunk in stream_programme(
            fetch_text,
            fetch_bytes,
            REWIND_URL,
            LIVE_URL,
            start,
            end,
            LOGGER,
            on_start,
            is_superseded,
        )
    ]


def _playing_queue(
    monkeypatch: pytest.MonkeyPatch, provider: BBCSoundsProvider, streamdetails: object
) -> None:
    """Set up a queue whose current item is streamed with the given stream details."""
    queue = Mock(queue_id="queue_id", current_index=0)
    queue.current_item.streamdetails = streamdetails
    monkeypatch.setattr(provider.mass.player_queues, "get", Mock(return_value=queue))


def _programme_details() -> Mock:
    """Return stream details of PROGRAMME, as streamed to the queue "queue_id"."""
    details = Mock(item_id=PROGRAMME.item_id, queue_id="queue_id")
    details.data = {"live_url": LIVE_URL}
    return details


def _schedule_item(start: datetime, end: datetime, container: bool = True) -> ScheduleItem:
    return ScheduleItem(
        id="p0p6m2kp",
        urn="urn:bbc:radio:episode:m0031hzl",
        type="broadcast_summary",
        titles={"primary": "Radio 1 Breakfast", "secondary": "Guest host"},
        network=Network(id="bbc_radio_one", key="radio1", short_title="Radio 1"),
        container=(
            Podcast(
                type="brand",
                id="b0080x5m",
                title="Radio 1 Breakfast",
                description=None,
                image_url=None,
                synopses={},
                titles={},
                urn="urn:bbc:radio:brand:b0080x5m",
            )
            if container
            else None
        ),
        start=start,
        end=end,
    )


class TestLiveProgrammeId:
    """Tests for the item ids of live programmes."""

    def test_round_trip(self) -> None:
        """Test an id parses back to the same programme."""
        assert LiveProgrammeId.parse(PROGRAMME.item_id) == PROGRAMME
        assert PROGRAMME.duration == 12600

    @pytest.mark.parametrize(
        "item_id",
        ["m0031hzl", "live_programme~bbc_radio_one~m0031hzl~start~end", "other~a~b~1~2"],
    )
    def test_other_ids_are_not_parsed(self, item_id: str) -> None:
        """Test on-demand pids and malformed ids are not taken for live programmes."""
        assert LiveProgrammeId.parse(item_id) is None


class TestPlaylists:
    """Tests for reading the station's live playlists."""

    def test_rewind_url_drops_the_norewind_suffix(self) -> None:
        """Test the rewind playlist is derived from the live-edge playlist, keeping its query."""
        assert rewind_playlist_url(LIVE_URL) == REWIND_URL
        assert rewind_playlist_url(f"{LIVE_URL}?token=1") == f"{REWIND_URL}?token=1"

    def test_rewind_url_needs_a_norewind_playlist(self) -> None:
        """Test a stream without a rewind window is reported as such."""
        with pytest.raises(MediaNotFoundError):
            rewind_playlist_url(REWIND_URL)

    def test_timeline_times_every_segment(self) -> None:
        """Test segment start times follow from the playlist date and segment durations."""
        timeline = parse_timeline(_playlist(100, 4), REWIND_URL)
        assert [s.sequence for s in timeline.segments] == [100, 101, 102, 103]
        assert [s.start for s in timeline.segments] == [_segment_time(n) for n in range(100, 104)]
        assert timeline.segments[0].url == f"{BASE_URL}seg-100.ts"
        assert not timeline.ended

    def test_timeline_is_moved_back_to_broadcast_time(self) -> None:
        """Test segment times are the playlist dates less the BBC HLS offset."""
        playlist = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-TARGETDURATION:6\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-09-24T14:33:36Z\n#EXTINF:6.4,\nseg-1.ts"
        )
        timeline = parse_timeline(playlist, REWIND_URL)
        assert timeline.segments[0].start == datetime(2026, 9, 24, 14, 33, tzinfo=UTC).timestamp()

    def test_index_at_stays_behind_the_live_edge(self) -> None:
        """Test the start segment is found by time, and clamped a few segments from the edge."""
        timeline = parse_timeline(_playlist(100, 10), REWIND_URL)
        assert timeline.index_at(WINDOW_START - 60) == 0
        assert timeline.index_at(_segment_time(102) + 3) == 2
        assert timeline.index_at(_segment_time(109) + 60) == 7


class TestStreamProgramme:
    """Tests for streaming a programme from the rewind window."""

    async def test_streams_from_start_to_end(self) -> None:
        """Test streaming starts at the segment holding the start and stops at the end."""
        fetch_text = AsyncMock(return_value=_playlist(100, 20))
        chunks = await _collect(fetch_text, _segment_time(103) + 2, _segment_time(106))
        assert chunks == [b"seg-103.ts", b"seg-104.ts", b"seg-105.ts"]

    async def test_follows_the_live_edge(self) -> None:
        """Test new segments from the live-edge playlist are picked up until the end."""
        fetch_text = AsyncMock(
            side_effect=[_playlist(100, 6), _playlist(103, 3), _playlist(104, 3, ended=True)]
        )
        chunks = await _collect(fetch_text, _segment_time(103), _segment_time(110))
        assert chunks == [b"seg-103.ts", b"seg-104.ts", b"seg-105.ts", b"seg-106.ts"]
        assert fetch_text.await_args_list[1].args == (LIVE_URL,)

    async def test_returns_to_the_rewind_window_when_behind_the_live_playlist(self) -> None:
        """Test playback that fell behind the short live playlist continues from the rewind one."""
        fetch_text = AsyncMock(
            side_effect=[
                _playlist(100, 5),
                _playlist(120, 3),
                _playlist(100, 25, ended=True),
            ]
        )
        chunks = await _collect(fetch_text, _segment_time(101), _segment_time(107))
        assert chunks == [f"seg-{n}.ts".encode() for n in range(101, 107)]
        assert fetch_text.await_args_list[2].args == (REWIND_URL,)

    async def test_reports_where_it_starts(self) -> None:
        """Test the start of the segment playback actually starts from is reported."""
        fetch_text = AsyncMock(return_value=_playlist(100, 10))
        starts: list[float] = []

        await _collect(fetch_text, _segment_time(109) + 600, _segment_time(108), starts.append)

        assert starts == [_segment_time(107)]

    async def test_stops_once_superseded(self) -> None:
        """Test a stream nothing reads any more stops instead of following the live edge."""
        fetch_text = AsyncMock(return_value=_playlist(100, 20))
        checks = iter([False, False, False, True])

        chunks = await _collect(
            fetch_text,
            _segment_time(103),
            _segment_time(119),
            is_superseded=lambda: next(checks),
        )

        assert chunks == [b"seg-103.ts", b"seg-104.ts"]

    async def test_programme_older_than_the_window_is_not_found(self) -> None:
        """Test a programme that started before the rewind window can not be played."""
        fetch_text = AsyncMock(return_value=_playlist(100, 20))
        with pytest.raises(MediaNotFoundError):
            await _collect(fetch_text, WINDOW_START - 3600, WINDOW_START)


class TestLiveProgrammeConversion:
    """Tests for converting a programme on air now."""

    async def test_on_air_programme_is_playable_from_its_start(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test the programme on air now becomes an episode covering the whole programme."""
        now = datetime.now(tz=UTC)
        item = _schedule_item(now - timedelta(hours=3), now + timedelta(minutes=30))

        episode = await provider.adaptor.new_object(item)

        assert isinstance(episode, MAPodcastEpisode)
        programme_id = LiveProgrammeId.parse(episode.item_id)
        assert programme_id is not None
        assert programme_id.station_id == "bbc_radio_one"
        assert programme_id.pid == "m0031hzl"
        assert episode.duration == 3.5 * 3600

    async def test_programme_without_series_links_to_its_station(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a programme without a series still converts, with its station as the podcast."""
        now = datetime.now(tz=UTC)
        item = _schedule_item(now - timedelta(hours=1), now + timedelta(hours=1), container=False)

        episode = await provider.adaptor.new_object(item)

        assert isinstance(episode, MAPodcastEpisode)
        assert isinstance(episode.podcast, ItemMapping)
        assert episode.podcast.name == "Radio 1"

    async def test_future_programme_is_not_playable(self, provider: BBCSoundsProvider) -> None:
        """Test a programme that has not started yet is left out, as before."""
        now = datetime.now(tz=UTC)
        item = _schedule_item(now + timedelta(hours=1), now + timedelta(hours=2))
        assert await provider.adaptor.new_object(item) is None


class TestLiveProgrammeStreamDetails:
    """Tests for getting the stream of a programme on air now."""

    async def test_on_air_programme_streams_from_the_rewind_window(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test an on-air programme gets a seekable stream the provider serves itself."""
        now = int(datetime.now(tz=UTC).timestamp())
        programme_id = LiveProgrammeId("bbc_radio_one", "m0031hzl", now - 3600, now + 3600)
        provider.client.streaming = AsyncMock()
        provider.client.stations.get_station.return_value = LiveStation(
            id="bbc_radio_one", titles={"primary": "Another programme"}, stream="master.m3u8"
        )
        monkeypatch.setattr(
            provider.mass.streams.audio,
            "get_hls_substream",
            AsyncMock(return_value=Mock(path=LIVE_URL)),
        )

        details = await provider.get_stream_details(programme_id.item_id, MediaType.PODCAST_EPISODE)

        assert details.stream_type == StreamType.CUSTOM
        assert details.duration == 7200
        assert details.can_seek
        assert details.allow_seek
        assert details.data == {"live_url": LIVE_URL}
        # the station's current programme is not the one being played
        assert details.stream_metadata is None
        # the details (and so the buffer seeks are checked against) last the whole programme
        assert details.expiration >= 3600
        provider.client.streaming.get_by_pid.assert_not_called()

    async def test_ended_programme_prefers_the_on_demand_version(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a programme that has finished plays its on-demand version when there is one."""
        now = int(datetime.now(tz=UTC).timestamp())
        programme_id = LiveProgrammeId("bbc_radio_one", "m0031hzl", now - 7200, now - 60)
        on_demand = Mock(spec=StreamDetails)
        catch_up = AsyncMock(return_value=on_demand)
        monkeypatch.setattr(provider, "_catch_up_stream_details", catch_up)

        details = await provider.get_stream_details(programme_id.item_id, MediaType.PODCAST_EPISODE)

        assert details is on_demand
        catch_up.assert_awaited_once_with("m0031hzl", MediaType.PODCAST_EPISODE)

    async def test_play_status_is_not_reported_for_live_programmes(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test the BBC play status API is not called with a live programme id."""
        provider.client.streaming = AsyncMock()
        await provider.on_played(
            MediaType.PODCAST_EPISODE,
            "live_programme~bbc_radio_one~m0031hzl~1~2",
            fully_played=False,
            position=60,
            media_item=Mock(),
            is_playing=True,
        )
        provider.client.streaming.update_play_status.assert_not_called()


class TestLiveProgrammeLookup:
    """Tests for looking up live programmes through the (cached) schedule."""

    @pytest.fixture(autouse=True)
    def _cache_misses(self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        # force every @use_cache lookup to miss so the real method body always runs
        cache = provider.mass.cache
        monkeypatch.setattr(
            cache, "get_with_freshness", AsyncMock(return_value=(None, False, False))
        )
        monkeypatch.setattr(cache, "set", AsyncMock())
        use_real_create_task(provider.mass)

    async def test_live_programme_is_found_on_its_broadcast_date(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a live programme id is looked up in the schedule of the day it started."""
        now = datetime.now(tz=UTC)
        item = _schedule_item(now - timedelta(hours=2), now + timedelta(hours=1))
        provider.client.schedules.get_schedule.return_value = Schedule(
            id="schedule", sub_items=[item]
        )
        programme_id = LiveProgrammeId(
            "bbc_radio_one",
            "m0031hzl",
            int(item.start.timestamp()),
            int(item.end.timestamp()),
        )

        episode = await provider.get_podcast_episode(programme_id.item_id)

        assert episode.item_id == programme_id.item_id
        broadcast_date = item.start.astimezone(ZoneInfo("Europe/London")).date().isoformat()
        provider.client.schedules.get_schedule.assert_awaited_once_with(
            station_id="bbc_radio_one", date=broadcast_date
        )


class TestContinueWithStation:
    """Tests for carrying on with the station live once a live programme ends."""

    async def test_station_is_queued_after_the_programme(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the station's live stream is added when the programme is the last item."""
        details = _programme_details()
        _playing_queue(monkeypatch, provider, details)
        monkeypatch.setattr(provider.mass.player_queues, "get_next_item", Mock(return_value=None))
        play_media = AsyncMock()
        monkeypatch.setattr(provider.mass.player_queues, "play_media", play_media)
        station = Mock()
        monkeypatch.setattr(provider, "get_radio", AsyncMock(return_value=station))

        await provider._continue_with_station(details, PROGRAMME)

        play_media.assert_awaited_once_with("queue_id", station, option=QueueOption.ADD)

    @pytest.mark.parametrize(("streamed_by_queue", "has_next"), [(True, True), (False, False)])
    async def test_queue_is_left_alone(
        self,
        provider: BBCSoundsProvider,
        monkeypatch: pytest.MonkeyPatch,
        streamed_by_queue: bool,
        has_next: bool,
    ) -> None:
        """Test nothing is queued when something follows, or the queue plays another stream."""
        details = _programme_details()
        _playing_queue(monkeypatch, provider, details if streamed_by_queue else Mock())
        monkeypatch.setattr(
            provider.mass.player_queues,
            "get_next_item",
            Mock(return_value=Mock() if has_next else None),
        )
        play_media = AsyncMock()
        monkeypatch.setattr(provider.mass.player_queues, "play_media", play_media)

        await provider._continue_with_station(details, PROGRAMME)

        play_media.assert_not_called()

    async def test_continuing_is_scheduled_when_the_programme_ends(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the stream schedules carrying on with the station once it reaches the end."""

        async def _segments(**_kwargs: object) -> AsyncGenerator[bytes]:
            yield b"segment"

        monkeypatch.setattr("music_assistant.providers.bbc_sounds.stream_programme", _segments)
        continue_with_station = Mock()
        monkeypatch.setattr(provider, "_continue_with_station", continue_with_station)
        create_task = Mock()
        monkeypatch.setattr(provider.mass, "create_task", create_task)
        details = _programme_details()

        chunks = [chunk async for chunk in provider.get_audio_stream(details)]

        assert chunks == [b"segment"]
        continue_with_station.assert_called_once_with(details, PROGRAMME)
        create_task.assert_called_once()

    async def test_superseded_stream_does_not_continue(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a stream whose buffer was replaced by a seek does not queue the station."""
        details = _programme_details()

        async def _segments(**_kwargs: object) -> AsyncGenerator[bytes]:
            details.buffer = Mock()
            yield b"segment"

        monkeypatch.setattr("music_assistant.providers.bbc_sounds.stream_programme", _segments)
        create_task = Mock()
        monkeypatch.setattr(provider.mass, "create_task", create_task)

        _ = [chunk async for chunk in provider.get_audio_stream(details)]

        create_task.assert_not_called()


class TestSnapToLive:
    """Tests for moving the queue back to the live edge after a seek past it."""

    @pytest.fixture
    def create_task(self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch) -> Mock:
        """Capture the tasks the provider creates, with the queue seek mocked."""
        monkeypatch.setattr(provider.mass.player_queues, "seek", Mock())
        create_task = Mock()
        monkeypatch.setattr(provider.mass, "create_task", create_task)
        return create_task

    def test_seek_past_the_live_edge_moves_back(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch, create_task: Mock
    ) -> None:
        """Test a seek well past the live edge seeks the queue to where playback started."""
        details = _programme_details()
        _playing_queue(monkeypatch, provider, details)

        provider._snap_to_live(details, PROGRAMME, PROGRAMME.start + 1500, PROGRAMME.start + 500)

        provider.mass.player_queues.seek.assert_called_once_with("queue_id", 500)  # type: ignore[attr-defined]
        create_task.assert_called_once()

    @pytest.mark.parametrize(("requested", "streamed_by_queue"), [(530, True), (1500, False)])
    def test_position_is_left_alone(
        self,
        provider: BBCSoundsProvider,
        monkeypatch: pytest.MonkeyPatch,
        create_task: Mock,
        requested: int,
        streamed_by_queue: bool,
    ) -> None:
        """Test starts near the live edge, or streams the queue is not playing, are left alone."""
        details = _programme_details()
        _playing_queue(monkeypatch, provider, details if streamed_by_queue else Mock())

        provider._snap_to_live(
            details, PROGRAMME, PROGRAMME.start + requested, PROGRAMME.start + 500
        )

        create_task.assert_not_called()
