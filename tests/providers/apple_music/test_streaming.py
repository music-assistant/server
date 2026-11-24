"""Unit tests for stream resolution of Apple Music."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import StreamType

from music_assistant.providers.apple_music.streaming import AppleMusicStreamingManager

CTRP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.rphq.aac.wa.m3u8"
KEY_URI = "data:;base64,AAAAABep6CkAHStsI7gv/Q=="


async def test_encrypted_stream_skips_the_fragment_scan() -> None:
    """The encrypted stream tells ffmpeg not to fetch every fragment before the first output."""
    manager: Any = AppleMusicStreamingManager(MagicMock())
    manager._decrypt_client_id = b"id"
    manager._decrypt_private_key = b"key"
    manager._fetch_song_stream_metadata = AsyncMock(
        return_value={"hls-key-server-url": "https://key.example", "assets": []}
    )
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))
    manager._get_decryption_key = AsyncMock(return_value="aabb")

    result = await manager.get_stream_details("397010985")

    assert result.stream_type is StreamType.ENCRYPTED_HTTP
    assert result.path == CTRP_URL
    # without it ffmpeg fetches every fragment before the first output (support#7205)
    assert result.extra_input_args == ["-fflags", "+ignidx"]
