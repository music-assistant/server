"""Test that Deezer hears of a playback once."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Self
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.deezer.streaming import DeezerStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

INSTANCE = "deezer--test123"


class _Response:
    """A fake track download of two chunks."""

    status = 200

    async def _iter_chunked(self, size: int) -> AsyncGenerator[bytes]:
        for _ in range(2):
            yield b"0" * size

    @property
    def content(self) -> Mock:
        return Mock(iter_chunked=self._iter_chunked)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _streamdetails() -> StreamDetails:
    return StreamDetails(
        provider=INSTANCE,
        item_id="1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        duration=200,
        queue_id="queue",
        data={"url": "http://x", "format": "FLAC", "track_id": "1"},
    )


@pytest.fixture
def provider() -> Mock:
    """Return a provider with a stubbed GW client, track download and queue."""
    provider = Mock(instance_id=INSTANCE)
    provider.gw_client.log_listen = AsyncMock()
    provider.mass.create_task.side_effect = lambda coro: coro.close()
    provider.mass.http_session.get = lambda *_args, **_kwargs: _Response()
    provider.mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue", current_index=0
    )
    return provider


@pytest.fixture
def streaming(provider: Mock, monkeypatch: pytest.MonkeyPatch) -> DeezerStreamingManager:
    """Wire up a real streaming manager on the stubbed provider."""
    manager = DeezerStreamingManager(provider)
    monkeypatch.setattr(manager, "_get_blowfish_key", lambda _track_id: "0" * 16)
    monkeypatch.setattr(manager, "_decrypt_chunk", lambda chunk, _key: chunk)
    return manager


def _queue(provider: Mock, *items: SimpleNamespace) -> None:
    """Let the queue hold the given items, starting at its current index."""
    provider.mass.player_queues.get_item.side_effect = lambda _queue_id, index: (
        items[index] if index < len(items) else None
    )


def _played_from(streamdetails: StreamDetails, container: None) -> SimpleNamespace:
    return SimpleNamespace(streamdetails=streamdetails, origin=SimpleNamespace(container=container))


async def test_fetching_a_track_reports_no_listen(
    provider: Mock, streaming: DeezerStreamingManager
) -> None:
    """A track that is only buffered ahead, and never plays, is not reported."""
    streamdetails = _streamdetails()

    _ = [chunk async for chunk in streaming._stream_encrypted_track(streamdetails)]
    await streaming.on_streamed(streamdetails)

    provider.gw_client.log_listen.assert_not_called()


async def test_streams_of_one_playback_share_their_listen(
    provider: Mock, streaming: DeezerStreamingManager
) -> None:
    """A refetch keeps the start and stream id, which Deezer merges into one listen."""
    streamdetails = _streamdetails()
    _queue(provider, _played_from(streamdetails, None))

    await streaming.on_stream_started(streamdetails)
    started = (streamdetails.data["start_ts"], streamdetails.data["stream_id"])
    for _ in range(2):
        _ = [chunk async for chunk in streaming._stream_encrypted_track(streamdetails)]
        await streaming.on_streamed(streamdetails)

    assert (streamdetails.data["start_ts"], streamdetails.data["stream_id"]) == started
    assert provider.gw_client.log_listen.call_args_list[0].kwargs == {"next_track": "1"}
