"""Tests for the track details taken from the ICY 'StreamTitle' field."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio


@pytest.mark.parametrize(
    ("stream_title", "expected_title", "expected_artist"),
    [
        ("John Lennon - I&apos;m Losing You", "I'm Losing You", "John Lennon"),
        ("&quot;Cornflake Girl&quot; by Tori Amos", "Cornflake Girl", "Tori Amos"),
    ],
)
def test_html_entities_are_decoded(
    stream_title: str, expected_title: str, expected_artist: str
) -> None:
    """Entity-encoded stream titles reach the player metadata decoded."""
    mass = MagicMock()
    mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    sd = StreamDetails(
        item_id="radio1",
        provider="test",
        audio_format=AudioFormat(content_type=ContentType.MP3, channels=2),
        media_type=MediaType.RADIO,
        stream_type=StreamType.ICY,
        path="http://example.invalid/stream",
        duration=0,
    )

    StreamsAudio(mass)._parse_icy_metadata(f"StreamTitle='{stream_title}';".encode(), sd)

    assert sd.stream_title == f"{expected_artist} - {expected_title}"
    assert sd.stream_metadata is not None
    assert sd.stream_metadata.title == expected_title
    assert sd.stream_metadata.artist == expected_artist
