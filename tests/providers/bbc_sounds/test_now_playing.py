"""Test the now playing and segment functionality."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

from auntie_sounds.history import SegmentHistory
from auntie_sounds.models import (
    URI,
    Segment,
    SegmentOffset,
    Titles,
)
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId

PROGRAMME_START = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _mock(service: object) -> AsyncMock:
    """Return a client service, which the client fixture replaces with a mock, as that mock."""
    assert isinstance(service, AsyncMock)
    return service


def _segment(segment_id: str = "segment", start: int = 0, end: int = 60) -> Segment:
    """Build a track with an image that plays from start to end seconds into a programme."""
    return Segment(
        id=segment_id,
        segment_type="music",
        uris=[],
        titles=Titles(primary="Artist", secondary="Song"),
        image_url="https://example.com/song.jpg",
        offset=SegmentOffset(start=start, end=end),
    )


def _segment_spotify_only() -> Segment:
    """Build a track that has a spotify URL, not no image_url."""
    return Segment(
        id="segment",
        segment_type="music",
        titles=Titles(primary="Artist", secondary="Song"),
        image_url=None,
        offset=SegmentOffset(start=0, end=60),
        uris=[
            URI(
                type="commercial-music-service",
                id="commercial-music-service-spotify",
                label="Spotify",
                uri="https://open.spotify.com/track/15tHagkk8z306XkyOHqiip",
            )
        ],
    )


def _segment_no_images() -> Segment:
    """Build a segment with no image information at all."""
    return Segment(
        id="segment",
        segment_type="music",
        uris=[],
        titles=Titles(primary="Artist", secondary="Song"),
        image_url=None,
        offset=SegmentOffset(start=0, end=60),
    )


def _stream_details(segment: Segment) -> StreamDetails:
    """Build an on-demand stream whose segments are already loaded, so only image lookups run."""
    return StreamDetails(
        provider="bbc_sounds",
        item_id="vpid",
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        data={"vpid": "vpid", "pid": "pid", "segments": [segment]},
        stream_metadata=StreamMetadata(title="Programme"),
    )


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

        assert (
            await provider.stream_manager.now_playing._load_programme_segments("vpid") == segments
        )
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

        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=30
        )

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

        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=30
        )
        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=35
        )

        _mock(provider.client.content).get_show_segments.assert_awaited_once()
        cache_set.assert_not_awaited()

    async def test_segment_image_is_used_without_lookups(self, provider: BBCSoundsProvider) -> None:
        """Test a segment's own image is shown without asking Spotify or MA for one."""
        stream_details = _stream_details(_segment())
        artwork = AsyncMock()
        cache_set = AsyncMock()
        provider.mass.metadata.update_radio_stream_artwork = artwork  # type: ignore[method-assign]
        provider.mass.cache.set = cache_set  # type: ignore[method-assign]

        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=30
        )

        assert stream_details.stream_metadata is not None

        assert stream_details.stream_metadata.image_url == "https://example.com/song.jpg"
        _mock(provider.client.content).ensure_segment_image.assert_not_awaited()
        artwork.assert_not_awaited()
        cache_set.assert_not_awaited()

    async def test_spotify_image_is_used_and_cached(self, provider: BBCSoundsProvider) -> None:
        """Test a segment without an image gets Spotify's, which is cached without asking MA."""
        spotify_image = "https://i.scdn.co/image/song.jpg"
        segment = _segment_spotify_only()

        async def _ensure_segment_image(target: Segment) -> Segment:
            target.image_url = spotify_image
            return target

        content = _mock(provider.client.content)
        content.ensure_segment_image.side_effect = _ensure_segment_image
        stream_details = _stream_details(segment)
        artwork = AsyncMock()
        cache_set = AsyncMock()
        provider.mass.metadata.update_radio_stream_artwork = artwork  # type: ignore[method-assign]
        provider.mass.cache.set = cache_set  # type: ignore[method-assign]

        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=30
        )

        content.ensure_segment_image.assert_awaited_once_with(segment)
        artwork.assert_not_awaited()
        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.image_url == spotify_image
        assert cache_set.await_args is not None
        cached = cache_set.await_args.kwargs["data"]
        assert [Segment.from_dict(item).image_url for item in cached] == [spotify_image]

    async def test_ma_image_is_used_and_cached(self, provider: BBCSoundsProvider) -> None:
        """Test a segment Spotify has no image for falls back to MA's artwork, which is cached."""
        ma_image = "https://example.com/ma.jpg"
        segment = _segment_no_images()

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            details.stream_metadata = StreamMetadata(
                title="Song", artist="Artist", image_url=ma_image
            )

        stream_details = _stream_details(segment)
        artwork = AsyncMock(side_effect=_update_radio_stream_artwork)
        cache_set = AsyncMock()
        provider.mass.metadata.update_radio_stream_artwork = artwork  # type: ignore[method-assign]
        provider.mass.cache.set = cache_set  # type: ignore[method-assign]

        await provider.stream_manager.now_playing.update_on_demand_stream_metadata(
            stream_details, elapsed_time=30
        )

        _mock(provider.client.content).ensure_segment_image.assert_awaited_once_with(segment)
        artwork.assert_awaited_once()
        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.image_url == ma_image
        assert cache_set.await_args is not None
        cached = cache_set.await_args.kwargs["data"]
        assert [Segment.from_dict(item).image_url for item in cached] == [ma_image]

    async def test_slow_ma_lookup_does_not_replace_the_next_segment(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test an MA lookup that outlives its segment leaves the newer segment showing."""
        now_playing = provider.stream_manager.now_playing
        ma_image = "https://example.com/ma.jpg"
        first = _segment_no_images()
        second = _segment("next", start=60, end=120)
        second.titles = Titles(primary="Artist", secondary="Next Song")
        stream_details = _stream_details(first)
        stream_details.data["segments"] = [first, second]

        async def _update_radio_stream_artwork(details: StreamDetails) -> None:
            # Playback moves on to the next segment while MA is still looking
            await now_playing.update_on_demand_stream_metadata(stream_details, elapsed_time=90)
            details.stream_metadata = StreamMetadata(
                title="Song", artist="Artist", image_url=ma_image
            )

        provider.mass.metadata.update_radio_stream_artwork = AsyncMock(  # type: ignore[method-assign]
            side_effect=_update_radio_stream_artwork
        )
        provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]

        await now_playing.update_on_demand_stream_metadata(stream_details, elapsed_time=30)

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.title == "Next Song"
        assert stream_details.data["segment_id"] == "next"
        # The image is still kept on the segment for next time
        assert first.image_url == ma_image


class TestLiveProgrammeMetadata:
    """Tests for the track shown while listening to a live programme from the start."""

    @staticmethod
    def _history(provider: BBCSoundsProvider) -> None:
        """Make the station's history two tracks a minute long with a gap between them."""
        history = SegmentHistory()
        history.merge(
            [_segment("second", start=120, end=180), _segment("first")],
            programme_start=PROGRAMME_START,
            now=PROGRAMME_START + timedelta(minutes=10),
        )
        _mock(provider.client.schedules).refresh_history.return_value = history

    @staticmethod
    def _stream_details() -> StreamDetails:
        programme = LiveProgrammeId(
            "bbc_radio_two",
            "pid",
            int(PROGRAMME_START.timestamp()),
            int(PROGRAMME_START.timestamp()) + 3600,
        )
        return StreamDetails(
            provider="bbc_sounds",
            item_id=programme.item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            stream_metadata=StreamMetadata(title="Programme"),
        )

    async def test_track_in_history_is_shown(self, provider: BBCSoundsProvider) -> None:
        """Test the track on air at the listener's point in the programme is shown."""
        self._history(provider)
        stream_details = self._stream_details()

        await provider.stream_manager.now_playing.update_live_programme_metadata(
            stream_details, elapsed_time=150
        )

        assert stream_details.stream_metadata is not None
        assert stream_details.stream_metadata.title == "Song"
        assert stream_details.stream_metadata.artist == "Artist"

    async def test_between_tracks_falls_back_to_the_programme(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test no track on air clears the metadata, so the queue item describes the programme."""
        self._history(provider)
        stream_details = self._stream_details()

        await provider.stream_manager.now_playing.update_live_programme_metadata(
            stream_details, elapsed_time=90
        )

        assert stream_details.stream_metadata is None
