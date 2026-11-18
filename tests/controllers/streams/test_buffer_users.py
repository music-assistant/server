"""
Tests for who uses an audio buffer and who holds a provider's stream slot.

Every party that waits for a buffer's audio or reads it is one of its users, for as long as
it does: the buffer keeps its source while it has one, and gives it up afterwards when its
queue no longer plays it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
from functools import partial
from types import MethodType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

import music_assistant.controllers.streams.audio as audio_mod
import music_assistant.controllers.streams.controller as controller_mod
from music_assistant.controllers.streams.audio_buffer import AudioBuffer, BufferUser
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.models.music_provider import ProviderStreamLimitError
from tests.controllers.streams.stream_slot_rig import (
    OTHER_QUEUE_ID,
    PCM_FORMAT,
    QUEUE_ID,
    SESSION_ID,
    SingleStreamProvider,
    SlotRig,
    TwoStreamProvider,
    UnlimitedProvider,
    drain,
    install_mixer,
    source,
)

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

PLAYER_ID = "player1"
ENDINGS = ("normal end", "disconnect", "error", "cancellation")


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SlotRig]:
    """Build the rig with every music source open to the queue's playback."""
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    rig = SlotRig(getattr(request, "param", SingleStreamProvider))
    yield rig
    await rig.close()


def _track_source(ending: str, gate: asyncio.Event) -> partial[AsyncGenerator[bytes]]:
    """
    Return a source that delivers three seconds and then waits for the way the test ends.

    :param ending: How the consumer of the track ends in this test.
    :param gate: Opened by the test when the source may go on.
    """
    return partial(source, 3, 2, gate, fails=ending == "error")


async def _end(stream: AsyncGenerator[bytes], ending: str, gate: asyncio.Event) -> None:
    """
    End a stream that has delivered its first audio, in the given way.

    :param stream: The stream, suspended after one of its chunks.
    :param ending: One of ENDINGS.
    :param gate: The gate the source of the stream waits at.
    """
    if ending == "normal end":
        gate.set()
        await drain(stream)
    elif ending == "disconnect":
        await stream.aclose()
    elif ending == "error":
        gate.set()
        with suppress(AudioError):
            await drain(stream)
    else:
        reading = asyncio.ensure_future(drain(stream))
        for _ in range(5):
            await asyncio.sleep(0)
        reading.cancel()
        with suppress(asyncio.CancelledError):
            await reading


# --- the buffer and its users


async def test_a_wait_for_the_first_audio_counts_as_a_user(rig: SlotRig) -> None:
    """Whoever waits for a buffer to become ready uses it, and no longer once the wait ends."""
    playing, _upcoming = rig.play("track 0", "track 1")
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(source, 0, 3, gate)

    waiting = asyncio.ensure_future(rig.capture(playing))
    await asyncio.sleep(0.05)
    buffer = rig.buffer(playing)
    assert list(buffer.users) == ["prepare_next"]

    gate.set()
    assert await waiting is buffer
    assert list(buffer.users) == []


async def test_a_wait_that_is_given_up_lets_go_of_the_buffer(rig: SlotRig) -> None:
    """A wait that is cancelled leaves no user behind."""
    playing, _upcoming = rig.play("track 0", "track 1")
    rig.sources["track 0"] = partial(source, 0, 3, asyncio.Event())

    waiting = asyncio.ensure_future(rig.capture(playing))
    await asyncio.sleep(0.05)
    buffer = rig.buffer(playing)
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting

    assert list(buffer.users) == []


@pytest.mark.parametrize("ending", ENDINGS)
async def test_an_open_read_stream_counts_as_a_user_until_it_ends(
    rig: SlotRig, ending: str
) -> None:
    """A read stream uses the buffer from its first read until it ends, whatever ends it."""
    playing, _upcoming = rig.play("track 0", "track 1")
    gate = asyncio.Event()
    rig.sources["track 0"] = _track_source(ending, gate)
    buffer = await rig.capture(playing)
    stream = buffer.get_stream(PCM_FORMAT)
    assert list(buffer.users) == []

    await anext(stream)
    assert list(buffer.users) == ["reader"]
    await _end(stream, ending, gate)

    assert list(buffer.users) == []


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_user_moves_along_to_the_buffer_that_takes_over(rig: SlotRig) -> None:
    """A user that attaches to another buffer no longer uses the one before."""
    first, second = rig.play("track 0", "track 1")
    first_buffer = await rig.capture(first)
    second_buffer = await rig.capture(second)
    user = BufferUser("request")

    user.attach(first_buffer)
    user.attach(first_buffer)
    assert list(first_buffer.users) == ["request"]
    user.attach(second_buffer)
    assert list(first_buffer.users) == []
    assert list(second_buffer.users) == ["request"]
    user.release()
    user.release()

    assert list(second_buffer.users) == []


async def test_a_buffer_gives_its_source_up_only_when_nothing_needs_it(rig: SlotRig) -> None:
    """The source stays while the buffer has a user or may not release, and goes otherwise."""
    playing, _upcoming = rig.play("track 0", "track 1")
    buffer = await rig.capture(playing)
    user = BufferUser("request")
    user.attach(buffer)

    buffer.may_release = lambda _buffer: True
    assert buffer.release_if_unneeded() is None
    buffer.may_release = lambda _buffer: False
    user.release()
    assert buffer.release_if_unneeded() is None
    buffer.may_release = None
    assert buffer.release_if_unneeded() is None
    assert rig.fills(buffer)

    buffer.may_release = lambda _buffer: True
    release = buffer.release_if_unneeded()
    assert release is not None
    # a source that is being stopped is not stopped a second time
    assert buffer.release_if_unneeded() is release
    await release

    assert not rig.fills(buffer)
    assert buffer.release_if_unneeded() is None


async def test_a_source_that_delivered_everything_gives_its_audio_up_too(rig: SlotRig) -> None:
    """A buffer whose source is done frees its audio like any other once nothing needs it."""
    playing, _upcoming = rig.play("track 0", "track 1", realtime=False)
    rig.sources["track 0"] = partial(source, 3)
    await rig.capture(playing)
    buffer = await rig.delivered(playing)
    assert buffer.seconds_available == 3
    buffer.may_release = lambda _buffer: True

    release = buffer.release_if_unneeded()

    assert release is not None
    assert buffer.cancelled
    await release
    assert buffer.seconds_available == 0


# --- the queue's side of the rule


@pytest.mark.parametrize(
    ("current_index", "index_in_buffer", "plays"),
    [
        (0, 0, [True, True, False, False]),
        # the player fetched the next track while the current one still plays
        (0, 1, [True, True, True, False]),
        # a flow stream runs further ahead of what the player plays
        (0, 2, [True, True, True, True]),
        (3, 3, [False, False, False, True]),
        (None, 1, [False, True, True, False]),
    ],
)
async def test_a_queue_plays_now_or_next_what_it_is_on_and_what_follows(
    rig: SlotRig, current_index: int | None, index_in_buffer: int, plays: list[bool]
) -> None:
    """The current item, the item the player fetched, and what follows either, play soon."""
    items = rig.play("track 0", "track 1", "track 2", "track 3")
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.current_index = current_index
    queue.index_in_buffer = index_in_buffer

    assert [rig.queues.plays_now_or_next(item) for item in items] == plays


async def test_a_queue_plays_next_what_its_repeat_and_its_available_items_say(
    rig: SlotRig,
) -> None:
    """What plays next follows the repeat mode and passes over unavailable items."""
    first, second, third, last = rig.play("track 0", "track 1", "track 2", "track 3", current=3)
    queue = rig.queues._queue_data[QUEUE_ID].queue

    assert not rig.queues.plays_now_or_next(first)
    queue.repeat_mode = RepeatMode.ALL
    assert rig.queues.plays_now_or_next(first)
    queue.repeat_mode = RepeatMode.ONE
    assert rig.queues.plays_now_or_next(last)
    assert not rig.queues.plays_now_or_next(first)

    queue.repeat_mode = RepeatMode.OFF
    queue.current_index = queue.index_in_buffer = 0
    assert rig.queues.plays_now_or_next(second)
    assert not rig.queues.plays_now_or_next(third)
    second.available = False
    assert not rig.queues.plays_now_or_next(second)
    assert rig.queues.plays_now_or_next(third)


async def test_a_queue_plays_neither_now_nor_next_what_is_not_on_it(rig: SlotRig) -> None:
    """Nothing that left the queue, or that belongs to a queue the rig does not know, plays."""
    playing, upcoming = rig.play("track 0", "track 1")
    stranger = rig.track("track 5")
    elsewhere = rig.track("track 6", queue_id=OTHER_QUEUE_ID)

    assert rig.queues.plays_now_or_next(playing)
    assert rig.queues.plays_now_or_next(upcoming)
    assert not rig.queues.plays_now_or_next(stranger)
    assert not rig.queues.plays_now_or_next(elsewhere)


async def test_a_queue_without_a_playback_session_plays_nothing(rig: SlotRig) -> None:
    """A queue that has no playback session plays no item, not the one it is on either."""
    first, second = rig.play("track 0", "track 1")
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.session_id = None

    assert not rig.queues.plays_now_or_next(first)
    assert not rig.queues.plays_now_or_next(second)
    queue_data.queue.current_index = 1
    assert not rig.queues.plays_now_or_next(first)
    assert not rig.queues.plays_now_or_next(second)


def _may_release_in_every_state(rig: SlotRig, capture: AudioBuffer) -> dict[str, bool]:
    """
    Ask the rule about the capture of a queue's second track in every state its item can be in.

    :param rig: The rig, whose queue holds three tracks and is on the first.
    :param capture: The capture of the second track, which has no user.
    :return: Whether the capture may give its source up, by state.
    """
    queue_data = rig.queues._queue_data[QUEUE_ID]
    playing, prepared, later = queue_data.items
    details = prepared.streamdetails
    may_release = capture.may_release
    assert details is not None
    assert may_release is not None
    verdicts = {"it plays next": may_release(capture)}
    queue_data.items = [playing, later, prepared]
    verdicts["it waits further down"] = may_release(capture)
    queue_data.items = [playing, prepared, later]
    queue_data.session_id = None
    verdicts["its queue is not playing, and is on another item"] = may_release(capture)
    queue_data.queue.current_index = 1
    verdicts["its queue is not playing, and is on it"] = may_release(capture)
    queue_data.queue.current_index = 0
    queue_data.session_id = SESSION_ID
    queue_data.items = [playing, later]
    verdicts["its item left the queue"] = may_release(capture)
    queue_data.items = [playing, prepared, later]
    del rig.queues._queue_data[QUEUE_ID]
    verdicts["its queue is gone"] = may_release(capture)
    rig.queues._queue_data[QUEUE_ID] = queue_data
    details.buffer = MagicMock(spec=AudioBuffer)
    verdicts["another buffer took its place"] = may_release(capture)
    details.buffer = capture
    prepared.streamdetails = None
    verdicts["its item lost its stream details"] = may_release(capture)
    prepared.streamdetails = details
    return verdicts


@pytest.mark.parametrize(
    "rig",
    [SingleStreamProvider, TwoStreamProvider, UnlimitedProvider],
    ids=["one slot", "a slot to spare", "no limit"],
    indirect=True,
)
async def test_a_buffer_nobody_reads_is_kept_only_while_its_queue_plays_it_now_or_next(
    rig: SlotRig,
) -> None:
    """On every provider a buffer without a reader may go unless its queue plays it now or next."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)

    assert _may_release_in_every_state(rig, capture) == {
        "it plays next": False,
        "it waits further down": True,
        "its queue is not playing, and is on another item": True,
        "its queue is not playing, and is on it": True,
        "its item left the queue": True,
        "its queue is gone": True,
        "another buffer took its place": True,
        "its item lost its stream details": True,
    }


async def test_a_claim_holds_its_item_for_the_playback_it_was_made_in_only(rig: SlotRig) -> None:
    """A stream's claim keeps a track in the stream's own playback session, and in no other."""
    playing, prepared, later = rig.play("track 0", "track 1", "track 2")
    queue_data = rig.queues._queue_data[QUEUE_ID]
    capture = await rig.prepare_next(playing)
    may_release = capture.may_release
    assert may_release is not None
    queue_data.items = [playing, later, prepared]
    assert may_release(capture)

    # a stream of this playback decided to play the track
    claim = audio_mod._ItemClaim("stream", SESSION_ID)
    rig.audio._claim_item(claim, prepared)
    assert not may_release(capture)
    # playback started over on the queue since
    queue_data.session_id = "session-after"
    assert may_release(capture)
    # the queue stopped, and a stream that starts while nothing plays there holds nothing
    queue_data.session_id = None
    assert may_release(capture)
    idle_claim = audio_mod._ItemClaim("idle stream", None)
    rig.audio._claim_item(idle_claim, prepared)
    assert may_release(capture)
    queue_data.session_id = SESSION_ID
    rig.audio._claim_item(idle_claim, None)
    assert not may_release(capture)
    # the track moved to the queue of another player, which plays in a session of its own
    rig.play("track 7", "track 8", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    rig.queues._queue_data[OTHER_QUEUE_ID].items.append(prepared)
    prepared.queue_id = OTHER_QUEUE_ID
    assert may_release(capture)
    # a stream of that playback holds it there
    claim_there = audio_mod._ItemClaim("stream there", "session-b")
    rig.audio._claim_item(claim_there, prepared)
    assert not may_release(capture)

    # the track gives its source up when the claim that held it ends
    rig.audio._claim_item(claim, None)
    await rig.settle()
    assert rig.fills(capture)
    rig.audio._claim_item(claim_there, None)
    await rig.settle()
    assert not rig.fills(capture)
    assert rig.audio._item_claims == set()


async def test_a_request_that_took_the_source_keeps_it_until_the_request_ends(
    rig: SlotRig,
) -> None:
    """A request that has the source and has not read from it yet is one of its users."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    request = BufferUser("request")
    assert await rig.audio.get_audio_buffer(prepared, reason="streaming", user=request) is capture
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.REPLACE_NEXT)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(new_next, wait=0.2)
    assert rig.fills(capture)

    request.release()
    await rig.settle()

    assert not rig.fills(capture)
    assert rig.slot_free()


async def test_a_queue_that_stopped_keeps_no_capture_for_a_late_preparation(rig: SlotRig) -> None:
    """A capture whose queue has no playback session any more gives way when its user leaves."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    late_user = BufferUser("late preparation")
    late_user.attach(capture)

    rig.queues._queue_data[QUEUE_ID].session_id = None
    late_user.release()
    await rig.settle()

    assert not rig.fills(capture)
    assert rig.buffer(prepared) is capture


# --- the holders of a provider's stream slots


async def test_a_provider_lists_the_buffers_that_hold_its_slots(rig: SlotRig) -> None:
    """A buffer is a holder from the moment its source has the slot until it gives it back."""
    playing, upcoming = rig.play("track 0", "track 1")
    assert list(rig.provider.stream_slot_holders) == []

    capture = await rig.capture(playing)
    assert list(rig.provider.stream_slot_holders) == [capture]
    # a source that waits for the slot does not hold it
    waiting = asyncio.ensure_future(rig.ask(upcoming, wait=0.2))
    await asyncio.sleep(0.05)
    assert list(rig.provider.stream_slot_holders) == [capture]
    with pytest.raises(AudioError):
        await waiting

    await capture.clear()
    assert list(rig.provider.stream_slot_holders) == []
    assert rig.slot_free()


async def test_a_slot_taken_without_a_buffer_has_no_holder(rig: SlotRig) -> None:
    """Whoever takes a slot for something else than a buffer is not listed."""
    async with rig.provider.acquire_stream_slot(0):
        assert not rig.slot_free()
        assert list(rig.provider.stream_slot_holders) == []
    assert rig.slot_free()


@pytest.mark.parametrize("rig", [UnlimitedProvider], indirect=True)
async def test_a_provider_without_a_stream_limit_has_no_holders(rig: SlotRig) -> None:
    """Without a limit there are no slots to hold."""
    playing, _upcoming = rig.play("track 0", "track 1")

    capture = await rig.capture(playing)

    assert rig.fills(capture)
    assert list(rig.provider.stream_slot_holders) == []


# --- every kind of consumer is a user while it reads, and no longer once it ended


@pytest.mark.parametrize("ending", ENDINGS)
async def test_a_speakers_request_uses_the_buffer_until_the_request_ends(
    rig: SlotRig, ending: str
) -> None:
    """The stream route of a speaker uses the item's buffer for as long as the request runs."""
    playing, _upcoming = rig.play("track 0", "track 1")
    gate = asyncio.Event()
    rig.sources["track 0"] = _track_source(ending, gate)
    request, connection = _start_request(rig, playing)

    # the first chunk reaches the speaker, the second one is on its way
    await connection.next_write()
    connection.deliver()
    await connection.next_write()
    buffer = rig.buffer(playing)
    assert list(buffer.users) == ["stream of track 0", "reader"]

    if ending in ("normal end", "error"):
        gate.set()
        connection.deliver_all()
    elif ending == "disconnect":
        connection.drop()
    else:
        request.cancel()
    with suppress(asyncio.CancelledError):
        await request

    assert list(buffer.users) == []
    assert rig.audio._item_claims == set()


async def _request_in_setup(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch, lookup_fails_with: Exception | None = None
) -> tuple[asyncio.Task[web.StreamResponse], _Connection, AudioBuffer, asyncio.Event]:
    """
    Let a speaker ask for the prepared next track and hold its request before it streams.

    The request is accepted and waits for the content length of its response, while a queue
    change makes its track no longer the next one.

    :param rig: The rig whose queue and audio the route serves.
    :param monkeypatch: The test's monkeypatch.
    :param lookup_fails_with: What the content length lookup raises once it may go on.
    :return: The request, its connection, the track's prepared audio and the event that
        lets the request go on.
    """
    setting_up = asyncio.Event()
    go_on = asyncio.Event()

    async def _content_length(*_args: object) -> int:
        setting_up.set()
        await go_on.wait()
        if lookup_fails_with is not None:
            raise lookup_fails_with
        return 48_000

    monkeypatch.setattr(controller_mod, "get_content_length", _content_length)
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    rig.sources["track 1"] = partial(source, 3)
    capture = await rig.prepare_next(playing)
    await rig.delivered(prepared)
    request, connection = _start_request(rig, prepared)
    # this speaker wants a content length, which the route looks up before it streams
    rig.mass.players.get_output_protocol.return_value.get_config_value.return_value = True
    await asyncio.wait_for(setting_up.wait(), 1)
    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], QueueOption.NEXT)
    await rig.settle()
    return request, connection, capture, go_on


@pytest.mark.parametrize(
    "competing", [False, True], ids=["nobody else asks", "the new next track takes the slot"]
)
async def test_a_track_a_speaker_asked_for_keeps_its_audio_while_its_request_is_set_up(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch, competing: bool
) -> None:
    """A queue change while a speaker's request is set up leaves the track it asked for alone."""
    request, connection, capture, go_on = await _request_in_setup(rig, monkeypatch)
    try:
        assert not capture.cancelled
        if competing:
            playing = rig.queues._queue_data[QUEUE_ID].items[0]
            await rig.prepare_next(playing)
            assert not rig.slot_free()
        go_on.set()
        connection.deliver_all()
        await asyncio.wait_for(request, 5)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    # the speaker got the audio that was prepared for it: its source was not opened again
    assert rig.opened["track 1"] == 1
    assert connection.writer.write.await_count > 0
    assert rig.audio._item_claims == set()


@pytest.mark.parametrize("ending", ["cancelled", "refused", "failed", "client gone"])
async def test_a_request_that_ends_before_it_streams_lets_go_of_its_track(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """However a request ends before its first audio, its track is no longer kept for it."""
    failure = {
        "refused": web.HTTPNotFound(reason="No longer there"),
        "failed": RuntimeError("the lookup broke"),
    }.get(ending)
    request, connection, capture, go_on = await _request_in_setup(rig, monkeypatch, failure)
    try:
        assert capture.is_valid()
        if ending == "cancelled":
            request.cancel()
        elif ending == "client gone":
            connection.drop()
        go_on.set()
        await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 5)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    assert rig.audio._item_claims == set()
    # nothing else needs the track any more, so its audio goes with the request
    assert capture.cancelled
    assert rig.opened["track 1"] == 1


@pytest.mark.parametrize("ending", ENDINGS)
async def test_a_direct_pcm_consumer_uses_the_buffer_until_its_stream_ends(
    rig: SlotRig, ending: str
) -> None:
    """A player provider that takes the raw PCM stream of an item uses the item's buffer."""
    playing, _upcoming = rig.play("track 0", "track 1")
    gate = asyncio.Event()
    rig.sources["track 0"] = _track_source(ending, gate)
    stream = _direct_pcm_stream(rig, playing)

    await anext(stream)
    buffer = rig.buffer(playing)
    assert list(buffer.users) == ["stream of track 0", "reader"]
    await _end(stream, ending, gate)

    assert list(buffer.users) == []
    assert rig.audio._item_claims == set()


@pytest.mark.parametrize("ending", ENDINGS)
async def test_a_flow_stream_uses_the_buffer_of_the_track_it_plays_until_it_ends(
    rig: SlotRig, ending: str
) -> None:
    """A flow stream claims the track it selected and reads it, until the stream ends."""
    (playing,) = rig.play("track 0")
    queue = rig.queues._queue_data[QUEUE_ID].queue
    rig.mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    gate = asyncio.Event()
    rig.sources["track 0"] = _track_source(ending, gate)
    # the start of the queue prepared the track before the player asked for the stream
    buffer = await rig.capture(playing)
    stream = rig.audio.get_queue_flow_stream(queue, playing, PCM_FORMAT, SESSION_ID)

    await anext(stream)
    assert list(buffer.users) == ["stream of track 0", "reader"]
    assert rig.audio._users_of(buffer) == [
        "stream of track 0",
        "reader",
        "flow stream of Queue A (claims it)",
    ]
    await _end(stream, ending, gate)

    assert list(buffer.users) == []
    assert rig.audio._item_claims == set()


@pytest.mark.parametrize("ending", ENDINGS)
async def test_a_flow_prefetch_uses_the_buffer_of_the_next_track_until_it_ends(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """The fade-in a flow stream collects ahead of time uses the next track's buffer."""
    playing, upcoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    install_mixer(rig, monkeypatch)
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(source, 12)
    rig.sources["track 1"] = _track_source(ending, gate)
    await rig.capture(playing)
    await rig.delivered(playing)
    buffer = await rig.capture(upcoming)
    stream = rig.audio.get_queue_flow_stream(queue, playing, PCM_FORMAT, SESSION_ID)

    # the tail of the playing track is held back, and the next track is collected meanwhile
    await anext(stream)
    await asyncio.sleep(0)
    assert rig.queues._queue_data[QUEUE_ID].queue.index_in_buffer == 0
    assert list(buffer.users) == ["stream of track 1", "reader"]
    await _end(stream, ending, gate)

    assert list(buffer.users) == []
    assert rig.audio._item_claims == set()


@pytest.mark.parametrize("ending", ENDINGS)
async def test_a_fade_outside_flow_mode_uses_the_buffer_of_the_next_track_until_it_ends(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """The stream of the playing track mixes the next track's start, as a user of its buffer."""
    playing, upcoming = rig.play("track 0", "track 1", realtime=False)
    # the mix hands two seconds of the next track to the playing track's stream
    install_mixer(rig, monkeypatch, crossfade_duration=10.0)
    rig.audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(source, 12)
    rig.sources["track 1"] = _track_source(ending, gate)
    # all of the playing track has arrived, so its stream holds its tail back for the fade
    await rig.capture(playing)
    await rig.delivered(playing)
    playing_stream = _fading_stream(rig, playing)

    if ending == "disconnect":
        # the speaker hangs up on the playing track in the middle of the mix
        async for _chunk in playing_stream:
            if (details := upcoming.streamdetails) and details.buffer:
                if "reader" in details.buffer.users:
                    break
        buffer = rig.buffer(upcoming)
        assert list(buffer.users) == ["stream of track 1", "reader"]
        await playing_stream.aclose()
    else:
        # the playing track's stream ends, and the rest of the mix waits for the next track's
        await drain(playing_stream)
        buffer = rig.buffer(upcoming)
        assert rig.queues._queue_data[QUEUE_ID].queue.index_in_buffer == 1
        assert list(buffer.users) == ["stream of track 1", "reader"]
        await _end(_fading_stream(rig, upcoming), ending, gate)

    assert list(buffer.users) == []
    assert rig.audio._item_claims == set()


class _Connection:
    """A speaker's connection whose writes the test lets through or breaks off."""

    def __init__(self) -> None:
        """Initialize a connection without any write waiting yet."""
        self._pending: asyncio.Queue[None] = asyncio.Queue()
        self._verdicts: asyncio.Queue[bool] = asyncio.Queue()
        self._deliver_all = False
        self.protocol = Mock()
        self.writer = Mock()
        self.writer.write_headers = AsyncMock()
        self.writer.write = AsyncMock(side_effect=self._write)
        self.writer.write_eof = AsyncMock()
        self.writer.drain = AsyncMock()

    async def next_write(self) -> None:
        """Wait until the response tries to write its next chunk to the speaker."""
        await asyncio.wait_for(self._pending.get(), 1)

    def deliver(self) -> None:
        """Let the waiting write reach the speaker."""
        self._verdicts.put_nowait(True)

    def deliver_all(self) -> None:
        """Let the waiting write and every later one reach the speaker."""
        self._deliver_all = True
        self.deliver()

    def drop(self) -> None:
        """Break the connection off, as a speaker does when it gives up on the track."""
        # what aiohttp does as soon as it reads the speaker's close
        self.protocol.transport = None
        self._verdicts.put_nowait(False)

    async def _write(self, _data: bytes) -> None:
        """Hold a write until the test decides whether the speaker still reads."""
        if self._deliver_all:
            return
        self._pending.put_nowait(None)
        if not await self._verdicts.get():
            raise ConnectionResetError("Cannot write to closing transport")


@pytest.fixture(autouse=True)
def _passthrough_encoder() -> Iterator[None]:
    """Hand the item audio to the speaker as it comes, without an encoder in between."""
    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
        side_effect=lambda audio_input, **_kwargs: audio_input,
    ):
        yield


def _start_request(
    rig: SlotRig, queue_item: QueueItem
) -> tuple[asyncio.Task[web.StreamResponse], _Connection]:
    """
    Let a speaker request the audio of a queue item from the real stream route.

    :param rig: The rig whose queue and audio the route serves.
    :param queue_item: The queue item the speaker asks for.
    """
    player = MagicMock()
    player.stop_called = False
    player.needs_gapless_burst = False
    player.get_config_value.return_value = False
    rig.mass.players.get_output_protocol.return_value = player
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    rig.audio.get_output_format = AsyncMock(  # type: ignore[method-assign]
        return_value=AudioFormat(content_type=ContentType.FLAC, sample_rate=8000)
    )
    rig.audio.get_player_output_plan = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(filter_params=[])
    )
    ctrl = MagicMock()
    ctrl.logger = Mock()
    ctrl._log_request = Mock()
    ctrl._open_item_streams = {}
    ctrl._ended_item_streams = {}
    ctrl._active_output_streams = 0
    # bind the real helpers: the mocked self would silently swallow them otherwise
    for helper in ("_requested_item_ids", "_ended_item_ids", "_release_abandoned_item_sources"):
        setattr(ctrl, helper, MethodType(getattr(StreamsController, helper), ctrl))
    ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    ctrl.mass = rig.mass
    ctrl.audio = rig.audio
    connection = _Connection()
    request = make_mocked_request(
        "GET",
        f"/single/{SESSION_ID}/{QUEUE_ID}/{queue_item.queue_item_id}/{PLAYER_ID}.flac",
        match_info={
            "session_id": SESSION_ID,
            "queue_id": QUEUE_ID,
            "queue_item_id": queue_item.queue_item_id,
            "player_id": PLAYER_ID,
            "fmt": "flac",
        },
        writer=connection.writer,
        protocol=connection.protocol,
    )
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(ctrl, request))
    return task, connection


def _direct_pcm_stream(rig: SlotRig, queue_item: QueueItem) -> AsyncGenerator[bytes]:
    """
    Take the raw PCM stream of a queue item, as a player provider without HTTP does.

    :param rig: The rig whose queue and audio the stream comes from.
    :param queue_item: The queue item to stream.
    """
    protocol = MagicMock()
    protocol.flow_mode = False
    rig.mass.players.get_output_protocol.return_value = protocol
    # the queue's player is no group that shares one stream with its members
    rig.mass.players.get_player.return_value.own_protocol.get_shared_stream.return_value = None
    ctrl: Any = StreamsController.__new__(StreamsController)
    ctrl.mass = rig.mass
    ctrl.audio = rig.audio
    ctrl.logger = Mock()
    ctrl._active_output_streams = 0
    ctrl._flow_mode_owners = {}
    ctrl._boundary_mix_owners = {}
    ctrl._get_live_audio_source_session = Mock(return_value=None)
    ctrl._update_audio_processing_context = Mock()
    media = PlayerMedia(
        uri=queue_item.uri,
        media_type=MediaType.TRACK,
        source_id=QUEUE_ID,
        queue_item_id=queue_item.queue_item_id,
        queue_session_id=SESSION_ID,
    )
    stream: AsyncGenerator[bytes] = ctrl.get_stream(media, PCM_FORMAT, PLAYER_ID)
    return stream


def _fading_stream(rig: SlotRig, queue_item: QueueItem) -> AsyncGenerator[bytes]:
    """
    Return the stream a gapless speaker gets for an item of a queue that crossfades.

    :param rig: The rig whose queue crossfades.
    :param queue_item: The queue item to stream.
    """
    return rig.audio.get_queue_item_stream_with_smartfade(
        MagicMock(), queue_item, PCM_FORMAT, CrossfadeMode.STANDARD_CROSSFADE, 8, SESSION_ID
    )
