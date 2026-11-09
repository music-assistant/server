"""Test that Deezer hears of a playback once, with the album, artist or playlist it came from."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Self
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat, ItemMapping
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.deezer.gw_client import GWClient
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


def _played_from(streamdetails: StreamDetails, container: ItemMapping | None) -> SimpleNamespace:
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


async def test_listen_names_the_library_playlist(
    provider: Mock, streaming: DeezerStreamingManager
) -> None:
    """A library playlist is named by its Deezer id, so Deezer lists it as recently played."""
    streamdetails = _streamdetails()
    playlist = ItemMapping(
        media_type=MediaType.PLAYLIST, item_id="85", provider="library", name="Peaceful Piano"
    )
    # the next item can start reaching the player before the queue moves on to it
    _queue(provider, _played_from(_streamdetails(), None), _played_from(streamdetails, playlist))
    provider.mass.music.get_controller.return_value.get_library_item = AsyncMock(
        return_value=SimpleNamespace(
            provider_mappings=[
                SimpleNamespace(provider_instance="tidal--test", item_id="abc"),
                SimpleNamespace(provider_instance=INSTANCE, item_id="14134446001"),
            ]
        )
    )

    await streaming.on_stream_started(streamdetails)

    assert streamdetails.data["context"] == {
        "t": "playlist_page",
        "id": "14134446001",
        "c": "14134446001",
    }


@pytest.mark.parametrize(
    "container",
    [
        ItemMapping(media_type=MediaType.PLAYLIST, item_id="flow", provider=INSTANCE, name="Flow"),
        ItemMapping(media_type=MediaType.ALBUM, item_id="abc", provider="tidal--test", name="A"),
        None,
    ],
    ids=["virtual playlist", "other service", "no container"],
)
async def test_listen_without_deezer_page_keeps_the_default(
    provider: Mock, streaming: DeezerStreamingManager, container: ItemMapping | None
) -> None:
    """A container Deezer has no page for leaves the listen without a context."""
    streamdetails = _streamdetails()
    _queue(provider, _played_from(streamdetails, container))

    await streaming.on_stream_started(streamdetails)

    assert "context" not in streamdetails.data


async def test_log_listen_sends_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listen carries the album's context, a track on its own the default one."""
    client = GWClient(Mock(), "arl")
    api_call = AsyncMock()
    monkeypatch.setattr(client, "_gw_api_call", api_call)
    streamdetails = _streamdetails()
    streamdetails.data |= {"start_ts": 0, "stream_id": "s"}
    sent = []
    for context in ({"t": "album_page", "id": "2", "c": "2"}, None):
        streamdetails.data["context"] = context
        await client.log_listen(last_track=streamdetails)
        sent.append(api_call.call_args.kwargs["args"]["params"]["ctxt"])

    assert sent == [{"t": "album_page", "id": "2", "c": "2"}, {"t": "search_page", "id": "1"}]
