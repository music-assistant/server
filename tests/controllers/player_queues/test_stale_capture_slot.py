"""
Tests for the stream slot of a music service after the queue changed under a prepared track.

The server prepares the queue's next track ahead of time. For a source that delivers its audio
at playback pace, that capture holds one of the service's stream slots until the track has
played out. When the queue changes so that the prepared track is no longer the one that plays
next, the track that does play next needs that slot: a source nobody uses and no queue is
about to play gives it up, a source that is in use or still plays next keeps it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import CrossfadeMode, QueueOption, RepeatMode

import music_assistant.controllers.streams.audio as audio_mod
import music_assistant.models.music_provider as music_provider_mod
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
    slow_to_stop_capture,
    source,
)

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SlotRig]:
    """Build the rig with every music source open to the queue's playback."""
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    rig = SlotRig(getattr(request, "param", SingleStreamProvider))
    yield rig
    await rig.close()


# --- the track that plays next after a queue change gets the slot


@pytest.mark.parametrize(
    "option",
    [QueueOption.REPLACE_NEXT, QueueOption.NEXT, QueueOption.REPLACE],
    ids=["replace_next", "next", "replace"],
)
async def test_the_track_that_plays_next_after_a_queue_change_gets_the_slot(
    rig: SlotRig, option: QueueOption
) -> None:
    """The prepared track that no longer plays next gives its capture up for the one that does."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)
    assert not rig.slot_free()
    new_next, new_after = rig.track("track 5"), rig.track("track 6")
    # a replace starts the new content itself; only what the content change leaves behind counts
    rig.queues.play_index = AsyncMock()  # type: ignore[method-assign]

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next, new_after], option)
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(stale_capture)


@pytest.mark.parametrize("edit", ["delete", "move"])
async def test_the_track_after_a_removed_or_moved_next_track_gets_the_slot(
    rig: SlotRig, edit: str
) -> None:
    """Taking the prepared track off the queue, or moving it back, hands its slot over."""
    playing, prepared, following = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)

    if edit == "delete":
        rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    else:
        rig.queues.move_item(QUEUE_ID, prepared.queue_item_id, pos_shift=1)
    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is following
    buffer = await rig.ask(following)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(stale_capture)


async def test_the_preparation_of_the_new_next_track_gets_the_slot(rig: SlotRig) -> None:
    """The server's own preparation of the track that now plays next takes the slot over too."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    buffer = await rig.prepare_next(playing)

    assert buffer is rig.buffer(new_next)
    assert rig.fills(buffer)
    assert not rig.fills(stale_capture)


async def test_the_track_after_the_wrap_gives_its_slot_to_a_track_played_next(
    rig: SlotRig,
) -> None:
    """With repeat on, the first track prepared to follow the last one makes way for play next."""
    first, _second, _third, playing = rig.play(
        "track 0", "track 1", "track 2", "track 3", current=3, repeat=RepeatMode.ALL
    )
    prepared_capture = await rig.prepare_next(playing)
    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is first
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(prepared_capture)


async def test_a_track_prepared_after_it_stopped_being_next_does_not_keep_the_slot(
    rig: SlotRig,
) -> None:
    """A "play next" that lands while the preparation fetches details leaves no capture behind."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    details_fetched = asyncio.Event()

    async def _slow_load(queue_id: str, current_item_id: str) -> QueueItem:
        item = await rig._load_next(queue_id, current_item_id)
        await details_fetched.wait()
        return item

    rig.queues.load_next_queue_item = AsyncMock(side_effect=_slow_load)  # type: ignore[method-assign]
    rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    await asyncio.sleep(0)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    details_fetched.set()
    await rig.settle()

    assert prepared.streamdetails is not None
    assert prepared.streamdetails.buffer is None
    assert rig.opened["track 1"] == 0
    assert rig.slot_free()


# --- a source that is in use keeps its slot until its last user leaves


@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_a_track_a_speaker_still_streams_keeps_its_source_until_that_stream_ends(
    rig: SlotRig, option: QueueOption
) -> None:
    """A speaker that asked for the prepared track before the change keeps receiving it."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], option)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(new_next, wait=0.2)
    assert rig.fills(capture)

    # the speaker lets go of the track, in whatever way
    await stream.aclose()
    await rig.settle()

    assert not rig.fills(capture)
    assert rig.slot_free()


async def test_a_track_that_waits_for_the_slot_gets_it_when_the_stale_stream_ends(
    rig: SlotRig,
) -> None:
    """The new next track, already waiting, gets the slot the moment the old stream ends."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)
    new_next = rig.track("track 5")
    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.REPLACE_NEXT)

    waiting = asyncio.ensure_future(rig.ask(new_next, wait=5))
    await asyncio.sleep(0.2)
    assert not waiting.done()
    await stream.aclose()
    buffer = await waiting

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(capture)


async def test_a_request_that_ended_before_it_took_the_source_leaves_nothing_behind(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed track whose request returned without audio does not keep the slot."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    access_resolved = asyncio.Event()
    connected = True

    async def _slow_sources(*_args: object) -> tuple[None, list[str]]:
        await access_resolved.wait()
        return None, []

    # the speaker asks for the prepared track, and its request is held up before it has the
    # source
    monkeypatch.setattr(audio_mod, "playback_sources", _slow_sources)
    request = asyncio.ensure_future(
        drain(
            rig.audio.get_queue_item_stream(
                prepared, PCM_FORMAT, consumer_connected=lambda: connected
            )
        )
    )
    await asyncio.sleep(0)
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    new_next = rig.track("track 5")
    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.REPLACE_NEXT)
    connected = False
    access_resolved.set()

    assert await request == 0
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert not rig.fills(capture)
    assert list(capture.users) == []


async def test_a_request_under_way_is_not_handed_a_source_that_is_being_released(
    rig: SlotRig,
) -> None:
    """
    Nobody takes a source from the moment it is told to give its slot up.

    The track that now plays next takes the slot of the prepared track. While that source still
    unwinds, a request for the prepared track that was already under way arrives: it waits for
    the release and then opens a source of its own.
    """
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    stopped = asyncio.Event()
    rig.sources["track 1"] = lambda: (
        slow_to_stop_capture(stopped) if rig.opened["track 1"] == 1 else source(3)
    )
    capture = await rig.prepare_next(playing)
    new_next = rig.track("track 5")
    rig.sources["track 5"] = partial(source, 3)
    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)

    next_request = asyncio.ensure_future(rig.ask(new_next))
    await asyncio.sleep(0.05)
    stale_stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    stale_request = asyncio.ensure_future(anext(stale_stream))
    await asyncio.sleep(0.05)

    # the source still unwinds, and it is nobody's any more
    assert not rig.slot_free()
    assert not stale_request.done()
    assert list(capture.users) == []

    stopped.set()
    assert await next_request is not None
    assert len(await stale_request) == PCM_FORMAT.pcm_sample_size
    await stale_stream.aclose()

    assert capture.cancelled
    assert rig.buffer(prepared) is not capture
    assert rig.opened["track 1"] == 2


async def test_a_request_that_waits_for_the_preparation_keeps_the_track_it_asked_for(
    rig: SlotRig,
) -> None:
    """
    A speaker's request holds its track from the moment it arrives.

    The speaker asks for the next track while its preparation still waits for the first audio.
    A "play next" lands, so the track is no longer next by the time its audio is there.
    """
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    first_audio = asyncio.Event()
    rig.sources["track 1"] = partial(source, 0, 3, first_audio)
    rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    assert prepared.streamdetails is not None
    while prepared.streamdetails.buffer is None:
        await asyncio.sleep(0)
    request = asyncio.ensure_future(drain(rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)))
    await asyncio.sleep(0.05)

    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], QueueOption.NEXT)
    first_audio.set()

    # all of the track, from the source the preparation opened
    assert await request == 3 * PCM_FORMAT.pcm_sample_size
    assert rig.opened["track 1"] == 1


# --- a flow stream keeps the track it selected


async def test_a_track_the_flow_stream_took_plays_in_full_when_another_track_needs_its_slot(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A track the flow stream took to play next keeps its source through a change of the queue.

    The change lands while the flow stream builds its crossfade into that track, before any of
    the track's audio is out, and the track that now plays next asks for the slot.
    """
    outgoing, incoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(source, 30)
    rig.sources["track 1"] = partial(source, 20, 10, gate)
    new_next = rig.track("track 5", realtime=False)

    async def _play_next_while_the_fade_is_built() -> None:
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
        assert rig.queues.get_next_item(QUEUE_ID, outgoing.queue_item_id) is new_next
        with pytest.raises(ProviderStreamLimitError):
            await rig.ask(new_next, wait=0.2)
        gate.set()

    install_mixer(rig, monkeypatch, _play_next_while_the_fade_is_built)
    emitted = await drain(rig.audio.get_queue_flow_stream(queue, outgoing, PCM_FORMAT, SESSION_ID))

    # all of both tracks, 30 seconds each
    assert emitted == 60 * PCM_FORMAT.pcm_sample_size
    assert not rig.buffer(incoming).cancelled


async def test_a_track_the_flow_stream_selected_before_it_was_prepared_is_opened_once(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A flow stream's claim on its next track also holds a buffer that is created afterwards.

    The flow stream selects its next track before that track is prepared, and plays the tail of
    the playing track out while the preparation waits for audio. A "play next" lands in between,
    so the selected track is no longer next when its audio arrives.
    """
    outgoing, incoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    first_audio = asyncio.Event()
    rig.sources["track 0"] = partial(source, 12)
    rig.sources["track 1"] = partial(source, 0, 30, first_audio)
    install_mixer(rig, monkeypatch)
    await rig.capture(outgoing)
    await rig.delivered(outgoing)
    flow = asyncio.ensure_future(
        drain(rig.audio.get_queue_flow_stream(queue, outgoing, PCM_FORMAT, SESSION_ID))
    )

    # the preparation of the selected track waits for its first audio
    assert incoming.streamdetails is not None
    while incoming.streamdetails.buffer is None:
        await asyncio.sleep(0)
    prepared_buffer = rig.buffer(incoming)
    assert list(prepared_buffer.users) == ["prepare_next"]
    await rig.queues._enqueue_with_option(
        QUEUE_ID, [rig.track("track 5", realtime=False)], QueueOption.NEXT
    )
    first_audio.set()
    emitted = await flow

    assert not prepared_buffer.cancelled
    assert rig.opened["track 1"] == 1
    # all of both tracks
    assert emitted == 42 * PCM_FORMAT.pcm_sample_size


async def test_a_superseded_flow_stream_leaves_the_track_of_the_active_one_alone(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A flow stream that a newer session took over does not touch what the newer one plays next.

    The old stream is still loading its next track when a skip starts a new flow stream, which
    takes its own next track and builds the crossfade into it while the old load returns.
    """
    first, _old_next, playing, incoming = rig.play(
        "track 0", "track 1", "track 2", "track 3", realtime=False
    )
    queue_data = rig.queues._queue_data[QUEUE_ID]
    gate, old_load_waits, old_load_returns = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rig.sources["track 0"] = partial(source, 2)
    rig.sources["track 2"] = partial(source, 30)
    rig.sources["track 3"] = partial(source, 20, 10, gate)
    new_next = rig.track("track 5", realtime=False)

    async def _load_next(queue_id: str, current_item_id: str) -> QueueItem:
        if current_item_id == first.queue_item_id:
            # the old stream's next track takes its time to load
            old_load_waits.set()
            await old_load_returns.wait()
        return await rig._load_next(queue_id, current_item_id)

    rig.queues.load_next_queue_item = AsyncMock(side_effect=_load_next)  # type: ignore[method-assign]
    old_stream = asyncio.ensure_future(
        drain(rig.audio.get_queue_flow_stream(queue_data.queue, first, PCM_FORMAT, SESSION_ID))
    )

    async def _old_load_returns_then_play_next() -> None:
        old_load_returns.set()
        await old_stream
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
        with pytest.raises(ProviderStreamLimitError):
            await rig.ask(new_next, wait=0.2)
        gate.set()

    install_mixer(rig, monkeypatch, _old_load_returns_then_play_next)
    await old_load_waits.wait()
    # a skip starts a new session, whose flow stream plays from the third track on
    queue_data.session_id = "session-b"
    queue_data.queue.current_index = queue_data.queue.index_in_buffer = 2
    queue_data.queue.current_item = playing
    emitted = await drain(
        rig.audio.get_queue_flow_stream(queue_data.queue, playing, PCM_FORMAT, "session-b")
    )

    # all of the playing track and of the one it took to play next, 30 seconds each
    assert emitted == 60 * PCM_FORMAT.pcm_sample_size
    assert not rig.buffer(incoming).cancelled


async def test_a_second_flow_stream_of_the_same_session_leaves_the_active_ones_track_alone(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An older flow stream of the same session cannot take the active one's next track away.

    A speaker that reconnects starts a second flow stream in the session while the first one
    still awaits its next track. That load returns, the first stream goes on by itself and its
    speaker hangs up, all while the second stream builds the crossfade into its own next track.
    """
    first, _old_next, playing, incoming = rig.play(
        "track 0", "track 1", "track 2", "track 3", realtime=False
    )
    queue_data = rig.queues._queue_data[QUEUE_ID]
    gate, old_load_waits, old_load_returns = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rig.sources["track 0"] = partial(source, 2)
    rig.sources["track 2"] = partial(source, 30)
    rig.sources["track 3"] = partial(source, 20, 10, gate)
    new_next = rig.track("track 5", realtime=False)

    async def _load_next(queue_id: str, current_item_id: str) -> QueueItem:
        if current_item_id == first.queue_item_id:
            old_load_waits.set()
            await old_load_returns.wait()
        return await rig._load_next(queue_id, current_item_id)

    rig.queues.load_next_queue_item = AsyncMock(side_effect=_load_next)  # type: ignore[method-assign]
    old_stream = asyncio.ensure_future(
        drain(rig.audio.get_queue_flow_stream(queue_data.queue, first, PCM_FORMAT, SESSION_ID))
    )

    async def _old_stream_goes_on_and_ends_then_play_next() -> None:
        old_load_returns.set()
        for _ in range(5):
            await asyncio.sleep(0)
        old_stream.cancel()
        with suppress(asyncio.CancelledError):
            await old_stream
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
        with pytest.raises(ProviderStreamLimitError):
            await rig.ask(new_next, wait=0.2)
        gate.set()

    install_mixer(rig, monkeypatch, _old_stream_goes_on_and_ends_then_play_next)
    await old_load_waits.wait()
    queue_data.queue.current_index = queue_data.queue.index_in_buffer = 2
    queue_data.queue.current_item = playing
    emitted = await drain(
        rig.audio.get_queue_flow_stream(queue_data.queue, playing, PCM_FORMAT, SESSION_ID)
    )

    assert emitted == 60 * PCM_FORMAT.pcm_sample_size
    assert not rig.buffer(incoming).cancelled
    # each stream took its own claim away again
    assert rig.audio._item_claims == set()


async def test_a_flow_stream_that_ends_lets_go_of_the_removed_track_it_held(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed track a flow stream was fading into gives its slot up when that stream ends."""
    outgoing, incoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    rig.sources["track 0"] = partial(source, 30)
    rig.sources["track 1"] = partial(source, 20, 10, asyncio.Event())
    removed = asyncio.Event()

    async def _replace_next_then_keep_building() -> None:
        await rig.queues._enqueue_with_option(
            QUEUE_ID, [rig.track("track 5", realtime=False)], QueueOption.REPLACE_NEXT
        )
        removed.set()
        await asyncio.Event().wait()

    install_mixer(rig, monkeypatch, _replace_next_then_keep_building)
    flow = asyncio.ensure_future(
        drain(rig.audio.get_queue_flow_stream(queue, outgoing, PCM_FORMAT, SESSION_ID))
    )
    await removed.wait()
    capture = rig.buffer(incoming)
    assert rig.fills(capture)
    # nobody reads it yet: the flow stream's claim on the track is what holds it
    assert list(capture.users) == []
    assert rig.audio._users_of(capture) == ["flow stream of Queue A (claims it)"]

    # the speaker disconnects while the fade is still being built
    flow.cancel()
    with suppress(asyncio.CancelledError):
        await flow
    await rig.settle()

    assert list(capture.users) == []
    assert not rig.fills(capture)
    assert rig.slot_free()


# --- a fade outside flow mode


async def test_a_track_a_fade_is_mixed_into_keeps_its_source_when_the_queue_changes(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The track a stream fades into is the track the player holds, so nothing takes it away.

    A "play next" that lands while the playing track's stream builds the fade goes in behind
    the incoming track, and the track played next cannot take the incoming track's slot.
    """
    playing, incoming, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    gate = asyncio.Event()
    rig.sources["track 0"] = partial(source, 30)
    rig.sources["track 1"] = partial(source, 20, 10, gate)
    new_next = rig.track("track 5", realtime=False)

    async def _play_next_while_the_fade_is_built() -> None:
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
        assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is incoming
        assert rig.queues.get_next_item(QUEUE_ID, incoming.queue_item_id) is new_next
        with pytest.raises(ProviderStreamLimitError):
            await rig.ask(new_next, wait=0.2)
        gate.set()

    install_mixer(rig, monkeypatch, _play_next_while_the_fade_is_built)
    # all of the playing track has arrived, so its stream holds its tail back for the fade
    await rig.capture(playing)
    await rig.delivered(playing)
    rig.audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    emitted = await drain(
        rig.audio.get_queue_item_stream_with_smartfade(
            MagicMock(), playing, PCM_FORMAT, CrossfadeMode.STANDARD_CROSSFADE, 8, SESSION_ID
        )
    )

    # all of the playing track; the rest of the mix waits for the incoming track's own stream
    assert emitted >= 30 * PCM_FORMAT.pcm_sample_size
    assert rig.audio._crossfade_handover[QUEUE_ID].queue_item_id == incoming.queue_item_id
    assert not rig.buffer(incoming).cancelled
    rig.audio.clear_crossfade_handover(QUEUE_ID)
    await rig.settle()


async def test_a_track_a_fade_was_decided_on_before_it_was_prepared_is_opened_once(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Outside flow mode the track a stream fades into is held by the queue's position.

    The playing track's stream decides on the next track before it is prepared, and a "play
    next" lands while the preparation waits for audio: it goes in behind that track.
    """
    playing, incoming, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    first_audio = asyncio.Event()
    rig.sources["track 0"] = partial(source, 12)
    rig.sources["track 1"] = partial(source, 0, 30, first_audio)
    install_mixer(rig, monkeypatch)
    rig.audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    await rig.capture(playing)
    await rig.delivered(playing)
    stream = asyncio.ensure_future(
        drain(
            rig.audio.get_queue_item_stream_with_smartfade(
                MagicMock(), playing, PCM_FORMAT, CrossfadeMode.STANDARD_CROSSFADE, 8, SESSION_ID
            )
        )
    )

    assert incoming.streamdetails is not None
    while incoming.streamdetails.buffer is None:
        await asyncio.sleep(0)
    prepared_buffer = rig.buffer(incoming)
    new_next = rig.track("track 5", realtime=False)
    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    assert rig.queues.get_next_item(QUEUE_ID, incoming.queue_item_id) is new_next
    first_audio.set()
    await stream

    assert not prepared_buffer.cancelled
    assert rig.opened["track 1"] == 1
    assert rig.audio._crossfade_handover[QUEUE_ID].queue_item_id == incoming.queue_item_id
    rig.audio.clear_crossfade_handover(QUEUE_ID)
    await rig.settle()


# --- a transfer


async def test_a_stream_of_the_old_player_that_ends_after_a_transfer_leaves_the_new_source_alone(
    rig: SlotRig,
) -> None:
    """A stream that ends after its queue moved to another player keeps that player playing."""
    track, _upcoming = rig.play("track 0", "track 1")
    old_capture = await rig.capture(track)
    old_stream = rig.audio.get_queue_item_stream(track, PCM_FORMAT)
    await anext(old_stream)

    await rig.transfer_to(OTHER_QUEUE_ID)
    # the stop of the old player releases its audio, in use or not
    assert not rig.fills(old_capture)
    # the new player starts the track with a source of its own
    target = rig.queues._queue_data[OTHER_QUEUE_ID]
    target.session_id = "session-b"
    target.queue.index_in_buffer = target.queue.current_index
    new_capture = await rig.capture(track)
    assert new_capture is not old_capture

    # the old player lets go of the track only now
    await old_stream.aclose()
    await rig.settle()

    assert rig.fills(new_capture)
    assert rig.buffer(track) is new_capture


async def test_a_stream_the_old_player_left_open_holds_no_track_of_the_new_player(
    rig: SlotRig,
) -> None:
    """
    A track the new player prepares is not held by a stream the old player still has open.

    The old player asked for the prepared track and never let go of it. After the transfer
    the new player prepares that track again and the track is taken off its queue: the track
    that plays next there gets the service's only stream.
    """
    playing, prepared, following = rig.play("track 0", "track 1", "track 2")
    await rig.prepare_next(playing)
    old_stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT, session_id=SESSION_ID)
    await anext(old_stream)

    await rig.transfer_to(OTHER_QUEUE_ID)
    target = rig.queues._queue_data[OTHER_QUEUE_ID]
    target.session_id = "session-b"
    target.queue.index_in_buffer = target.queue.current_index
    new_capture = await rig.prepare_next(playing)
    assert rig.fills(new_capture)
    rig.queues.delete_item(OTHER_QUEUE_ID, prepared.queue_item_id)
    buffer = await rig.ask(following)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(new_capture)
    await old_stream.aclose()


async def test_a_stream_of_an_earlier_playback_holds_no_track_of_the_one_that_followed(
    rig: SlotRig,
) -> None:
    """
    A stream a speaker left open when its queue started over holds no track of the new playback.

    The speaker asked for the prepared track, then playback started over on the same queue.
    The track is prepared again and taken off the queue: the track that plays next gets the
    service's only stream.
    """
    playing, prepared, following = rig.play("track 0", "track 1", "track 2")
    await rig.prepare_next(playing)
    old_stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT, session_id=SESSION_ID)
    await anext(old_stream)

    # the start of an item releases what the playback before it held, in use or not
    await rig.queues._cleanup_queue_audio_data(QUEUE_ID)
    rig.queues._queue_data[QUEUE_ID].session_id = "session-after"
    new_capture = await rig.prepare_next(playing)
    assert rig.fills(new_capture)
    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    buffer = await rig.ask(following)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(new_capture)
    await old_stream.aclose()


# --- audio buffered ahead for a queue that is not playing yet


# --- what the rule leaves alone


async def test_a_prepared_track_that_still_plays_next_is_not_taken_by_its_own_queue(
    rig: SlotRig,
) -> None:
    """A request for another track of the queue cannot take the slot of the next track."""
    playing, _prepared, later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)

    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(later, wait=0.2)

    assert rig.fills(prepared_capture)


async def test_a_prepared_track_that_still_plays_next_is_not_taken_by_another_queue(
    rig: SlotRig,
) -> None:
    """Playback on another player cannot take the slot of a queue's next track."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    (other_track,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(other_track, wait=0.2)

    assert rig.fills(prepared_capture)


async def test_a_track_a_speaker_asks_for_out_of_turn_gets_its_audio(rig: SlotRig) -> None:
    """A speaker that jumps to a track by itself is served, though the queue plays it later."""
    _playing, _upcoming, later = rig.play("track 0", "track 1", "track 2")

    stream = rig.audio.get_queue_item_stream(later, PCM_FORMAT)
    first_audio = await anext(stream)
    await rig.settle()

    assert len(first_audio) == PCM_FORMAT.pcm_sample_size
    assert rig.fills(rig.buffer(later))
    await stream.aclose()


async def test_a_stale_capture_gives_its_slot_to_another_queue(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A capture nobody needs makes way for playback on another player of the same account.

    Its slot is kept for its own queue first, which goes on playing from that account, and
    goes to the other player once that queue did not come for it.
    """
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.2)
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    stale_capture = await rig.prepare_next(playing)
    await rig.queues._enqueue_with_option(
        QUEUE_ID, [rig.track("track 5")], QueueOption.REPLACE_NEXT
    )
    (other_track,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    buffer = await rig.ask(other_track, wait=30)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not rig.fills(stale_capture)


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_moved_track_that_is_read_keeps_its_capture_until_its_reader_leaves(
    rig: SlotRig,
) -> None:
    """A prepared track moved back keeps its capture for its reader, also with a slot to spare."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    await rig.settle()
    assert rig.fills(prepared_capture)
    # its last reader leaves while a slot is free
    await stream.aclose()
    await rig.settle()
    buffer = await rig.ask(new_next)

    assert buffer is not None
    assert not rig.fills(prepared_capture)


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_moved_track_gives_its_capture_up_while_a_slot_is_free(rig: SlotRig) -> None:
    """A prepared track that waits further down gives its capture up, slot to spare or not."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    moved_capture = await rig.prepare_next(playing)
    assert rig.slot_free()
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    await rig.settle()
    next_capture = await rig.prepare_next(playing)

    assert not rig.fills(moved_capture)
    # the track that plays next keeps its own
    assert rig.fills(next_capture)


@pytest.mark.parametrize(
    "rig",
    [TwoStreamProvider, UnlimitedProvider],
    ids=["a slot to spare", "no limit"],
    indirect=True,
)
@pytest.mark.parametrize("change", ["removed", "stopped"])
async def test_a_capture_nothing_will_play_goes_when_its_reader_leaves_with_a_slot_to_spare(
    rig: SlotRig, change: str
) -> None:
    """A capture nothing will play is kept for its reader and goes with it, slot to spare or not."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)

    if change == "removed":
        rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    else:
        rig.queues._queue_data[QUEUE_ID].session_id = None
    await rig.settle()
    assert rig.fills(capture)
    # its last reader leaves
    await stream.aclose()
    await rig.settle()

    assert not rig.fills(capture)
    assert capture.cancelled


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_removed_track_gives_its_capture_up_while_a_slot_is_free(rig: SlotRig) -> None:
    """A prepared track that left the queue gives its capture up, slot to spare or not."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    removed_capture = await rig.prepare_next(playing)
    assert rig.slot_free()

    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    await rig.settle()
    next_capture = await rig.prepare_next(playing)

    assert not rig.fills(removed_capture)
    # the track that plays next keeps its own
    assert rig.fills(next_capture)


@pytest.mark.parametrize("rig", [UnlimitedProvider], indirect=True)
async def test_a_moved_track_gives_its_capture_up_on_a_provider_without_a_stream_limit(
    rig: SlotRig,
) -> None:
    """A prepared track that waits further down gives its capture up where no slot can run out."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], QueueOption.NEXT)
    buffer = await rig.ask(new_next)
    await rig.settle()

    assert buffer is not None
    assert not rig.fills(prepared_capture)


async def test_a_change_that_keeps_the_next_track_leaves_its_capture_alone(rig: SlotRig) -> None:
    """Items added behind the prepared track, or a refresh of the same items, change nothing."""
    playing, _prepared, later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(playing)
    queue_data = rig.queues._queue_data[QUEUE_ID]
    added = rig.track("track 5")

    rig.queues.update_items(QUEUE_ID, [*queue_data.items, added])
    rig.queues.update_items(QUEUE_ID, queue_data.items)
    for item in (later, added):
        with pytest.raises(ProviderStreamLimitError):
            await rig.ask(item, wait=0.1)

    assert rig.fills(prepared_capture)


@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_the_playing_track_keeps_its_capture_when_what_follows_it_changes(
    rig: SlotRig, option: QueueOption
) -> None:
    """A capture the speaker still plays from is never given up for the queue's next track."""
    playing, _next = rig.play("track 0", "track 1")
    playing_capture = await rig.capture(playing)
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], option)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(new_next, wait=0.2)

    assert rig.fills(playing_capture)
    assert not rig.slot_free()


@pytest.mark.parametrize(
    ("current_index", "index_in_buffer"),
    [(1, 1), (0, 1), (1, 2)],
    # it plays; the player took it while the one before still plays; it plays while the player
    # already took the one after it
    ids=["playing", "taken_by_the_player", "playing_with_the_next_taken"],
)
@pytest.mark.parametrize(
    "option", [QueueOption.REPLACE_NEXT, QueueOption.NEXT], ids=["replace_next", "next"]
)
async def test_a_prepared_track_that_plays_keeps_its_capture_when_what_follows_it_changes(
    rig: SlotRig, option: QueueOption, current_index: int, index_in_buffer: int
) -> None:
    """Once the prepared track plays or the player took it, changes after it leave it alone."""
    first, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    prepared_capture = await rig.prepare_next(first)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.current_index = current_index
    queue.current_item = rig.queues.get_item(QUEUE_ID, current_index)
    queue.index_in_buffer = index_in_buffer
    new_next = rig.track("track 5")

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_next], option)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(new_next, wait=0.2)

    assert rig.fills(prepared_capture)


# --- what a lab run shows


async def test_a_wait_for_a_slot_names_the_holders_and_their_users(
    rig: SlotRig, caplog: pytest.LogCaptureFixture
) -> None:
    """The log says which source holds the slot a track waits for, and who uses it."""
    playing, prepared, later = rig.play("track 0", "track 1", "track 2")
    await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)

    with (
        caplog.at_level(logging.DEBUG, logger=rig.audio.logger.name),
        pytest.raises(ProviderStreamLimitError),
    ):
        await rig.ask(later, wait=0.1)
    await stream.aclose()

    assert prepared.streamdetails is not None
    assert (
        "No free Single stream slot for track 2, held by: "
        f"{prepared.streamdetails.uri} (used by: stream of track 1, reader)"
    ) in caplog.text


async def test_a_wait_for_a_slot_names_the_streams_that_hold_the_track(
    rig: SlotRig, caplog: pytest.LogCaptureFixture
) -> None:
    """The log names a stream that claims the holder's track, and none of an earlier playback."""
    playing, prepared, later = rig.play("track 0", "track 1", "track 2")
    await rig.prepare_next(playing)
    claim = audio_mod._ItemClaim("flow stream of Queue A", SESSION_ID)
    earlier = audio_mod._ItemClaim("stream of an earlier playback", "session-before")
    rig.audio._claim_item(claim, prepared)
    rig.audio._claim_item(earlier, prepared)

    with (
        caplog.at_level(logging.DEBUG, logger=rig.audio.logger.name),
        pytest.raises(ProviderStreamLimitError),
    ):
        await rig.ask(later, wait=0.1)
    rig.audio._claim_item(claim, None)
    rig.audio._claim_item(earlier, None)

    assert prepared.streamdetails is not None
    assert (
        "No free Single stream slot for track 2, held by: "
        f"{prepared.streamdetails.uri} (used by: flow stream of Queue A (claims it))"
    ) in caplog.text
