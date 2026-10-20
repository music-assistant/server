"""Test that seeking a Deezer track starts the stream where it was asked to."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

CHUNK_SIZE = 2048
DURATION = 600


class _Track:
    """A fake encrypted track, every chunk carries its own index as payload."""

    status = 200

    def __init__(self, chunk_count: int) -> None:
        self._data = b"".join(
            f"{index:08d}".encode() * (CHUNK_SIZE // 8) for index in range(chunk_count)
        )

    async def _iter_chunked(self, size: int) -> AsyncGenerator[bytes]:
        for start in range(0, len(self._data), size):
            yield self._data[start : start + size]

    @property
    def content(self) -> Mock:
        return Mock(iter_chunked=self._iter_chunked)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


async def _played_from(
    monkeypatch: pytest.MonkeyPatch, bitrate_kbit: int, seek_position: int
) -> float:
    """Return the position in seconds the stream actually starts playing at."""
    bytes_per_second = bitrate_kbit * 1000 // 8
    size = bytes_per_second * DURATION

    provider = Mock()
    provider.mass.http_session.get = lambda *_args, **_kwargs: _Track(size // CHUNK_SIZE)
    streaming = DeezerStreamingManager(provider)
    monkeypatch.setattr(streaming, "_get_blowfish_key", lambda _track_id: "0" * 16)
    monkeypatch.setattr(streaming, "_decrypt_chunk", lambda chunk, _key: chunk)

    streamdetails = StreamDetails(
        provider="deezer--test",
        item_id="1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        duration=DURATION,
        size=size,
        data={"track_id": "1", "url": "http://x"},
    )
    chunks = [c async for c in streaming._stream_encrypted_track(streamdetails, seek_position)]
    # the first chunk is always sent, playback continues from the one after it
    return int(chunks[1][:8]) * CHUNK_SIZE / bytes_per_second


@pytest.mark.parametrize("bitrate_kbit", [128, 320, 800])
@pytest.mark.parametrize("seek_position", [30, 60, 300])
async def test_seek_starts_within_one_chunk(
    monkeypatch: pytest.MonkeyPatch, bitrate_kbit: int, seek_position: int
) -> None:
    """A seek may only be off by the chunk it cannot split."""
    chunk_seconds = CHUNK_SIZE / (bitrate_kbit * 1000 / 8)
    played_from = await _played_from(monkeypatch, bitrate_kbit, seek_position)

    assert abs(played_from - seek_position) <= chunk_seconds


async def test_drift_does_not_grow_with_the_seek_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncating the chunks per second first scaled the drift with the distance."""
    near = 60 - await _played_from(monkeypatch, 128, 60)
    far = 300 - await _played_from(monkeypatch, 128, 300)

    assert far == pytest.approx(near, abs=CHUNK_SIZE / (128 * 1000 / 8))
