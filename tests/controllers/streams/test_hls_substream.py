"""Tests for HLS master playlist selection on the streams controller."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from music_assistant.controllers.streams.audio import StreamsAudio

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\n'
    "https://radio.example.com/low.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=320000,CODECS="mp4a.40.2"\n'
    "https://radio.example.com/high.m3u8\n"
)


class _FakeResponse:
    """Stand-in for the aiohttp response of a master playlist fetch."""

    def __init__(self, raw_data: bytes, charset: str | None, real_url: str) -> None:
        self._raw_data = raw_data
        self.charset = charset
        self.real_url = URL(real_url)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    def raise_for_status(self) -> None:
        """Accept the response as a successful one."""

    async def read(self) -> bytes:
        return self._raw_data


def _streams_audio(raw_data: bytes, charset: str | None, real_url: str) -> StreamsAudio:
    """Build a streams audio controller whose HTTP session serves the given playlist."""
    mass = MagicMock()
    mass.http_session_no_ssl.get = MagicMock(
        return_value=_FakeResponse(raw_data, charset, real_url)
    )
    return StreamsAudio(mass)


class TestGetHlsSubstream:
    """get_hls_substream picks the best child playlist of an HLS master playlist."""

    @pytest.mark.asyncio
    async def test_unknown_charset_falls_back_to_detection(self) -> None:
        """
        A charset the remote server made up must not break substream selection.

        Stations do send names Python has no codec for, which decode() answers with a
        LookupError that no caller on this path catches.
        """
        controller = _streams_audio(
            MASTER_PLAYLIST.encode(),
            charset="utf8mb4",
            real_url="https://radio.example.com/master.m3u8",
        )
        substream = await controller.get_hls_substream("https://radio.example.com/master.m3u8")
        assert substream.path == "https://radio.example.com/high.m3u8"

    @pytest.mark.asyncio
    async def test_undecodable_byte_degrades_instead_of_raising(self) -> None:
        """One bad byte costs a character, not the whole stream."""
        raw_data = MASTER_PLAYLIST.encode().replace(b"#EXTM3U", b"#EXTM3U\n#\xff")
        controller = _streams_audio(
            raw_data, charset="utf-8", real_url="https://radio.example.com/master.m3u8"
        )
        substream = await controller.get_hls_substream("https://radio.example.com/master.m3u8")
        assert substream.path == "https://radio.example.com/high.m3u8"

    @pytest.mark.asyncio
    async def test_url_forward_with_differing_real_url(self) -> None:
        """Differing real_url parameters is respected for the created substream path in relative paths."""
        raw_data = MASTER_PLAYLIST.encode().replace(b"https://radio.example.com/", b"")
        controller = _streams_audio(
            raw_data, charset="utf-8", real_url="https://forwarded.example.com/master.m3u8"
        )
        substream = await controller.get_hls_substream("https://radio.example.com/master.m3u8")
        assert substream.path == "https://forwarded.example.com/high.m3u8"


@pytest.mark.asyncio
async def test_hls_radio_title_and_artist_are_decoded() -> None:
    """Entity-encoded EXTINF fields reach the player metadata decoded."""
    controller = StreamsAudio(MagicMock())
    playlist = '#EXTM3U\n#EXTINF:4,title="I&apos;m Losing You",artist="John Lennon"\nseg.aac\n'
    response = MagicMock()
    response.__aenter__.return_value.text = AsyncMock(return_value=playlist)
    http_session: Any = controller.mass.http_session_no_ssl
    http_session.get = MagicMock(return_value=response)
    update = MagicMock()
    controller._update_radio_stream_metadata = update  # type: ignore[method-assign]
    details = MagicMock(data={"hls_media_playlist_url": "https://a.example"}, stream_title=None)

    await controller._update_hls_radio_metadata(details, 0)

    assert details.stream_title == "John Lennon - I'm Losing You"
    assert update.call_args.kwargs["title"] == "I'm Losing You"
    assert update.call_args.kwargs["artist"] == "John Lennon"
