"""Tests for ICY metadata parsing when a provider owns stream_metadata via callback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)

_META = b"StreamTitle='Some Artist - Some Song';"


def _stream_details(*, callback: bool, opt_in: bool = False) -> StreamDetails:
    """Return radio StreamDetails with optional callback and metadata handoff."""
    data = {STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True} if opt_in else None
    return StreamDetails(
        item_id="radio1",
        provider="test",
        audio_format=AudioFormat(content_type=ContentType.MP3, channels=2),
        media_type=MediaType.RADIO,
        stream_type=StreamType.ICY,
        path="http://example.invalid/stream",
        duration=0,
        data=data,
        stream_metadata_update_callback=AsyncMock() if callback else None,
    )


def test_opted_in_provider_callback_owns_stream_metadata() -> None:
    """An opted-in callback provider receives the title through StreamDetails.data."""
    audio = StreamsAudio(MagicMock())
    sd = _stream_details(callback=True, opt_in=True)

    audio._parse_icy_metadata(_META, sd)

    assert sd.data is not None
    assert sd.data[STREAMDETAILS_INBAND_TITLE_KEY] == "Some Artist - Some Song"
    assert sd.stream_metadata is None
    assert sd.stream_title is None


def test_callback_without_opt_in_keeps_legacy_icy_metadata() -> None:
    """A callback alone does not suppress the core ICY metadata path."""
    mass = MagicMock()
    mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    mass.metadata.get_radio_stream_station_image.return_value = None
    audio = StreamsAudio(mass)
    sd = _stream_details(callback=True)

    audio._parse_icy_metadata(_META, sd)

    assert sd.data is None
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Some Song"
    assert sd.stream_metadata.artist == "Some Artist"
    assert sd.stream_title == "Some Artist - Some Song"


def test_without_callback_icy_builds_stream_metadata() -> None:
    """Without a provider callback, ICY parsing keeps building stream_metadata itself."""
    mass = MagicMock()
    mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    mass.metadata.get_radio_stream_station_image.return_value = None
    audio = StreamsAudio(mass)
    sd = _stream_details(callback=False)

    audio._parse_icy_metadata(_META, sd)

    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == "Some Song"
    assert sd.stream_metadata.artist == "Some Artist"
    assert sd.stream_title == "Some Artist - Some Song"
