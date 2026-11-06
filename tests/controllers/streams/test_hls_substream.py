"""Tests for HLS master playlist selection on the streams controller."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import MagicMock

import aiohttp
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
    async def test_resolves_root_relative_child(self) -> None:
        """Root-relative child playlists should resolve against the master URL origin."""
        playlist = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=128000\n"
            "/music/:/transcode/session/children/stream.m3u8?X-Plex-Token=token\n"
        )
        controller = _streams_audio(
            playlist.encode(),
            charset="utf-8",
            real_url="http://plex.local:32400/music/:/transcode/universal/start.m3u8?path=%2Flibrary%2F1",
        )

        substream = await controller.get_hls_substream(
            "http://plex.local:32400/music/:/transcode/universal/start.m3u8?path=%2Flibrary%2F1"
        )

        assert (
            substream.path
            == "http://plex.local:32400/music/:/transcode/session/children/stream.m3u8?"
            "X-Plex-Token=token"
        )

    @pytest.mark.asyncio
    async def test_resolves_directory_relative_child(self) -> None:
        """Directory-relative child playlists should resolve against the master URL."""
        playlist = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000\nchildren/stream.m3u8\n"
        controller = _streams_audio(
            playlist.encode(), charset="utf-8", real_url="http://media.local/live/master.m3u8"
        )

        substream = await controller.get_hls_substream("http://media.local/live/master.m3u8")

        assert substream.path == "http://media.local/live/children/stream.m3u8"

    @pytest.mark.asyncio
    async def test_url_forward_with_differing_real_url(self) -> None:
        """Differing real_url parameters is respected for the created substream path in relative paths."""
        raw_data = MASTER_PLAYLIST.encode().replace(b"https://radio.example.com", b"")
        controller = _streams_audio(
            raw_data, charset="utf-8", real_url="https://forwarded.example.com/master.m3u8"
        )
        substream = await controller.get_hls_substream("https://radio.example.com/master.m3u8")
        assert substream.path == "https://forwarded.example.com/high.m3u8"


class TestHlsRadioStart:
    """An HLS radio stream starts behind live by time, not by a fixed number of segments."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target_duration", "segments"),
        [("4", 6), ("6", 4), ("10", 3), ("16", 3), ("2.5", 10)],
    )
    async def test_start_follows_the_segment_length(
        self, target_duration: str, segments: int
    ) -> None:
        """Short segments start further back, long ones stay at the default of ffmpeg."""
        playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:{target_duration}\n#EXTINF:4,\nseg.aac\n"
        controller = _streams_audio(playlist.encode(), None, "https://radio.example.com/a.m3u8")

        assert await controller._get_hls_radio_start_segments("https://radio.example.com/a") == (
            segments
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "playlist", ["#EXTM3U\n#EXTINF:4,\nseg.aac\n", "#EXTM3U\n#EXT-X-TARGETDURATION:0\n"]
    )
    async def test_an_unusable_playlist_keeps_the_default_start(self, playlist: str) -> None:
        """Without a usable segment length the start stays where it always was."""
        controller = _streams_audio(playlist.encode(), None, "https://radio.example.com/a.m3u8")

        assert await controller._get_hls_radio_start_segments("https://radio.example.com/a") == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [aiohttp.ClientError(), TimeoutError()])
    async def test_a_failed_request_does_not_stop_the_stream(self, error: Exception) -> None:
        """The playlist is only a hint, the stream starts without it."""
        controller = _streams_audio(b"", None, "https://radio.example.com/a.m3u8")
        http_session: Any = controller.mass.http_session_no_ssl
        http_session.get = MagicMock(side_effect=error)

        assert await controller._get_hls_radio_start_segments("https://radio.example.com/a") == 3


def _titled_playlist(titles: list[str]) -> str:
    """Return a media playlist with one titled segment for each title."""
    entries = "".join(f'#EXTINF:4,title="{title}"\nseg.aac\n' for title in titles)
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:4\n{entries}"


class _FakeTextResponse(_FakeResponse):
    """A response that also answers the text of the playlist."""

    async def text(self) -> str:
        return self._raw_data.decode()


class TestHlsRadioTitle:
    """The title comes from the segment that plays now, not from the newest one."""

    @staticmethod
    async def _title(titles: list[str], data: dict[str, Any]) -> str | None:
        controller = StreamsAudio(MagicMock())
        response = _FakeTextResponse(_titled_playlist(titles).encode(), None, "https://a.example")
        http_session: Any = controller.mass.http_session_no_ssl
        http_session.get = MagicMock(return_value=response)
        controller._update_radio_stream_metadata = MagicMock()  # type: ignore[method-assign]
        details = MagicMock(
            data={"hls_media_playlist_url": "https://a.example", **data}, stream_title=None
        )
        await controller._update_hls_radio_metadata(details, 0)
        return cast("str | None", details.stream_title)

    @pytest.mark.asyncio
    async def test_title_matches_how_far_the_stream_started_behind_live(self) -> None:
        """A stream that started 3 segments back plays the third newest segment."""
        title = await self._title(["A", "B", "C", "D", "E"], {"hls_start_segments": 3})

        assert title == "C"

    @pytest.mark.asyncio
    async def test_a_short_playlist_uses_its_oldest_title(self) -> None:
        """Ffmpeg starts at the first segment when the playlist is shorter than the offset."""
        title = await self._title(["A", "B"], {"hls_start_segments": 6})

        assert title == "A"

    @pytest.mark.asyncio
    async def test_title_before_the_stream_started_is_the_newest(self) -> None:
        """Before a start offset is known, the newest title is the only one to go by."""
        assert await self._title(["A", "B"], {}) == "B"
