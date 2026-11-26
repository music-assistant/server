"""Test the now playing and segment functionality."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from auntie_sounds.history import SegmentHistory
from auntie_sounds.models import (
    URI,
    ScheduleItem,
    Segment,
    SegmentOffset,
    Titles,
)
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

import music_assistant.helpers.datetime as dt
from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.constants import Constants
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId
from music_assistant.providers.bbc_sounds.now_playing import NowPlaying, NowPlayingManager

PROGRAMME_START = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
PROGRAMME = ScheduleItem(id="pid", titles=Titles(primary="Programme"), start=PROGRAMME_START)
EPISODE = StreamMetadata(title="Programme")
IMAGE = "https://example.com/song.jpg"
SPOTIFY_IMAGE = "https://i.scdn.co/image/song.jpg"
MA_IMAGE = "https://example.com/ma.jpg"
SPOTIFY_URI = URI(
    type="commercial-music-service",
    id="commercial-music-service-spotify",
    label="Spotify",
    uri="https://open.spotify.com/track/15tHagkk8z306XkyOHqiip",
)


def _mock(service: object) -> AsyncMock:
    """Return a client service, which the client fixture replaces with a mock, as that mock."""
    assert isinstance(service, AsyncMock)
    return service


def _segment(
    segment_id: str = "segment",
    start: int = 0,
    end: int = 60,
    image_url: str | None = IMAGE,
    uris: list[URI] | None = None,
) -> Segment:
    """Build a track that plays from start to end seconds into a programme."""
    return Segment(
        id=segment_id,
        segment_type="music",
        uris=uris or [],
        titles=Titles(primary="Artist", secondary=f"Song {segment_id}"),
        image_url=image_url,
        offset=SegmentOffset(start=start, end=end),
    )


def _stream_details(item_id: str = "vpid") -> StreamDetails:
    return StreamDetails(
        provider="bbc_sounds",
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        stream_metadata=EPISODE,
    )


def _history(*segments: Segment, programme_start: datetime = PROGRAMME_START) -> SegmentHistory:
    """Build a station history holding the given tracks of one programme."""
    history = SegmentHistory()
    history.merge(
        sorted(segments, key=lambda s: s.offset.start if s.offset else 0, reverse=True),
        programme_start=programme_start,
        now=programme_start + timedelta(hours=1),
    )
    return history


@pytest.fixture(name="now_playing")
def _now_playing(provider: BBCSoundsProvider) -> NowPlayingManager:
    return provider.stream_manager.now_playing


@pytest.fixture(name="lookups", autouse=True)
def _lookups(provider: BBCSoundsProvider) -> list[asyncio.Task[Any]]:
    """Run background tasks for real, starting them eagerly as MA does, and return them."""
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(target, eager_start=True)
        tasks.append(task)
        return task

    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


@pytest.fixture(name="image_cache")
def _image_cache(provider: BBCSoundsProvider) -> dict[str, Any]:
    """Back the MA cache with a dict, and stub MA's artwork lookup to find nothing."""
    store: dict[str, Any] = {}

    async def _get(key: str, **_kwargs: Any) -> Any:
        return store.get(key)

    async def _set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    provider.mass.cache.get = AsyncMock(side_effect=_get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=_set)  # type: ignore[method-assign]
    provider.mass.metadata.update_radio_stream_artwork = AsyncMock()  # type: ignore[method-assign]
    return store


class TestProgrammeSegments:
    """Tests for loading the tracks of an on-demand programme."""

    async def test_cache_miss_returns_and_stores_the_segments(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test the first lookup returns the API's segments as well as caching them."""
        segments = [_segment()]
        _mock(now_playing.client.content).get_show_segments.return_value = segments

        assert await now_playing.programme_segments("vpid") == segments
        assert await now_playing.programme_segments("vpid") == segments
        _mock(now_playing.client.content).get_show_segments.assert_awaited_once()

    async def test_empty_track_list_is_not_cached(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test a programme whose track list isn't published yet is fetched again next time."""
        _mock(now_playing.client.content).get_show_segments.return_value = []

        assert await now_playing.programme_segments("vpid") == []
        assert image_cache == {}


class TestOnDemand:
    """Tests for the track shown while playing an on-demand programme."""

    async def test_track_then_episode_between_tracks(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test the track at the position is shown, and the episode when no track covers it."""
        stream_details = _stream_details()
        state = NowPlaying(fallback=EPISODE)
        segments = [_segment()]

        await now_playing.update_on_demand(state, segments, stream_details, 30)
        assert stream_details.stream_metadata == StreamMetadata(
            title="Song segment", artist="Artist", image_url=IMAGE
        )

        await now_playing.update_on_demand(state, segments, stream_details, 90)
        assert stream_details.stream_metadata == EPISODE


class TestArtwork:
    """Tests for filling in the artwork of a track the BBC has none for."""

    async def test_own_image_is_used_without_lookups(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test a track's own image is shown without asking Spotify or MA."""
        stream_details = _stream_details()

        await now_playing.update_on_demand(
            NowPlaying(fallback=EPISODE), [_segment()], stream_details, 30
        )

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.image_url == IMAGE
        _mock(now_playing.client.content).ensure_segment_image.assert_not_awaited()
        assert image_cache == {}

    async def test_spotify_image_is_used_and_cached(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test Spotify's image is shown and cached by segment, without changing the segment."""
        segment = _segment(image_url=None, uris=[SPOTIFY_URI])

        async def _ensure_segment_image(target: Segment) -> Segment:
            target.image_url = SPOTIFY_IMAGE
            return target

        content = _mock(now_playing.client.content)
        content.ensure_segment_image.side_effect = _ensure_segment_image
        stream_details = _stream_details()

        await now_playing.update_on_demand(
            NowPlaying(fallback=EPISODE), [segment], stream_details, 30
        )

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.image_url == SPOTIFY_IMAGE
        assert image_cache == {"segment_image_segment": SPOTIFY_IMAGE}
        assert segment.image_url is None

    async def test_ma_image_is_used_when_spotify_has_none(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test a track Spotify has no image for falls back to MA's artwork lookup."""

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            details.stream_metadata = StreamMetadata(title="Song", image_url=MA_IMAGE)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )
        stream_details = _stream_details()

        await now_playing.update_on_demand(
            NowPlaying(fallback=EPISODE), [_segment(image_url=None)], stream_details, 30
        )

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.image_url == MA_IMAGE
        assert image_cache == {"segment_image_segment": MA_IMAGE}

    async def test_a_miss_is_cached(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test a track with no artwork anywhere isn't looked up again in the next playback."""
        segments = [_segment(image_url=None)]

        for _ in range(2):
            await now_playing.update_on_demand(
                NowPlaying(fallback=EPISODE), segments, _stream_details(), 30
            )

        provider.mass.metadata.update_radio_stream_artwork.assert_awaited_once()  # type: ignore[attr-defined]
        assert image_cache == {"segment_image_segment": ""}

    async def test_slow_lookup_does_not_replace_the_next_track(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test artwork that arrives after playback moved on leaves the newer track showing."""
        state = NowPlaying(fallback=EPISODE)
        segments = [_segment("first", image_url=None), _segment("next", start=60, end=120)]
        stream_details = _stream_details()

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            # Playback moves on to the next track while MA is still looking
            await now_playing.update_on_demand(state, segments, stream_details, 90)
            details.stream_metadata = StreamMetadata(title="Song", image_url=MA_IMAGE)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )

        await now_playing.update_on_demand(state, segments, stream_details, 30)

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.title == "Song next"
        assert image_cache == {"segment_image_first": MA_IMAGE}

    async def test_slow_lookup_does_not_replace_a_return_to_the_same_track(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test an A, B, A sequence shows the second A lookup's artwork, not the first's."""
        state = NowPlaying(fallback=EPISODE)
        segments = [_segment("first", image_url=None), _segment("next", start=60, end=120)]
        stream_details = _stream_details()
        lookups = 0

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            nonlocal lookups
            lookups += 1
            if lookups == 1:
                # The listener skips to the next track and back while MA is still looking
                await now_playing.update_on_demand(state, segments, stream_details, 90)
                await now_playing.update_on_demand(state, segments, stream_details, 30)
                details.stream_metadata = StreamMetadata(title="Song", image_url="stale.jpg")
            else:
                details.stream_metadata = StreamMetadata(title="Song", image_url=MA_IMAGE)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )

        await now_playing.update_on_demand(state, segments, stream_details, 30)

        assert stream_details.stream_metadata == StreamMetadata(
            title="Song first", artist="Artist", image_url=MA_IMAGE
        )

    async def test_slow_lookup_does_not_replace_the_fallback(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test artwork that arrives after the track ended leaves the fallback showing."""
        state = NowPlaying(fallback=EPISODE)
        segments = [_segment(image_url=None)]
        stream_details = _stream_details()

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            # Playback moves past the end of the track while MA is still looking
            await now_playing.update_on_demand(state, segments, stream_details, 90)
            details.stream_metadata = StreamMetadata(title="Song", image_url=MA_IMAGE)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )

        await now_playing.update_on_demand(state, segments, stream_details, 30)

        assert stream_details.stream_metadata == EPISODE

    async def test_timed_out_lookup_shows_the_track_and_moves_on(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
        lookups: list[asyncio.Task[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test a lookup that times out shows the track without artwork and isn't cached."""
        monkeypatch.setattr(Constants, "ARTWORK_TIMEOUT", 0.01)

        async def _update_radio_stream_artwork(_details: StreamDetails) -> None:
            await asyncio.sleep(1)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )
        state = NowPlaying(fallback=EPISODE)
        segments = [_segment("first", image_url=None), _segment("next", start=60, end=120)]
        stream_details = _stream_details()

        await now_playing.update_on_demand(state, segments, stream_details, 30)

        assert stream_details.stream_metadata == StreamMetadata(title="Song first", artist="Artist")
        await asyncio.gather(*lookups)
        assert image_cache == {}

        await now_playing.update_on_demand(state, segments, stream_details, 90)

        assert stream_details.stream_metadata == StreamMetadata(
            title="Song next", artist="Artist", image_url=IMAGE
        )


class TestLiveProgramme:
    """Tests for the track shown while listening to a live programme from the start."""

    PROGRAMME = LiveProgrammeId(
        "bbc_radio_two",
        "pid",
        int(PROGRAMME_START.timestamp()),
        int(PROGRAMME_START.timestamp()) + 3600,
    )

    async def _update(
        self, now_playing: NowPlayingManager, state: NowPlaying, elapsed: int
    ) -> StreamDetails:
        stream_details = _stream_details(self.PROGRAMME.item_id)
        await now_playing.update_live_programme(
            state, self.PROGRAMME, PROGRAMME, stream_details, elapsed
        )
        return stream_details

    async def test_track_is_shown_with_the_station(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test the track on air at the listener's point is shown, with the station as album."""
        history = _history(_segment("first"), _segment("second", start=120, end=180))
        _mock(now_playing.client.schedules).refresh_history.return_value = history
        state = NowPlaying(fallback=None, album="Radio 2")

        stream_details = await self._update(now_playing, state, 150)

        assert stream_details.stream_metadata == StreamMetadata(
            title="Song second", artist="Artist", album="Radio 2", image_url=IMAGE
        )

    @pytest.mark.parametrize(("behind", "marker"), [(30, "«LIVE»"), (600, "«Rewound»")])
    async def test_between_tracks_the_programme_is_marked_for_the_live_edge(
        self,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        behind: int,
        marker: str,
    ) -> None:
        """Test the programme is marked live near the live edge and rewound further back."""
        history = _history(_segment("first"), _segment("second", start=120, end=180))
        _mock(now_playing.client.schedules).refresh_history.return_value = history
        monkeypatch.setattr(dt, "utc_timestamp", lambda: PROGRAMME_START.timestamp() + 90 + behind)

        stream_details = await self._update(
            now_playing, NowPlaying(fallback=None, album="Radio 2"), 90
        )

        assert stream_details.stream_metadata == StreamMetadata(
            title=f"{marker} Programme", album="Radio 2"
        )

    async def test_artwork_is_looked_up_once_per_track(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
    ) -> None:
        """Test ticks within one track don't repeat the artwork lookup."""
        history = _history(_segment(image_url=None, end=600))
        _mock(now_playing.client.schedules).refresh_history.return_value = history
        state = NowPlaying(fallback=None)
        # Never answer from the cache, so only the per-track check can prevent a lookup
        provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

        for elapsed in (10, 20, 30, 40):
            await self._update(now_playing, state, elapsed)

        provider.mass.metadata.update_radio_stream_artwork.assert_awaited_once()  # type: ignore[attr-defined]


class TestStation:
    """Tests for the track shown while playing a station live."""

    async def test_track_is_the_one_the_listener_hears(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test the track shown is the one on air as far back as the stream plays."""
        start = datetime.now(UTC) - timedelta(seconds=600)
        history = _history(
            _segment("heard", start=500, end=590),
            _segment("on_air", start=590, end=700),
            programme_start=start,
        )
        _mock(now_playing.client.schedules).refresh_history.return_value = history
        stream_details = _stream_details("bbc_radio_two")
        stream_details.data = {"hls_start_seconds": 23}

        await now_playing.update_station(NowPlaying(fallback=None), stream_details, 0)

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.title == "Song heard"

    async def test_track_is_shown_before_its_artwork_is_found(
        self,
        provider: BBCSoundsProvider,
        now_playing: NowPlayingManager,
        image_cache: dict[str, Any],
        lookups: list[asyncio.Task[Any]],
    ) -> None:
        """Test a station shows its track at once, and the artwork when the lookup ends."""
        found = asyncio.Event()

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            await found.wait()
            details.stream_metadata = StreamMetadata(title="Song", image_url=MA_IMAGE)

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )
        start = datetime.now(UTC) - timedelta(seconds=600)
        _mock(now_playing.client.schedules).refresh_history.return_value = _history(
            _segment("on_air", start=500, end=700, image_url=None), programme_start=start
        )
        stream_details = _stream_details("bbc_radio_two")

        await now_playing.update_station(NowPlaying(fallback=None), stream_details, 0)

        assert stream_details.stream_metadata == StreamMetadata(
            title="Song on_air", artist="Artist"
        )

        found.set()
        await asyncio.gather(*lookups)

        assert stream_details.stream_metadata.image_url == MA_IMAGE

    async def test_zero_delay_is_kept(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test a stream that started at the live edge shows the track on air now."""
        start = datetime.now(UTC) - timedelta(seconds=600)
        history = _history(
            _segment("heard", start=500, end=590),
            _segment("on_air", start=590, end=700),
            programme_start=start,
        )
        _mock(now_playing.client.schedules).refresh_history.return_value = history
        stream_details = _stream_details("bbc_radio_two")
        stream_details.data = {"hls_start_seconds": 0}

        await now_playing.update_station(NowPlaying(fallback=None), stream_details, 0)

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.title == "Song on_air"

    async def test_unavailable_schedule_keeps_the_fallback(
        self, now_playing: NowPlayingManager, image_cache: dict[str, Any]
    ) -> None:
        """Test the fallback stays when no track is on air and the schedule has nothing."""
        schedules = _mock(now_playing.client.schedules)
        schedules.refresh_history.return_value = SegmentHistory()
        schedules.on_air.return_value = None
        fallback = StreamMetadata(title="Programme", album="Radio 2")
        state = NowPlaying(fallback=fallback, album="Radio 2")
        stream_details = _stream_details("bbc_radio_two")

        await now_playing.update_station(state, stream_details, 0)

        assert state.fallback == fallback
        assert stream_details.stream_metadata == fallback
