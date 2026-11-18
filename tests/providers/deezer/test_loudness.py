"""Test that Deezer tracks pass on the loudness Deezer reports for them."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.deezer.streaming import DeezerStreamingManager


def _streaming(song_data: dict[str, Any]) -> tuple[DeezerStreamingManager, Mock]:
    """Return a streaming manager for one MP3 track and its mocked provider."""
    provider = Mock()
    provider.gw_client.get_deezer_track_urls = AsyncMock(
        return_value=(
            {"format": "MP3_320", "sources": [{"url": "http://x"}]},
            {"SNG_ID": "1", "DURATION": "60", "FILESIZE_MP3_320": "1000", **song_data},
        )
    )
    return DeezerStreamingManager(provider), provider


async def test_gain_sets_the_track_loudness() -> None:
    """Deezer's GAIN is used as loudness for playback and stored for the track."""
    streaming, provider = _streaming({"GAIN": "-12.4"})

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert details.loudness == -12.4
    provider.mass.streams.audio_analysis.set_track_loudness.assert_called_once_with(
        item_id="1", provider_instance_id_or_domain=provider.instance_id, loudness=-12.4
    )


@pytest.mark.parametrize("song_data", [{}, {"GAIN": None}, {"GAIN": ""}])
async def test_track_without_gain_has_no_loudness(song_data: dict[str, Any]) -> None:
    """A track without GAIN, like a personal upload, leaves the loudness to Music Assistant."""
    streaming, provider = _streaming(song_data)

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert details.loudness is None
    provider.mass.streams.audio_analysis.set_track_loudness.assert_not_called()
