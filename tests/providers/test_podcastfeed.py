"""Tests for the podcast feed provider."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.podcastfeed import PodcastMusicprovider


def _podcast_stream(content_type: ContentType = ContentType.MP3) -> StreamDetails:
    return StreamDetails(
        provider="podcastfeed",
        item_id="episode",
        audio_format=AudioFormat(content_type=content_type),
        media_type=MediaType.PODCAST_EPISODE,
        stream_type=StreamType.CUSTOM,
        path="https://example.com/podcast.mp3",
        duration=120,
        size=1200,
        can_seek=True,
        allow_seek=True,
    )


def test_podcast_seek_headers_use_original_duration_for_byte_range() -> None:
    """Test that podcast seek uses the original duration to calculate the byte range."""
    streamdetails = _podcast_stream()
    headers: dict[str, str] = {}
    logger = Mock()
    provider = cast("PodcastMusicprovider", SimpleNamespace(logger=logger))

    seek_position = PodcastMusicprovider._prepare_seek_headers(
        provider,
        streamdetails,
        headers,
        original_duration=120,
        seek_position=30,
        seek_supported=True,
    )

    assert seek_position == 30
    assert headers["Range"] == "bytes=300-1199"


def test_podcast_seek_headers_disable_unsupported_seek() -> None:
    """Test that unsupported podcast seeks fall back to a normal stream."""
    streamdetails = _podcast_stream(ContentType.UNKNOWN)
    headers: dict[str, str] = {}
    logger = Mock()
    provider = cast("PodcastMusicprovider", SimpleNamespace(logger=logger))

    seek_position = PodcastMusicprovider._prepare_seek_headers(
        provider,
        streamdetails,
        headers,
        original_duration=120,
        seek_position=30,
        seek_supported=True,
    )

    assert seek_position == 0
    assert "Range" not in headers
    assert streamdetails.seek_position == 0
    logger.warning.assert_called_once()


def test_podcast_media_info_size_parsing() -> None:
    """Test stream size extraction from ffprobe media info."""
    assert (
        PodcastMusicprovider._get_media_info_size(
            cast("AudioTags", SimpleNamespace(raw={"format": {"size": "1234"}}))
        )
        == 1234
    )
    assert (
        PodcastMusicprovider._get_media_info_size(
            cast("AudioTags", SimpleNamespace(raw={"format": {}}))
        )
        is None
    )
    assert (
        PodcastMusicprovider._get_media_info_size(
            cast("AudioTags", SimpleNamespace(raw={"format": {"size": "invalid"}}))
        )
        is None
    )
