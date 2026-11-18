"""Tests for NetEase stream detail bitrate normalization."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider


@pytest.mark.parametrize(
    ("api_bitrate", "expected_kbps"),
    [
        (128000, 128),
        (3351266, 3351),
        (320, 320),
    ],
)
async def test_stream_bitrate_is_normalized_to_kbps(
    provider: NeteaseCloudMusicProvider,
    api_bitrate: int,
    expected_kbps: int,
) -> None:
    """Convert bps values returned by the API while preserving values already in kbps."""
    provider._client = AsyncMock()
    provider._client.get = AsyncMock(
        return_value={
            "data": [
                {
                    "url": "https://example.com/track.flac",
                    "type": "flac",
                    "level": "lossless",
                    "br": api_bitrate,
                    "sr": 44100,
                    "time": 200000,
                }
            ]
        }
    )
    provider._get_song_detail = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"dt": 200000}]
    )

    stream_details = await provider.get_stream_details("123", MediaType.TRACK)

    assert stream_details.audio_format.bit_rate == expected_kbps
