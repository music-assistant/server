"""Test the chained chapter stream of a Deezer audiobook."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest


def _flac_header(max_block_size: int) -> bytes:
    """Return the start of a FLAC file whose STREAMINFO announces the given max block size."""
    block_size = max_block_size.to_bytes(2, "big")
    return b"fLaC" + b"\x80\x00\x00\x22" + block_size + block_size + bytes(30)


async def test_audiobook_allows_bigger_flac_blocks_in_later_chapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header ffmpeg reads first allows any block size, so later chapters can use bigger."""
    provider = Mock()
    provider.gw_client.get_deezer_track_urls = AsyncMock(
        return_value=(
            {"sources": [{"url": "http://x"}], "format": "FLAC"},
            {"DURATION": "60", "SNG_ID": "1", "FILESIZE_FLAC": "1000"},
        )
    )
    streaming = DeezerStreamingManager(provider)
    header = _flac_header(4096)

    async def _chapter(_details: StreamDetails, _seek: int) -> AsyncGenerator[bytes]:
        yield header
        yield b"frames"

    monkeypatch.setattr(streaming, "_stream_encrypted_track", _chapter)
    details = StreamDetails(
        provider="deezer--test",
        item_id="1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.AUDIOBOOK,
        data={"chapter_ids": ["1", "2"], "chapter_durations_ms": [36000, 121000]},
    )

    chunks = [chunk async for chunk in streaming.get_audio_stream(details)]

    first_header = header[:10] + b"\xff\xff" + header[12:]
    assert chunks == [first_header, b"frames", header, b"frames"]
