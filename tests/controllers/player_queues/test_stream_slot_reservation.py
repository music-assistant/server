"""
Tests for the stream slot a provider keeps for a queue that goes on playing from it.

A source that delivers at playback pace holds its provider's slot for as long as its track
plays. When it gives the slot back and its queue goes on with that provider, the slot stays
reserved for that queue for a moment, so its next source finds it. To every other taker the
slot is busy that much longer. Nothing is kept when the queue has nothing to go on with there,
when its playback ended, or for a source that loads ahead of playback.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from functools import partial
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
import music_assistant.models.music_provider as music_provider_mod
from music_assistant.models.music_provider import ProviderStreamLimitError
from tests.controllers.streams.stream_slot_rig import (
    INSTANCE,
    OTHER_QUEUE_ID,
    PCM_FORMAT,
    QUEUE_ID,
    SESSION_ID,
    SingleStreamProvider,
    SlotRig,
    TwoStreamProvider,
    allow_starts,
    count_starts,
    source,
)

# seconds another taker waits for a slot in these tests, far less than a reservation lasts
BRIEF_WAIT = 0.3


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SlotRig]:
    """Build the rig with every music source open to the queue's playback."""
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    rig = SlotRig(getattr(request, "param", SingleStreamProvider))
    rig.mass.providers = [rig.provider]
    yield rig
    await rig.close()


async def _turns() -> None:
    """Let everything that can run without waiting for time run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _play_through(rig: SlotRig, *names: str, realtime: bool = True) -> asyncio.Event:
    """
    Let the queue play the first of the given tracks from a source that ends when told to.

    :param rig: The rig to put the queue on.
    :param names: The tracks of the queue; the first one plays.
    :param realtime: Whether the sources deliver at playback pace.
    :return: The event that lets the playing track's source deliver its last audio and end.
    """
    playing = rig.play(*names, realtime=realtime)[0]
    ended = asyncio.Event()
    rig.sources[names[0]] = partial(source, 3, 0, ended)
    await rig.ask(playing)
    return ended


async def _source_ends(rig: SlotRig, ended: asyncio.Event) -> None:
    """Let the playing track's source deliver everything and give its slot back."""
    ended.set()
    await rig.delivered(rig.queues._queue_data[QUEUE_ID].items[0])


async def _preview(rig: SlotRig, wait: float) -> bool:
    """
    Open a source without a queue behind it, as a preview does.

    :param rig: The rig whose provider the source is on.
    :param wait: Seconds the request may wait for a slot.
    :return: Whether the source was opened.
    """
    details = StreamDetails(
        provider=INSTANCE,
        item_id="track-preview",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        stream_type=StreamType.CUSTOM,
        duration=30,
    )
    rig.sources["preview"] = partial(source, 1)
    stream = rig.audio.get_media_stream(details, PCM_FORMAT, source_wait_timeout=wait)
    try:
        await anext(stream)
    finally:
        await stream.aclose()
    return True


# --- a queue that goes on with the provider keeps its slot


async def test_another_queue_does_not_take_the_stream_between_two_tracks(rig: SlotRig) -> None:
    """A queue that asks while another one moves to its next track waits; that track plays."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    other_started = count_starts(rig, "track 7")

    await _source_ends(rig, ended)
    asking = asyncio.create_task(rig.ask(other, wait=BRIEF_WAIT))
    await _turns()
    assert not asking.done()
    next_capture = await rig.ask(upcoming, wait=0)

    assert next_capture is not None
    assert rig.fills(next_capture)
    with pytest.raises(ProviderStreamLimitError):
        await asking
    assert other_started == []


async def test_a_preview_that_waits_does_not_slip_in_between_two_tracks(rig: SlotRig) -> None:
    """A preview that already waits when a track's source ends stays behind the next track."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)
    await _turns()
    assert not preview.done()
    next_capture = await rig.ask(upcoming, wait=0)

    assert next_capture is not None
    assert rig.fills(next_capture)
    with pytest.raises(ProviderStreamLimitError):
        await preview


async def test_the_queue_prepares_its_next_track_ahead_of_a_preview_that_waits(
    rig: SlotRig,
) -> None:
    """The next track the queue prepares by itself when a track ends finds the slot kept for it."""
    ended = await _play_through(rig, "track 0", "track 1")
    playing, upcoming = rig.queues._queue_data[QUEUE_ID].items
    # the player fetches the playing track, so the end of its source prepares the next one
    rig.queues._queue_data[QUEUE_ID].last_served_item_id = playing.queue_item_id
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)
    await rig.settle()

    assert rig.fills(rig.buffer(upcoming))
    assert rig.buffer(upcoming).seconds_available > 0
    with pytest.raises(ProviderStreamLimitError):
        await preview


async def test_another_queue_does_not_take_the_stream_at_a_skip(rig: SlotRig) -> None:
    """The slot a skip frees goes to the track the queue skipped to, not to a queue that asks."""
    tracks = rig.play("track 0", "track 1", "track 2")
    stopped_capture = await rig.ask(tracks[0])
    assert stopped_capture is not None
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    other_started = count_starts(rig, "track 7")
    allow_starts(rig)
    asked_in_between: list[asyncio.Task[object]] = []

    async def _load(_queue_item: object, **_kwargs: object) -> None:
        # what the start of a track does: stop the sources in its way, then get its audio
        await rig.queues._abort_superseded_source_buffers(tracks[2])
        asked_in_between.append(asyncio.create_task(rig.ask(other, wait=BRIEF_WAIT)))
        await _turns()
        await rig.audio.get_audio_buffer(tracks[2], reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 2)

    assert not rig.fills(stopped_capture)
    assert rig.fills(rig.buffer(tracks[2]))
    with pytest.raises(ProviderStreamLimitError):
        await asked_in_between[0]
    assert other_started == []


async def test_a_preview_that_waits_does_not_take_the_stream_at_a_seek(rig: SlotRig) -> None:
    """The slot a seek frees goes to the same track's new source, also on the last track."""
    (track,) = rig.play("track 0")
    replaced_capture = await rig.ask(track)
    assert replaced_capture is not None
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    capture = await rig.audio.get_audio_buffer(
        track, seek_position_ms=60_000, reason="streaming", capacity_wait_timeout=0
    )

    assert capture is not None
    assert capture is not replaced_capture
    assert rig.fills(capture)
    with pytest.raises(ProviderStreamLimitError):
        await preview


# --- nothing is kept where the queue does not go on


async def test_a_preview_that_waits_gets_the_stream_after_the_last_track(rig: SlotRig) -> None:
    """After the last track of a queue nothing is kept, so whoever waits is served at once."""
    ended = await _play_through(rig, "track 0")
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)

    assert await asyncio.wait_for(preview, BRIEF_WAIT)


async def test_a_preview_that_waits_gets_the_stream_when_the_next_track_is_from_elsewhere(
    rig: SlotRig,
) -> None:
    """A queue whose next track comes from another provider keeps nothing on this one."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    assert upcoming.streamdetails is not None
    upcoming.streamdetails.provider = "elsewhere--account_y"
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)

    assert await asyncio.wait_for(preview, BRIEF_WAIT)


async def test_a_next_track_without_a_source_yet_keeps_the_stream(rig: SlotRig) -> None:
    """A next track whose source is not chosen yet can come from this provider, so it is kept."""
    ended = await _play_through(rig, "track 0", "track 1")
    rig.queues._queue_data[QUEUE_ID].items[1].streamdetails = None
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)

    with pytest.raises(ProviderStreamLimitError):
        await preview


async def test_a_track_a_stream_decided_to_play_keeps_the_stream(rig: SlotRig) -> None:
    """A track off the queue that a stream of the playback still claims is one to go on with."""
    ended = await _play_through(rig, "track 0")
    claimed = rig.track("track 4")
    claim = audio_mod._ItemClaim("flow stream of Queue A", SESSION_ID)
    rig.audio._claim_item(claim, claimed)
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)

    with pytest.raises(ProviderStreamLimitError):
        await preview
    rig.audio._claim_item(claim, None)


async def test_a_queue_that_has_all_the_audio_it_plays_keeps_no_stream(rig: SlotRig) -> None:
    """A prepared last track that is removed leaves nothing to go on with, so nothing is kept."""
    ended = await _play_through(rig, "track 0", "track 1")
    playing, prepared = rig.queues._queue_data[QUEUE_ID].items
    await _source_ends(rig, ended)
    await rig.prepare_next(playing)
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()
    assert not preview.done()

    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)

    assert await asyncio.wait_for(preview, BRIEF_WAIT)


async def test_a_source_that_loads_ahead_keeps_no_stream_for_its_queue(rig: SlotRig) -> None:
    """A track that loaded ahead of playback leaves its slot to whoever asks, also mid-queue."""
    ended = await _play_through(rig, "track 0", "track 1", realtime=False)
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    await _source_ends(rig, ended)
    buffer = await rig.ask(other, wait=0)

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_queue_whose_playback_ended_keeps_no_stream(rig: SlotRig) -> None:
    """The source a stop ends gives its slot to whoever asks next."""
    await _play_through(rig, "track 0", "track 1")
    stopped_capture = rig.buffer(rig.queues._queue_data[QUEUE_ID].items[0])
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    rig.queues._end_session(rig.queues._queue_data[QUEUE_ID], SESSION_ID)
    await rig.settle()
    assert not rig.fills(stopped_capture)
    buffer = await rig.ask(other, wait=0)

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_stream_kept_for_a_queue_is_free_at_once_when_its_playback_ends(
    rig: SlotRig,
) -> None:
    """A queue that is stopped between two tracks no longer holds another queue back."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)
    asking = asyncio.create_task(rig.ask(other))
    await _turns()
    assert not asking.done()

    rig.queues._end_session(rig.queues._queue_data[QUEUE_ID], SESSION_ID)

    # far inside the time the slot would have been kept
    buffer = await asyncio.wait_for(asking, BRIEF_WAIT)
    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_session_that_is_no_longer_the_playing_one_ends_no_reservation(
    rig: SlotRig,
) -> None:
    """Letting go of an earlier playback leaves the slot of the playback that followed alone."""
    ended = await _play_through(rig, "track 0", "track 1")
    await _source_ends(rig, ended)

    rig.queues._end_session(rig.queues._queue_data[QUEUE_ID], "an earlier session")

    with pytest.raises(ProviderStreamLimitError):
        await _preview(rig, 0)


# --- what another taker sees


async def test_another_queue_starts_once_a_kept_stream_was_not_used(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue that asks between two tracks starts within its wait when the next one never asks."""
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.2)
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    await _source_ends(rig, ended)
    buffer = await rig.ask(other)

    assert buffer is not None
    assert rig.fills(buffer)


@pytest.mark.parametrize("rig", [TwoStreamProvider], indirect=True)
async def test_a_second_stream_of_the_provider_is_not_held_back(rig: SlotRig) -> None:
    """One slot is kept for the queue that goes on; the provider's other slot is free to anyone."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other, another) = rig.play(
        "track 7", "track 8", queue_id=OTHER_QUEUE_ID, session_id="session-b"
    )

    await _source_ends(rig, ended)
    buffer = await rig.ask(other, wait=0)

    assert buffer is not None
    assert rig.fills(buffer)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(another, wait=0)


async def test_a_source_on_a_replaced_provider_object_holds_nothing_on_the_new_one(
    rig: SlotRig,
) -> None:
    """A source that ends after its provider was reloaded keeps no slot of the new object."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    replaced = rig.provider
    rig.provider = type(replaced)(replaced.mass, replaced.manifest, replaced.config)
    rig.provider.available = True
    rig.mass.providers = [rig.provider]

    # the new object starts with its slot free, while the old source still plays
    assert await _preview(rig, 0)
    await _source_ends(rig, ended)

    # what the old object keeps for the queue holds nobody back on the new one
    assert replaced.has_available_stream_slot
    assert await _preview(rig, 0)
    buffer = await rig.ask(other, wait=0)
    assert buffer is not None
    assert rig.fills(buffer)
    with pytest.raises(ProviderStreamLimitError):
        await rig.ask(upcoming, wait=0)
