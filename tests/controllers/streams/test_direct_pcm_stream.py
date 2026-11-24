"""Tests for the direct-PCM stream helper on the streams controller."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    StreamType,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat, ItemMapping, Radio
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, TailOverlap

from music_assistant.controllers.streams.controller import StreamsController

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

QUEUE_ID = "player-1"
QUEUE_ITEM_ID = "item-1"


def _queue_item(seek_position: int) -> QueueItem:
    """Build an audiobook queue item whose streamdetails carry the given seek position."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=QUEUE_ITEM_ID,
        name="Some Audiobook",
        duration=7200,
        streamdetails=StreamDetails(
            provider="builtin",
            item_id="book-1",
            audio_format=PCM_FORMAT,
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path="http://example.com/book.mp3",
            duration=7200,
            can_seek=True,
            allow_seek=True,
            queue_id=QUEUE_ID,
            seek_position=seek_position,
        ),
    )


def _controller(queue_item: QueueItem) -> tuple[StreamsController, dict[str, Any]]:
    """
    Build a streams controller that records the kwargs of the single item stream call.

    The queue has neither crossfade nor an audio overlay, which keeps the request on the
    non-flow (single item) branch.

    :param queue_item: The item the controller resolves the stream request to.
    """
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.player_queues.get.return_value = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player", available=True, items=1
    )
    mass.player_queues.get_item.return_value = queue_item
    # the queue's player is no group that shares one stream with its members
    mass.players.get_player.return_value.own_protocol.get_shared_stream.return_value = None
    controller = StreamsController(mass)
    call_kwargs: dict[str, Any] = {}

    def _record(**kwargs: Any) -> object:
        call_kwargs.update(kwargs)
        return object()

    controller.audio = MagicMock()
    controller.audio_processing = MagicMock()
    controller.audio.get_queue_item_stream.side_effect = _record
    return controller, call_kwargs


@pytest.mark.parametrize("seek_position", [1800, 0])
def test_single_item_stream_forwards_the_seek_position(seek_position: int) -> None:
    """A resumed (or seeked) item is streamed from its seek position, not from the start."""
    queue_item = _queue_item(seek_position)
    controller, call_kwargs = _controller(queue_item)

    controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )

    assert call_kwargs["seek_position"] == seek_position


def test_single_item_stream_without_a_player_never_mixes_a_declared_tail_overlap() -> None:
    """A direct PCM stream with no player to mix for plays a declared overlap as part of the item."""
    queue_item = _queue_item(0)
    assert queue_item.streamdetails is not None
    queue_item.streamdetails.tail_overlap = TailOverlap(duration=3.0, next_queue_item_id="item-2")
    controller, call_kwargs = _controller(queue_item)

    controller.get_stream(_media(), PCM_FORMAT)

    assert call_kwargs["queue_item"] is queue_item
    cast("Any", controller.audio).get_queue_item_stream_with_smartfade.assert_not_called()


async def _pcm_chunks(chunks: tuple[bytes, ...]) -> AsyncGenerator[bytes]:
    """Yield the given PCM chunks one at a time."""
    for chunk in chunks:
        yield chunk


def _pcm_stream_controller(
    chunks: tuple[bytes, ...] = (b"chunk-1", b"chunk-2"),
) -> StreamsController:
    """Build a controller whose single item stream yields the given PCM chunks."""
    controller, _ = _controller(_queue_item(0))
    audio = cast("Any", controller.audio)
    audio.get_queue_item_stream.side_effect = lambda **_kwargs: _pcm_chunks(chunks)
    return controller


def _media() -> PlayerMedia:
    """Build the player media that resolves to the audiobook queue item."""
    return PlayerMedia(
        uri="library://audiobook/1",
        media_type=MediaType.AUDIOBOOK,
        source_id=QUEUE_ID,
        queue_item_id=QUEUE_ITEM_ID,
    )


@pytest.mark.asyncio
async def test_direct_pcm_stream_counts_as_an_active_output_stream() -> None:
    """A player consuming raw PCM registers as playing, so analysis yields CPU to it."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )
    assert controller.output_stream_active() is False

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
        assert controller.output_stream_active() is True

    assert chunks == [b"chunk-1", b"chunk-2"]
    assert controller.output_stream_active() is False


@pytest.mark.asyncio
async def test_abandoned_direct_pcm_stream_releases_the_gauge() -> None:
    """A consumer that stops mid-stream releases its count, so analysis regains its budget."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )
    assert await anext(stream) == b"chunk-1"
    assert controller.output_stream_active() is True

    await stream.aclose()
    assert controller.output_stream_active() is False


@pytest.mark.asyncio
async def test_direct_pcm_stream_marks_the_item_served_on_its_first_chunk() -> None:
    """The item counts as served once its first chunk reached the consumer, and only once."""
    controller = _pcm_stream_controller()
    mark_item_served = cast("MagicMock", controller.mass.player_queues.mark_item_served)

    stream = controller.get_stream(_media(), PCM_FORMAT)
    mark_item_served.assert_not_called()

    assert await anext(stream) == b"chunk-1"
    mark_item_served.assert_called_once_with(QUEUE_ID, QUEUE_ITEM_ID)

    assert await anext(stream) == b"chunk-2"
    await stream.aclose()
    mark_item_served.assert_called_once_with(QUEUE_ID, QUEUE_ITEM_ID)


@pytest.mark.asyncio
async def test_direct_pcm_stream_without_audio_marks_nothing_served() -> None:
    """A source that produces no audio leaves the item unserved."""
    controller = _pcm_stream_controller(chunks=())

    stream = controller.get_stream(_media(), PCM_FORMAT)

    assert [chunk async for chunk in stream] == []
    cast("MagicMock", controller.mass.player_queues.mark_item_served).assert_not_called()


@pytest.mark.asyncio
async def test_unconsumed_direct_pcm_stream_marks_nothing_served() -> None:
    """A stream the player never pulled from leaves the item unserved."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(_media(), PCM_FORMAT)
    await stream.aclose()

    cast("MagicMock", controller.mass.player_queues.mark_item_served).assert_not_called()


async def _overlay_mix_that_fails(
    _queue: PlayerQueue, audio_input: AsyncGenerator[bytes], _pcm_format: AudioFormat
) -> AsyncGenerator[bytes]:
    """Read the source like the overlay mixer does, then fail before any mixed audio comes out."""
    async for _chunk in audio_input:
        raise AudioError("overlay mixer failed")
    yield b""  # not reached: the source has a chunk; the yield makes this an async generator


@pytest.mark.asyncio
async def test_direct_radio_stream_is_served_only_once_the_overlay_mix_emits_audio() -> None:
    """A radio source whose overlay mix fails before producing audio leaves the item unserved."""
    controller = _pcm_stream_controller()
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Player",
        available=True,
        items=1,
        overlay_enabled=True,
        overlay_source=ItemMapping(
            media_type=MediaType.SOUND_EFFECT, item_id="rain", provider="builtin", name="Rain"
        ),
    )
    cast("Any", controller.mass.player_queues.get).return_value = queue
    cast("Any", controller.mass.player_queues.get_item).return_value = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=QUEUE_ITEM_ID,
        name="Some Station",
        duration=None,
        media_item=Radio(
            item_id="station-1", provider="builtin", name="Some Station", provider_mappings=set()
        ),
    )
    cast("Any", controller.audio.get_overlay_mixed_stream).side_effect = _overlay_mix_that_fails

    stream = controller.get_stream(
        PlayerMedia(
            uri="library://radio/1",
            media_type=MediaType.RADIO,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )
    with pytest.raises(AudioError):
        await anext(stream)

    cast("MagicMock", controller.mass.player_queues.mark_item_served).assert_not_called()


@pytest.mark.parametrize(
    ("requested_session", "served"),
    [("session-2", True), ("session-1", False), (None, True)],
    ids=["current load", "superseded load", "no session named"],
)
@pytest.mark.asyncio
async def test_direct_pcm_stream_plays_only_for_the_queue_current_load(
    requested_session: str | None, served: bool
) -> None:
    """A stream left over from a superseded load ends without audio and serves nothing."""
    controller = _pcm_stream_controller()
    cast("Any", controller.mass.player_queues.queue_data_or_none).return_value = SimpleNamespace(
        session_id="session-2"
    )
    media = _media()
    media.queue_session_id = requested_session

    stream = controller.get_stream(media, PCM_FORMAT)
    chunks = [chunk async for chunk in stream]

    assert chunks == ([b"chunk-1", b"chunk-2"] if served else [])
    mark_item_served = cast("MagicMock", controller.mass.player_queues.mark_item_served)
    assert mark_item_served.called is served


@pytest.mark.asyncio
async def test_direct_pcm_stream_ends_once_its_session_is_replaced() -> None:
    """A stream stops at the first chunk after the queue moved on to another session."""
    controller = _pcm_stream_controller()
    queue_data = SimpleNamespace(session_id="session-1")
    cast("Any", controller.mass.player_queues.queue_data_or_none).return_value = queue_data
    media = _media()
    media.queue_session_id = "session-1"

    stream = controller.get_stream(media, PCM_FORMAT)
    assert await anext(stream) == b"chunk-1"
    queue_data.session_id = "session-2"

    assert [chunk async for chunk in stream] == []


def _gapless_player(*, supports_enqueue: bool) -> MagicMock:
    """Build a player protocol that plays gapless and takes raw PCM without flow mode."""
    player = MagicMock()
    player.flow_mode = False
    player.supported_features = {PlayerFeature.GAPLESS_PLAYBACK}
    if supports_enqueue:
        player.supported_features.add(PlayerFeature.ENQUEUE)
    player.supports_feature.side_effect = player.supported_features.__contains__
    return player


@pytest.mark.asyncio
async def test_direct_pcm_stream_crossfades_on_a_gapless_player() -> None:
    """A gapless player gets the crossfade stream, which mixes the next item into the end."""
    controller = _pcm_stream_controller()
    cast("Any", controller.mass.players.get_output_protocol).return_value = _gapless_player(
        supports_enqueue=True
    )
    cast("Any", controller.mass.player_queues.get).return_value = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Player",
        available=True,
        items=2,
        crossfade_enabled=True,
    )
    cast("Any", controller.mass.config.get_raw_core_config_value).side_effect = (
        lambda _domain, _key, default: default
    )
    queue_item = _queue_item(0)
    assert queue_item.streamdetails is not None
    queue_item.streamdetails.media_type = MediaType.TRACK
    cast("Any", controller.mass.player_queues.get_item).return_value = queue_item
    audio = cast("Any", controller.audio)
    audio.fade_into_pending.return_value = False
    audio.get_queue_item_stream_with_smartfade.side_effect = lambda **_kwargs: _pcm_chunks(
        (b"mixed",)
    )

    stream = controller.get_stream(_media(), PCM_FORMAT, player_id=QUEUE_ID)

    assert [chunk async for chunk in stream] == [b"mixed"]
    smartfade_kwargs = audio.get_queue_item_stream_with_smartfade.call_args.kwargs
    assert smartfade_kwargs["crossfade_mode"] != CrossfadeMode.DISABLED
    audio.get_queue_item_stream.assert_not_called()


@pytest.mark.parametrize(
    ("supports_enqueue", "loaded"),
    [(True, True), (False, False)],
    ids=["enqueue player", "player without enqueue"],
)
@pytest.mark.asyncio
async def test_direct_pcm_stream_reports_the_item_loaded_to_an_enqueue_player(
    supports_enqueue: bool, loaded: bool
) -> None:
    """Only a player that takes the next item gets it lined up from the first chunk on."""
    controller = _pcm_stream_controller()
    cast("Any", controller.mass.players.get_output_protocol).return_value = _gapless_player(
        supports_enqueue=supports_enqueue
    )
    cast("Any", controller.audio).fade_into_pending.return_value = False
    player_queues = cast("Any", controller.mass.player_queues)

    stream = controller.get_stream(_media(), PCM_FORMAT, player_id=QUEUE_ID)
    assert await anext(stream) == b"chunk-1"
    assert await anext(stream) == b"chunk-2"
    await stream.aclose()

    if loaded:
        player_queues.track_loaded_in_buffer.assert_called_once_with(QUEUE_ID, QUEUE_ITEM_ID)
    else:
        player_queues.track_loaded_in_buffer.assert_not_called()
    assert player_queues.mark_item_served.called is not loaded
