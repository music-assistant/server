"""
Tests for the record a queue keeps of the audio buffers made for its items.

A buffer knows the queue it was made for, and that queue keeps it on record for as long as it
holds audio, also when its item has left the queue.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from tests.controllers.streams.stream_slot_rig import (
    OTHER_QUEUE_ID,
    PCM_FORMAT,
    QUEUE_ID,
    SESSION_ID,
    SlotRig,
    TwoStreamProvider,
    drain,
    install_mixer,
    source,
)

if TYPE_CHECKING:
    from music_assistant.controllers.streams.audio_buffer import AudioBuffer


def _record(rig: SlotRig, queue_id: str = QUEUE_ID) -> list[AudioBuffer]:
    """Return the buffers the given queue has on record."""
    return rig.queues._queue_data[queue_id].audio_buffers


# --- every buffer made for a queue item is recorded


async def test_a_buffer_knows_the_queue_it_was_made_for(rig: SlotRig) -> None:
    """The buffer of a queue item names that item's queue."""
    playing, _upcoming = rig.play("track 0", "track 1")

    buffer = await rig.ask(playing)

    assert buffer is not None
    assert buffer.queue_id == QUEUE_ID


async def test_the_start_of_an_item_is_recorded(rig: SlotRig) -> None:
    """The buffer a start makes for its item is on the queue's record."""
    playing, _upcoming = rig.play("track 0", "track 1")

    buffer = await rig.audio.get_audio_buffer(playing, reason="prepare")

    assert _record(rig) == [buffer]


async def test_the_preparation_of_the_next_track_is_recorded(rig: SlotRig) -> None:
    """The buffer the queue prepares for its next track is on its record."""
    playing, _upcoming = rig.play("track 0", "track 1")

    prepared = await rig.prepare_next(playing)

    assert _record(rig) == [prepared]


async def test_the_request_of_a_speaker_is_recorded(rig: SlotRig) -> None:
    """The buffer a speaker's stream makes for its track is on the queue's record."""
    playing, _upcoming = rig.play("track 0", "track 1")

    stream = rig.audio.get_queue_item_stream(playing, PCM_FORMAT)
    await anext(stream)

    assert _record(rig) == [rig.buffer(playing)]
    await stream.aclose()


async def test_the_tracks_of_a_flow_stream_are_recorded(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flow stream's buffers, the one it fades into included, are on the queue's record."""
    outgoing, incoming = rig.play("track 0", "track 1", realtime=False)
    queue = rig.queues._queue_data[QUEUE_ID].queue
    rig.sources["track 0"] = partial(source, 30)
    rig.sources["track 1"] = partial(source, 30)
    install_mixer(rig, monkeypatch)

    await drain(rig.audio.get_queue_flow_stream(queue, outgoing, PCM_FORMAT, SESSION_ID))

    assert _record(rig) == [rig.buffer(outgoing), rig.buffer(incoming)]


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_buffer_that_replaces_one_that_is_read_is_recorded_next_to_it(rig: SlotRig) -> None:
    """A position the buffer cannot serve gets a buffer of its own, next to the one still read."""
    (track,) = rig.play("track 0")
    first = await rig.ask(track)
    assert first is not None
    reader = first.get_raw_stream()
    await anext(reader)

    second = await rig.audio.get_audio_buffer(track, seek_position_ms=60_000, reason="streaming")

    assert second is not first
    assert rig.buffer(track) is second
    assert _record(rig) == [first, second]
    await reader.aclose()
    await rig.settle()


async def test_a_buffer_made_after_a_transfer_is_recorded_on_the_new_queue(rig: SlotRig) -> None:
    """After a transfer the track's new buffer belongs to the queue that plays it now."""
    track, _upcoming = rig.play("track 0", "track 1")
    await rig.capture(track)
    await rig.transfer_to(OTHER_QUEUE_ID)
    target = rig.queues._queue_data[OTHER_QUEUE_ID]
    target.session_id = "session-b"
    target.queue.index_in_buffer = target.queue.current_index

    new_capture = await rig.capture(track)

    assert new_capture.queue_id == OTHER_QUEUE_ID
    assert _record(rig, OTHER_QUEUE_ID) == [new_capture]
    assert new_capture not in _record(rig)


# --- what stays on the record and what goes


async def test_a_buffer_of_an_item_that_left_the_queue_stays_on_record(rig: SlotRig) -> None:
    """A track that was taken off the queue while it is read keeps its buffer on the record."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    stream = rig.audio.get_queue_item_stream(prepared, PCM_FORMAT)
    await anext(stream)

    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    await rig.queues.get_diagnostics()

    assert rig.queues.get_item(QUEUE_ID, prepared.queue_item_id) is None
    assert _record(rig) == [capture]
    await stream.aclose()


async def test_a_cleared_buffer_leaves_the_record_when_the_next_one_is_recorded(
    rig: SlotRig,
) -> None:
    """A buffer that released its audio is dropped once the queue records another one."""
    playing, upcoming = rig.play("track 0", "track 1")
    first = await rig.ask(playing)
    assert first is not None
    await first.clear()

    second = await rig.ask(upcoming)

    assert _record(rig) == [second]


async def test_a_cleared_buffer_leaves_the_record_when_the_record_is_read(rig: SlotRig) -> None:
    """A buffer that released its audio is dropped without another one being recorded."""
    playing, upcoming = rig.play("track 0", "track 1", realtime=False)
    rig.sources["track 0"] = partial(source, 30)
    first = await rig.ask(playing)
    assert first is not None
    await rig.delivered(playing)
    second = await rig.ask(upcoming)
    await first.clear()

    await rig.queues.get_diagnostics()

    assert _record(rig) == [second]


async def test_a_buffer_that_still_holds_audio_stays_when_another_one_is_recorded(
    rig: SlotRig,
) -> None:
    """A buffer whose source delivered everything stays on record next to a newer one."""
    playing, upcoming = rig.play("track 0", "track 1", realtime=False)
    rig.sources["track 0"] = partial(source, 30)
    first = await rig.ask(playing)
    await rig.delivered(playing)

    second = await rig.ask(upcoming)

    assert _record(rig) == [first, second]


# --- who holds a stream slot


async def test_the_holder_of_a_stream_slot_is_the_buffer_of_its_track(rig: SlotRig) -> None:
    """The source that holds a provider's stream slot is known by its buffer and its queue."""
    playing, _upcoming = rig.play("track 0", "track 1")
    await rig.ask(playing)

    holders = rig.provider.stream_slot_holders

    assert holders == (rig.buffer(playing),)
    assert [holder.queue_id for holder in holders] == [QUEUE_ID]


# --- diagnostics


async def test_the_diagnostics_count_the_buffers_that_hold_audio(rig: SlotRig) -> None:
    """The queue controller reports how many buffers its queues have on record."""
    playing, upcoming = rig.play("track 0", "track 1", realtime=False)
    rig.sources["track 0"] = partial(source, 30)
    first = await rig.ask(playing)
    assert first is not None
    await rig.delivered(playing)
    await rig.ask(upcoming)
    assert (await rig.queues.get_diagnostics())["audio_buffers"] == 2

    await first.clear()

    assert (await rig.queues.get_diagnostics())["audio_buffers"] == 1
