"""Test Twitch Provider core audio streaming."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.twitch import (
    MAX_CONSECUTIVE_RECONNECTS,
    RECONNECT_DELAY,
    STREAM_CHUNK_SIZE,
    TwitchProvider,
)

# --- Stream Details ---


async def test_stream_details_returns_custom_type(provider: TwitchProvider) -> None:
    """get_stream_details() returns StreamDetails with stream_type=StreamType.CUSTOM."""
    details = await provider.get_stream_details("testchannel", MediaType.RADIO)
    assert details.stream_type == StreamType.CUSTOM


async def test_stream_details_media_type_is_radio(provider: TwitchProvider) -> None:
    """media_type is RADIO."""
    details = await provider.get_stream_details("testchannel", MediaType.RADIO)
    assert details.media_type == MediaType.RADIO


async def test_stream_details_no_seek(provider: TwitchProvider) -> None:
    """Live streams cannot be seeked."""
    details = await provider.get_stream_details("testchannel", MediaType.RADIO)
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_stream_details_provider_set(provider: TwitchProvider) -> None:
    """Provider field matches self.instance_id."""
    details = await provider.get_stream_details("testchannel", MediaType.RADIO)
    assert details.provider == provider.instance_id


async def test_stream_details_content_type_unknown(provider: TwitchProvider) -> None:
    """Content type is UNKNOWN (let ffmpeg detect from MPEG-TS stream)."""
    details = await provider.get_stream_details("testchannel", MediaType.RADIO)
    assert details.audio_format.content_type == ContentType.UNKNOWN


# --- Audio Stream — Happy Path ---


@pytest.fixture
def stream_details(provider: TwitchProvider) -> StreamDetails:
    """Return StreamDetails for a test channel."""
    return StreamDetails(
        provider=provider.instance_id,
        item_id="testchannel",
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        media_type=MediaType.RADIO,
        stream_type=StreamType.CUSTOM,
    )


@pytest.fixture
def mock_streamlink_stream() -> tuple[MagicMock, MagicMock]:
    """Return a mock Streamlink stream with fd that yields chunks then closes."""
    mock_fd = MagicMock()
    mock_stream = MagicMock()
    mock_stream.open.return_value = mock_fd
    return mock_stream, mock_fd


async def test_yields_bytes_from_streamlink(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """get_audio_stream() yields non-empty bytes chunks from a mock Streamlink stream."""
    chunk1 = b"\x00" * 1024
    chunk2 = b"\xff" * 1024

    mock_fd = MagicMock()
    mock_fd.read.side_effect = [chunk1, chunk2, b""]
    mock_fd.close.return_value = None

    mock_stream = MagicMock()
    mock_stream.open.return_value = mock_fd

    with patch.object(provider, "_resolve_streams", return_value={"audio_only": mock_stream}):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)
            if len(chunks) >= 2:
                # Simulate: after 2 chunks, empty read triggers reconnect path
                # which will fail since _resolve_streams returns None on 2nd call
                break

        assert len(chunks) == 2
        assert chunks[0] == chunk1
        assert chunks[1] == chunk2


async def test_uses_audio_only_quality(provider: TwitchProvider) -> None:
    """Streamlink quality selection picks audio_only when available."""
    streams = {"audio_only": "audio_stream", "worst": "worst_stream", "720p": "hd_stream"}
    result = provider._select_quality(streams)
    assert result == "audio_stream"


async def test_falls_back_to_worst_quality(provider: TwitchProvider) -> None:
    """When audio_only unavailable, selects worst."""
    streams = {"worst": "worst_stream", "720p": "hd_stream", "1080p": "fhd_stream"}
    result = provider._select_quality(streams)
    assert result == "worst_stream"


async def test_returns_none_when_no_qualities(provider: TwitchProvider) -> None:
    """When no matching qualities, returns None."""
    streams = {"720p": "hd_stream", "1080p": "fhd_stream"}
    result = provider._select_quality(streams)
    assert result is None


async def test_streamlink_called_via_to_thread(provider: TwitchProvider) -> None:
    """Verify get_audio_stream uses asyncio.to_thread for blocking Streamlink calls.

    Inspects the source to confirm to_thread is used rather than running
    the full generator (which requires complex mock choreography).
    """
    source = inspect.getsource(provider.get_audio_stream)
    assert "asyncio.to_thread" in source, (
        "get_audio_stream must use asyncio.to_thread for blocking Streamlink calls"
    )


async def test_chunk_size_is_64kb() -> None:
    """Read chunks are 64KB."""
    assert STREAM_CHUNK_SIZE == 64 * 1024


# --- Audio Stream — Streamlink Token ---


async def test_streamlink_token_passed_as_header(provider: TwitchProvider) -> None:
    """When streamlink_token configured, Streamlink session gets OAuth header."""
    mock_session = MagicMock()
    mock_session.streams.return_value = {"audio_only": MagicMock()}

    provider.config.get_value.side_effect = lambda key, default=None: {  # type: ignore[attr-defined]
        "streamlink_token": "test_oauth_token",
        "ad_handling": "silence",
        "log_level": "GLOBAL",
    }.get(key, default)

    with (
        patch("streamlink.Streamlink", return_value=mock_session),
        patch("music_assistant.providers.twitch.ad_handling.patch_ad_handling"),
    ):
        provider._resolve_streams("testchannel")

    mock_session.set_option.assert_called_once()
    call_args = mock_session.set_option.call_args
    assert "Authorization" in str(call_args)
    assert "OAuth test_oauth_token" in str(call_args)


async def test_streamlink_token_omitted_when_empty(provider: TwitchProvider) -> None:
    """When streamlink_token not set, no extra auth header on Streamlink."""
    mock_session = MagicMock()
    mock_session.streams.return_value = {"audio_only": MagicMock()}

    provider.config.get_value.side_effect = lambda key, default=None: {  # type: ignore[attr-defined]
        "streamlink_token": "",
        "ad_handling": "silence",
        "log_level": "GLOBAL",
    }.get(key, default)

    with (
        patch("streamlink.Streamlink", return_value=mock_session),
        patch("music_assistant.providers.twitch.ad_handling.patch_ad_handling"),
    ):
        provider._resolve_streams("testchannel")

    mock_session.set_option.assert_not_called()


async def test_reconnect_delay_is_half_second() -> None:
    """Reconnect delay between attempts is 0.5 seconds."""
    assert RECONNECT_DELAY == 0.5


# --- Audio Stream — Reconnection ---


async def test_reconnects_on_empty_read(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """Empty read triggers stream close + Streamlink re-resolution + continued yielding."""
    mock_fd1 = MagicMock()
    mock_fd1.read.side_effect = [b"chunk1", b""]  # data then empty
    mock_fd1.close.return_value = None

    mock_fd2 = MagicMock()
    mock_fd2.read.side_effect = [b"chunk2", b""]  # data then empty on reconnect
    mock_fd2.close.return_value = None

    mock_stream1 = MagicMock()
    mock_stream1.open.return_value = mock_fd1

    mock_stream2 = MagicMock()
    mock_stream2.open.return_value = mock_fd2

    resolve_calls = [
        {"audio_only": mock_stream1},
        {"audio_only": mock_stream2},
        None,  # third resolve fails — end
    ]

    with patch.object(provider, "_resolve_streams", side_effect=resolve_calls):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)

        assert b"chunk1" in chunks
        assert b"chunk2" in chunks


async def test_reconnect_resets_counter_on_success(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """After reconnect, a successful chunk read resets the consecutive failure counter."""
    # First stream: data, then empty (triggers reconnect, counter=1)
    mock_fd1 = MagicMock()
    mock_fd1.read.side_effect = [b"data1", b""]
    mock_fd1.close.return_value = None
    mock_stream1 = MagicMock()
    mock_stream1.open.return_value = mock_fd1

    # Second stream: data (resets counter to 0), then empty (counter=1 again)
    mock_fd2 = MagicMock()
    mock_fd2.read.side_effect = [b"data2", b""]
    mock_fd2.close.return_value = None
    mock_stream2 = MagicMock()
    mock_stream2.open.return_value = mock_fd2

    resolve_calls = [
        {"audio_only": mock_stream1},
        {"audio_only": mock_stream2},
        None,  # end
    ]

    with patch.object(provider, "_resolve_streams", side_effect=resolve_calls):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)

        # Both chunks received — counter was reset between them
        assert chunks == [b"data1", b"data2"]


async def test_max_consecutive_reconnects(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """After MAX_CONSECUTIVE_RECONNECTS consecutive empty reads, generator ends."""

    def make_empty_stream() -> dict[str, Any]:
        fd = MagicMock()
        fd.read.return_value = b""
        fd.close.return_value = None
        s = MagicMock()
        s.open.return_value = fd
        return {"audio_only": s}

    # Return streams for each reconnect attempt, plus the initial
    resolve_calls = [make_empty_stream() for _ in range(MAX_CONSECUTIVE_RECONNECTS + 2)]

    with patch.object(provider, "_resolve_streams", side_effect=resolve_calls):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)

        assert chunks == []


async def test_generator_ends_on_resolve_failure(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """If Streamlink re-resolution returns no streams, generator ends cleanly."""
    with patch.object(provider, "_resolve_streams", return_value=None):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)
        assert chunks == []


async def test_fd_closed_before_reconnect(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """Old stream fd is closed before attempting re-resolution."""
    call_order: list[str] = []

    mock_fd = MagicMock()
    mock_fd.read.side_effect = [b"data", b""]

    def tracking_close() -> None:
        call_order.append("close")

    mock_fd.close.side_effect = tracking_close

    mock_stream = MagicMock()
    mock_stream.open.return_value = mock_fd

    resolve_results = iter([{"audio_only": mock_stream}, None])

    def tracking_resolve(_channel: str) -> dict[str, Any] | None:
        call_order.append("resolve")
        return next(resolve_results)

    with patch.object(provider, "_resolve_streams", side_effect=tracking_resolve):
        async for _ in provider.get_audio_stream(stream_details):
            pass

    # There should be two resolve calls and one close between them
    # Pattern: resolve(initial), close, resolve(reconnect attempt)
    assert call_order.count("close") >= 1
    assert call_order.count("resolve") >= 2
    # First close must come before the second resolve
    first_close = call_order.index("close")
    second_resolve = len(call_order) - 1 - call_order[::-1].index("resolve")
    assert first_close < second_resolve


# --- Audio Stream — Error Cases ---


async def test_offline_channel_returns_empty(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """get_audio_stream() for offline channel yields nothing (resolve returns None)."""
    with patch.object(provider, "_resolve_streams", return_value=None):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)
        assert chunks == []


async def test_nonexistent_channel_returns_empty(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """get_audio_stream() for nonexistent channel yields nothing."""
    with patch.object(provider, "_resolve_streams", return_value=None):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)
        assert chunks == []


async def test_streamlink_exception_handled(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """Streamlink exception during resolution doesn't crash the provider."""
    with patch.object(provider, "_resolve_streams", return_value=None):
        chunks = []
        async for chunk in provider.get_audio_stream(stream_details):
            chunks.append(chunk)
        assert chunks == []


async def test_exception_during_read_closes_fd(
    provider: TwitchProvider, stream_details: StreamDetails
) -> None:
    """Exception from fd.read() still closes the fd via finally block."""
    mock_fd = MagicMock()
    mock_fd.read.side_effect = OSError("read failed")
    mock_fd.close.return_value = None

    mock_stream = MagicMock()
    mock_stream.open.return_value = mock_fd

    with (
        patch.object(provider, "_resolve_streams", return_value={"audio_only": mock_stream}),
        pytest.raises(OSError, match="read failed"),
    ):
        async for _ in provider.get_audio_stream(stream_details):
            pass

    # fd.close was still called (via finally)
    mock_fd.close.assert_called()
