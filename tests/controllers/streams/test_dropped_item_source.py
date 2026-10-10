"""Tests for handing the stream slot of a track a player gave up on to the track it asks for next."""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

QUEUE_ID = "q1"
SESSION_ID = "sess1"
PLAYER_ID = "player1"
LIMITED = "limited--1"
UNLIMITED = "local--1"
# audio chunks every streamed track has
CHUNKS = 3


class _Connection:
    """A player connection whose writes the test lets through or breaks off one at a time."""

    def __init__(self) -> None:
        """Initialize a connection without any write waiting yet."""
        self._pending: asyncio.Queue[None] = asyncio.Queue()
        self._verdicts: asyncio.Queue[bool] = asyncio.Queue()
        self.writer = Mock()
        self.writer.write_headers = AsyncMock()
        self.writer.write = AsyncMock(side_effect=self._write)
        self.writer.write_eof = AsyncMock()
        self.writer.drain = AsyncMock()

    async def next_write(self) -> None:
        """Wait until the response tries to write its next chunk to the player."""
        await asyncio.wait_for(self._pending.get(), 1)

    def deliver(self) -> None:
        """Let the waiting write reach the player."""
        self._verdicts.put_nowait(True)

    def drop(self) -> None:
        """Break the connection off, as a player does when it gives up on the track."""
        self._verdicts.put_nowait(False)

    async def _write(self, _data: bytes) -> None:
        """Hold a write until the test decides whether the player still reads."""
        self._pending.put_nowait(None)
        if not await self._verdicts.get():
            raise ConnectionResetError("Cannot write to closing transport")


class _Harness:
    """A streams controller serving one queue, with the queue's real source release behind it."""

    def __init__(self, *items: QueueItem) -> None:
        """
        Wire a bare streams controller to a queue holding the given items.

        :param items: The queue items, in queue order.
        """
        self.calls: list[str] = []
        self.details_waiting = asyncio.Event()
        self.details_released = asyncio.Event()
        self._background: list[asyncio.Task[Any]] = []
        self.queue_data = PlayerQueueData(
            queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"),
            items=list(items),
            session_id=SESSION_ID,
        )
        self._queues = _queues_controller(self.queue_data)
        self.release = AsyncMock(side_effect=self._release)

        player_queues = MagicMock()
        player_queues.get.return_value = self.queue_data.queue
        player_queues.queue_data.return_value = self.queue_data
        player_queues.get_item.side_effect = self._queues.get_item
        player_queues.track_loaded_in_buffer.side_effect = self._track_loaded
        player_queues.release_failed_item_source = self.release

        player = MagicMock()
        player.stop_called = False
        player.get_config_value.return_value = "default"

        self.ctrl = MagicMock()
        self.ctrl.logger = Mock()
        self.ctrl._log_request = Mock()
        self.ctrl._raise_if_stale_item_request = Mock()
        self.ctrl._open_item_streams = {}
        self.ctrl._active_output_streams = 0
        # bind the real helper: the mocked self would silently swallow it otherwise
        self.ctrl._requested_item_ids = MethodType(StreamsController._requested_item_ids, self.ctrl)
        self.ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
        self.ctrl.mass.player_queues = player_queues
        self.ctrl.mass.players.get_player.return_value = player
        self.ctrl.mass.config.get_raw_core_config_value.return_value = 8
        self.ctrl.mass.create_task.side_effect = self._create_task
        pcm_format = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100)
        self.ctrl.audio.select_pcm_format = AsyncMock(return_value=pcm_format)
        self.ctrl.audio.get_output_format = AsyncMock(
            return_value=AudioFormat(content_type=ContentType.FLAC, sample_rate=44100)
        )
        self.ctrl.audio.get_stream_details = AsyncMock(side_effect=self._stream_details)
        self.ctrl.audio.get_queue_item_stream.side_effect = _source

    def start(
        self, item_id: str, method: str = "GET"
    ) -> tuple[asyncio.Task[web.StreamResponse], _Connection]:
        """
        Let the player request the audio of a queue item.

        :param item_id: The queue item the player asks for.
        :param method: The HTTP method of the request.
        """
        connection = _Connection()
        request = make_mocked_request(
            method,
            f"/single/{SESSION_ID}/{QUEUE_ID}/{item_id}/{PLAYER_ID}.flac",
            match_info={
                "session_id": SESSION_ID,
                "queue_id": QUEUE_ID,
                "queue_item_id": item_id,
                "player_id": PLAYER_ID,
                "fmt": "flac",
            },
            writer=connection.writer,
        )
        task = asyncio.create_task(StreamsController.serve_queue_item_stream(self.ctrl, request))
        return task, connection

    async def settle(self) -> None:
        """Wait for the source releases running in the background."""
        await asyncio.gather(*self._background)

    async def _release(self, queue_id: str, queue_item_id: str) -> None:
        """Record the release, then let the queue release the item's source for real."""
        self.calls.append(f"release:{queue_item_id}")
        await self._queues.release_failed_item_source(queue_id, queue_item_id)

    def _track_loaded(self, queue_id: str, item_id: str) -> None:
        """Record the item whose first chunk reached the player, as the queue does."""
        self.queue_data.last_served_item_id = item_id

    async def _stream_details(self, queue_item: QueueItem) -> StreamDetails:
        """Resolve nothing: hold the request until the test lets it fail."""
        self.calls.append(f"details:{queue_item.queue_item_id}")
        self.details_waiting.set()
        await self.details_released.wait()
        raise RuntimeError("no stream details in this test")

    def _create_task(self, coro: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        """Start a background task the way the server does, eagerly."""
        task: asyncio.Task[Any] = asyncio.Task(
            coro, loop=asyncio.get_running_loop(), eager_start=True
        )
        self._background.append(task)
        return task


def _queues_controller(queue_data: PlayerQueueData) -> PlayerQueuesController:
    """Build a bare queue controller that holds the given queue and two providers."""
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.logger = MagicMock()
    queues._queue_data = {QUEUE_ID: queue_data}
    limited = MagicMock(spec=MusicProvider)
    limited.name = "Limited"
    limited.max_concurrent_streams = 1
    limited.has_available_stream_slot = False
    unlimited = MagicMock(spec=MusicProvider)
    unlimited.name = "Local"
    unlimited.max_concurrent_streams = None
    unlimited.has_available_stream_slot = True
    providers = {LIMITED: limited, UNLIMITED: unlimited}
    queues.mass = MagicMock()
    queues.mass.get_provider.side_effect = lambda instance, **_kwargs: providers.get(instance)
    return queues


def _item(item_id: str, provider: str | None = None) -> QueueItem:
    """
    Build a queue item, with a still-filling source when a provider is given.

    :param item_id: The queue item id.
    :param provider: Provider instance of the item's source, or None for an item whose
        audio the request still has to resolve.
    """
    queue_item = QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=180)
    if provider is None:
        return queue_item
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.is_buffering = True
    audio_buffer.clear = AsyncMock()
    queue_item.streamdetails = StreamDetails(
        provider=provider,
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path=f"http://test.invalid/{item_id}.mp3",
        duration=180,
    )
    queue_item.streamdetails.buffer = audio_buffer
    return queue_item


def _buffer(queue_item: QueueItem) -> MagicMock:
    """Return the buffer double attached to a queue item's source."""
    assert queue_item.streamdetails is not None
    assert isinstance(queue_item.streamdetails.buffer, MagicMock)
    return queue_item.streamdetails.buffer


async def _source(**_kwargs: Any) -> AsyncGenerator[bytes]:
    """Yield the audio of the requested queue item."""
    for _ in range(CHUNKS):
        yield b"\0" * 4


async def _stream_first_chunk(connection: _Connection) -> None:
    """Deliver the first chunk and leave the response streaming, waiting at the next one."""
    await connection.next_write()
    connection.deliver()
    await connection.next_write()


@pytest.fixture(autouse=True)
def _passthrough_encoder() -> Iterator[None]:
    """Hand the item audio to the player as it comes, without an encoder in between."""
    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
        side_effect=lambda audio_input, **_kwargs: audio_input,
    ):
        yield


async def test_a_track_dropped_earlier_hands_its_slot_to_the_next_request() -> None:
    """A player that broke off a track and later asks for another one gets the slot back."""
    dropped, upcoming = _item("dropped", LIMITED), _item("upcoming")
    harness = _Harness(dropped, upcoming)
    harness.details_released.set()
    stream, player = harness.start("dropped")
    await _stream_first_chunk(player)
    player.drop()
    await stream
    await harness.settle()
    # nothing tells yet whether the player comes back for it
    _buffer(dropped).clear.assert_not_awaited()

    with pytest.raises(web.HTTPNotFound):
        await harness.start("upcoming")[0]
    await harness.settle()

    _buffer(dropped).clear.assert_awaited_once()
    # freed before the requested track resolves its own audio
    assert harness.calls == ["release:dropped", "details:upcoming"]


async def test_a_track_dropped_after_the_next_request_hands_its_slot_over() -> None:
    """The next request usually arrives before the server notices the drop on its next write."""
    dropped, upcoming = _item("dropped", LIMITED), _item("upcoming")
    harness = _Harness(dropped, upcoming)
    stream, player = harness.start("dropped")
    await _stream_first_chunk(player)
    next_request, _ = harness.start("upcoming")
    await harness.details_waiting.wait()
    # still streaming when the next track was asked for
    harness.release.assert_not_awaited()

    player.drop()
    await stream
    await harness.settle()

    _buffer(dropped).clear.assert_awaited_once()
    harness.details_released.set()
    with pytest.raises(web.HTTPNotFound):
        await next_request


@pytest.mark.parametrize("asks_again_first", [False, True], ids=["after-drop", "before-drop"])
async def test_asking_for_the_same_track_again_keeps_its_source(asks_again_first: bool) -> None:
    """A reconnect for the same track (range request, resume, seek) needs its source."""
    track = _item("track", LIMITED)
    harness = _Harness(track)
    first, first_player = harness.start("track")
    await _stream_first_chunk(first_player)
    if asks_again_first:
        second, second_player = harness.start("track")
        await second_player.next_write()
        first_player.drop()
        await first
    else:
        first_player.drop()
        await first
        second, second_player = harness.start("track")
        await second_player.next_write()
    second_player.drop()
    await second
    await harness.settle()

    _buffer(track).clear.assert_not_awaited()


async def test_fetching_the_next_track_early_keeps_the_playing_tracks_source() -> None:
    """A gapless player asks for the next track while the playing one still streams."""
    playing, upcoming = _item("playing", LIMITED), _item("upcoming")
    harness = _Harness(playing, upcoming)
    stream, player = harness.start("playing")
    await _stream_first_chunk(player)
    prefetch, _ = harness.start("upcoming")
    await harness.details_waiting.wait()
    harness.release.assert_not_awaited()

    # the source delivered all of its audio before the response streamed its last chunk
    _buffer(playing).is_buffering = False
    player.deliver()
    await player.next_write()
    player.deliver()
    await stream
    harness.details_released.set()
    with pytest.raises(web.HTTPNotFound):
        await prefetch
    await harness.settle()

    _buffer(playing).clear.assert_not_awaited()


async def test_breaking_off_an_early_fetch_keeps_its_source_while_the_playing_track_streams() -> (
    None
):
    """Only a request that came in after the dropped one shows the player moved on."""
    playing, upcoming = _item("playing", LIMITED), _item("upcoming", LIMITED)
    harness = _Harness(playing, upcoming)
    stream, player = harness.start("playing")
    await _stream_first_chunk(player)
    prefetch, early_player = harness.start("upcoming")
    await _stream_first_chunk(early_player)

    early_player.drop()
    await prefetch
    await harness.settle()

    _buffer(upcoming).clear.assert_not_awaited()
    player.drop()
    await stream
    await harness.settle()
    harness.release.assert_not_awaited()


async def test_a_source_without_a_stream_limit_is_left_alone() -> None:
    """A provider without a stream limit has no slot to hand over."""
    dropped, upcoming = _item("dropped", UNLIMITED), _item("upcoming")
    harness = _Harness(dropped, upcoming)
    harness.details_released.set()
    stream, player = harness.start("dropped")
    await _stream_first_chunk(player)
    player.drop()
    await stream

    with pytest.raises(web.HTTPNotFound):
        await harness.start("upcoming")[0]
    await harness.settle()

    harness.release.assert_awaited_once_with(QUEUE_ID, "dropped")
    _buffer(dropped).clear.assert_not_awaited()


async def test_a_head_probe_for_another_track_releases_nothing() -> None:
    """A probe is not a request for audio, so it does not show the player moved on."""
    dropped, upcoming = _item("dropped", LIMITED), _item("upcoming")
    harness = _Harness(dropped, upcoming)
    harness.details_released.set()
    stream, player = harness.start("dropped")
    await _stream_first_chunk(player)
    player.drop()
    await stream

    with pytest.raises(web.HTTPNotFound):
        await harness.start("upcoming", method="HEAD")[0]
    await harness.settle()

    harness.release.assert_not_awaited()


async def test_a_session_music_assistant_moved_on_from_releases_nothing() -> None:
    """Music Assistant hands over the sources of its own skips and seeks itself."""
    playing, upcoming = _item("playing", LIMITED), _item("upcoming")
    harness = _Harness(playing, upcoming)
    stream, player = harness.start("playing")
    await _stream_first_chunk(player)
    pending, _ = harness.start("upcoming")
    await harness.details_waiting.wait()

    harness.queue_data.session_id = "sess2"
    # the superseded response is aborted under the player
    player.drop()
    await stream
    harness.details_released.set()
    with pytest.raises(web.HTTPNotFound):
        await pending
    await harness.settle()

    harness.release.assert_not_awaited()
