"""Tests for HLS substream resolution."""

from __future__ import annotations

from typing import Self
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.streams.audio import StreamsAudio


class _ResponseContext:
    """Minimal async context manager for mocked playlist responses."""

    def __init__(self, playlist: str) -> None:
        self.charset = "utf-8"
        self._playlist = playlist

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    def raise_for_status(self) -> None:
        """No-op for successful mocked responses."""

    async def read(self) -> bytes:
        """Return the mocked playlist body."""
        return self._playlist.encode()


@pytest.mark.asyncio
async def test_get_hls_substream_resolves_root_relative_child() -> None:
    """Root-relative child playlists should resolve against the master URL origin."""
    playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=128000
/music/:/transcode/session/children/stream.m3u8?X-Plex-Token=token
"""
    mass = MagicMock()
    mass.http_session_no_ssl.get.return_value = _ResponseContext(playlist)
    audio = StreamsAudio(mass)

    substream = await audio.get_hls_substream(
        "http://plex.local:32400/music/:/transcode/universal/start.m3u8?path=%2Flibrary%2F1"
    )

    assert (
        substream.path == "http://plex.local:32400/music/:/transcode/session/children/stream.m3u8?"
        "X-Plex-Token=token"
    )


@pytest.mark.asyncio
async def test_get_hls_substream_resolves_directory_relative_child() -> None:
    """Directory-relative child playlists should keep existing HLS behavior."""
    playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=128000
children/stream.m3u8
"""
    mass = MagicMock()
    mass.http_session_no_ssl.get.return_value = _ResponseContext(playlist)
    audio = StreamsAudio(mass)

    substream = await audio.get_hls_substream("http://media.local/live/master.m3u8")

    assert substream.path == "http://media.local/live/children/stream.m3u8"
