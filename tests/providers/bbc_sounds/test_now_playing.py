"""Test the now playing and segment functionality."""

from unittest.mock import AsyncMock, Mock

from auntie_sounds.models import (
    URI,
    Segment,
    SegmentOffset,
    Titles,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.bbc_sounds import BBCSoundsProvider


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


def _stream_details(segment: Segment) -> Mock:
    """Build an on-demand stream whose segments are already loaded, so only image lookups run."""
    stream_details = Mock(spec=StreamDetails)
    stream_details.data = {"vpid": "vpid", "pid": "pid", "segments": [segment]}
    stream_details.stream_metadata = StreamMetadata(title="Programme")
    return stream_details


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
        artwork.assert_awaited_once_with(stream_details)
        assert stream_details.stream_metadata.image_url == ma_image
        assert cache_set.await_args is not None
        cached = cache_set.await_args.kwargs["data"]
        assert [Segment.from_dict(item).image_url for item in cached] == [ma_image]
