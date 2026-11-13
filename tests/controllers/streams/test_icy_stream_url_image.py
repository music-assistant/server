"""Tests for station cover art taken from the ICY 'StreamUrl' field."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio

_STATION_IMAGE = "https://example.invalid/station.png"


def _parse(stream_url: str) -> tuple[StreamDetails, MagicMock]:
    """Parse ICY metadata carrying the given StreamUrl and return the result and mass mock."""
    mass = MagicMock()
    mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    mass.metadata.get_radio_stream_station_image.return_value = _STATION_IMAGE
    sd = StreamDetails(
        item_id="radio1",
        provider="test",
        audio_format=AudioFormat(content_type=ContentType.MP3, channels=2),
        media_type=MediaType.RADIO,
        stream_type=StreamType.ICY,
        path="http://example.invalid/stream",
        duration=0,
    )
    meta = f"StreamTitle='Tinashe - Too Easy';StreamUrl='{stream_url}';StreamArtwork='';"
    StreamsAudio(mass)._parse_icy_metadata(meta.encode(), sd)
    return sd, mass


def test_stream_url_image_is_used_as_cover() -> None:
    """A StreamUrl pointing at an image becomes the track cover and skips the lookup."""
    sd, mass = _parse("https://example.invalid/cover.jpg")

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.image_url == "https://example.invalid/cover.jpg"
    mass.call_later.assert_not_called()


def test_placeholder_stream_url_image_is_ignored() -> None:
    """A known placeholder image falls back to the station image and the artwork lookup."""
    sd, mass = _parse("https://broadcaster.live365.com/static/assets/img/blankart.jpg")

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.image_url == _STATION_IMAGE
    mass.call_later.assert_called_once()
