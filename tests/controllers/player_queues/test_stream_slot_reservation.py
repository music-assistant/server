"""
Tests for the stream slot a provider keeps for a queue that goes on playing from it.

A source that delivers at playback pace holds its provider's slot for as long as its track
plays. When it gives the slot back and its queue goes on with that provider, the slot stays
reserved for that queue for a moment, so its next source finds it. To every other taker the
slot is busy that much longer: nobody is refused because of it. Nothing is kept when the queue
has nothing to go on with there, when it is paused or its playback ended, or for a source that
loads ahead of playback.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption, StreamType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import ProviderMapping
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
import music_assistant.models.music_provider as music_provider_mod
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.provider_access import PlaybackSources
from music_assistant.models.music_provider import (
    ProviderStreamHeldError,
    ProviderStreamLimitError,
)
from tests.controllers.streams.stream_slot_rig import (
    INSTANCE,
    OTHER_QUEUE_ID,
    PCM_FORMAT,
    QUEUE_ID,
    SESSION_ID,
    SingleStreamProvider,
    SlotRig,
    TwoStreamProvider,
    allow_commands,
    allow_starts,
    count_starts,
    realtime_capture,
    source,
)

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

# seconds another taker waits for a slot in these tests, far less than a reservation lasts
BRIEF_WAIT = 0.3
# seconds within which what happens at once has happened, on a loaded machine too, and still
# less than a reservation lasts
SOON = 2.0
# seconds a start may wait for a slot when the test decides what ends that wait
LONG_WAIT = 30.0
ELSEWHERE = "elsewhere--account_y"
SIBLING = "single--account_z"
THIRD_QUEUE_ID = "queue_c"


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SlotRig]:
    """Build the rig with every music source open to the queue's playback."""
    monkeypatch.setattr(
        audio_mod, "playback_sources", AsyncMock(return_value=PlaybackSources(None, []))
    )
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


def _move_elsewhere(item: QueueItem, source_chosen: bool) -> None:
    """
    Make a track one of another music service.

    :param item: The queue item of the track.
    :param source_chosen: Whether its stream details are resolved already.
    """
    assert item.streamdetails is not None
    assert item.media_item is not None
    if source_chosen:
        item.streamdetails.provider = ELSEWHERE
        return
    item.streamdetails = None
    item.media_item.provider_mappings = {
        ProviderMapping(
            item_id=item.media_item.item_id,
            provider_domain="elsewhere",
            provider_instance=ELSEWHERE,
            audio_format=PCM_FORMAT,
        )
    }


# --- a queue that goes on with the provider keeps its slot


async def test_another_queue_does_not_take_the_stream_between_two_tracks(rig: SlotRig) -> None:
    """A queue that starts while another one moves to its next track waits; that track plays."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    other_started = count_starts(rig, "track 7")

    await _source_ends(rig, ended)
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    assert not asking.done()
    next_capture = await rig.ask(upcoming, wait=0)

    assert next_capture is not None
    assert rig.fills(next_capture)
    # and is told who plays as soon as that track has the stream
    with pytest.raises(ProviderStreamHeldError, match="already playing on Queue A"):
        await asyncio.wait_for(asking, SOON)
    assert other_started == []


async def test_a_preview_that_waits_does_not_slip_in_between_two_tracks(rig: SlotRig) -> None:
    """A preview that already waits when a track's source ends stays behind the next track."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    await _source_ends(rig, ended)
    await _turns()
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


async def test_a_queue_gets_the_stream_that_is_kept_for_it_without_a_wait(rig: SlotRig) -> None:
    """The next track of the queue takes what is kept for it at once, however long it may wait."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    await _source_ends(rig, ended)

    next_capture = await asyncio.wait_for(rig.ask(upcoming, wait=LONG_WAIT), SOON)

    assert next_capture is not None
    assert rig.fills(next_capture)


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
        asked_in_between.append(asyncio.create_task(rig.ask(other, wait=LONG_WAIT)))
        await _turns()
        await rig.audio.get_audio_buffer(tracks[2], reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 2)

    assert not rig.fills(stopped_capture)
    assert rig.fills(rig.buffer(tracks[2]))
    with pytest.raises(ProviderStreamHeldError, match="already playing on Queue A"):
        await asyncio.wait_for(asked_in_between[0], SOON)
    assert other_started == []


@pytest.mark.parametrize("paused", [False, True], ids=["playing", "paused"])
async def test_a_preview_that_waits_does_not_take_the_stream_at_a_skip(
    rig: SlotRig, paused: bool
) -> None:
    """A queue that skips goes on with the stream also when it was paused before the skip."""
    tracks = rig.play("track 0", "track 1", "track 2")
    stopped_capture = await rig.ask(tracks[0])
    assert stopped_capture is not None
    if paused:
        # what a pause command leaves behind; the speaker reports the pause until the skip plays
        queue_data = rig.queues._queue_data[QUEUE_ID]
        queue_data.queue.state = PlaybackState.PAUSED
        queue_data.halted_by_command = True
    preview = asyncio.create_task(_preview(rig, LONG_WAIT))
    await _turns()
    allow_starts(rig)
    served_at_the_stop: list[bool] = []

    async def _load(_queue_item: object, **_kwargs: object) -> None:
        await rig.queues._abort_superseded_source_buffers(tracks[2])
        await _turns()
        served_at_the_stop.append(preview.done())
        await rig.audio.get_audio_buffer(tracks[2], reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues._play_index(QUEUE_ID, 2, None, False)

    assert served_at_the_stop == [False]
    assert rig.fills(rig.buffer(tracks[2]))
    preview.cancel()
    await asyncio.gather(preview, return_exceptions=True)


@pytest.mark.parametrize(
    "reported", [False, True], ids=["pause command", "pause the player reports"]
)
async def test_a_pause_during_a_skip_does_not_end_the_stream_kept_for_it(
    rig: SlotRig, reported: bool
) -> None:
    """A pause while a skip fetches its track leaves the stream that is kept for that track."""
    tracks = rig.play("track 0", "track 1", "track 2")
    await rig.ask(tracks[0])
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()
    allow_starts(rig)
    rig.mass.players._handle_cmd_pause = AsyncMock()
    rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    rig.queues._update_current_index_from_player = MagicMock(  # type: ignore[method-assign]
        return_value=False
    )
    kept_after_the_pause: list[bool] = []

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        await rig.queues._abort_superseded_source_buffers(queue_item)
        await _turns()
        # the user pauses while the skip still fetches the stream details of its track
        if reported:
            rig.queues._update_queue_from_player(
                SimpleNamespace(  # type: ignore[arg-type]
                    player_id=QUEUE_ID,
                    name="Queue A",
                    available=True,
                    playback_state=PlaybackState.PAUSED,
                )
            )
        else:
            await rig.queues.pause(QUEUE_ID)
        await _turns()
        kept_after_the_pause.append(QUEUE_ID in rig.provider.stream_slot_reservations)
        await rig.audio.get_audio_buffer(queue_item, reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues._play_index(QUEUE_ID, 2, None, False)

    assert kept_after_the_pause == [True]
    assert rig.fills(rig.buffer(tracks[2]))
    with pytest.raises(ProviderStreamLimitError):
        await preview


async def test_a_pause_during_a_skip_does_not_take_the_stream_from_the_track_it_plays(
    rig: SlotRig,
) -> None:
    """A pause that comes in while a skip loads its track leaves the stream to that track."""
    tracks = rig.play("track 0", "track 1", "track 2")
    await rig.ask(tracks[0])
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()
    allow_starts(rig)
    served_at_the_stop: list[bool] = []

    async def _load(_queue_item: object, **_kwargs: object) -> None:
        # what a pause command does to the queue while the skip is under way; the skip goes
        # on and hands its track to the player all the same
        queue_data = rig.queues._queue_data[QUEUE_ID]
        queue_data.queue.state = PlaybackState.PAUSED
        queue_data.halted_by_command = True
        await rig.queues._abort_superseded_source_buffers(tracks[2])
        await _turns()
        served_at_the_stop.append(preview.done())
        await rig.audio.get_audio_buffer(tracks[2], reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues._play_index(QUEUE_ID, 2, None, False)

    assert served_at_the_stop == [False]
    assert rig.fills(rig.buffer(tracks[2]))
    with pytest.raises(ProviderStreamLimitError):
        await preview


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


async def test_another_queue_does_not_take_the_stream_at_a_skip_to_another_account(
    rig: SlotRig,
) -> None:
    """A skip to a track known on another account of the service plays from this account."""
    playing, target = rig.play("track 0", "track 1")
    resolved = target.streamdetails
    assert target.media_item is not None
    target.streamdetails = None
    # the account the track was added from is gone; this account serves the same item id
    target.media_item.provider_mappings = {
        ProviderMapping(
            item_id=target.media_item.item_id,
            provider_domain="single",
            provider_instance="single--account_gone",
            audio_format=PCM_FORMAT,
        )
    }
    rig.provider.get_stream_details = AsyncMock(return_value=resolved)  # type: ignore[method-assign]
    await rig.ask(playing)
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    allow_starts(rig)
    other_started = count_starts(rig, "track 7")

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        # what the start of a track does first: stop the sources in its way
        await rig.queues._abort_superseded_source_buffers(queue_item)
        with contextlib.suppress(ProviderStreamLimitError):
            await rig.ask(other, wait=0)
        await rig.ask(queue_item, wait=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 1)

    assert rig.fills(rig.buffer(target))
    assert other_started == []


# --- nothing is kept where the queue does not go on


async def test_a_skip_to_a_track_of_another_service_keeps_no_stream(rig: SlotRig) -> None:
    """
    A queue that skips to a track from elsewhere leaves the stream to whoever waits for it.

    The track it skipped over is from this service, and is not where the queue goes on.
    """
    playing, _skipped, target = rig.play("track 0", "track 1", "track 2")
    _move_elsewhere(target, source_chosen=True)
    await rig.ask(playing)
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()
    allow_starts(rig)
    served_at_the_stop: list[bool] = []

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        # what the start of a track does first: stop the sources in its way
        await rig.queues._abort_superseded_source_buffers(queue_item)
        await _turns()
        served_at_the_stop.append(preview.done())

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 2)

    # at the stop of the outgoing source already, not only once the new track is loaded
    assert served_at_the_stop == [True]
    assert await preview


@pytest.mark.parametrize(
    "elsewhere", [False, True], ids=["of the same service", "of another service"]
)
async def test_a_queue_that_replaces_its_tracks_keeps_the_stream_only_for_the_same_service(
    rig: SlotRig, elsewhere: bool
) -> None:
    """New tracks of the same service get the stream; with others it is free once they start."""
    (playing,) = rig.play("track 0")
    await rig.ask(playing)
    preview = asyncio.create_task(_preview(rig, SOON if elsewhere else BRIEF_WAIT))
    await _turns()
    allow_starts(rig)
    new_track = rig.track("track 9")
    if elsewhere:
        _move_elsewhere(new_track, source_chosen=True)

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        # what the start of a track does: stop the sources in its way, then get its audio
        await rig.queues._abort_superseded_source_buffers(queue_item)
        if not elsewhere:
            await rig.audio.get_audio_buffer(queue_item, reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues._enqueue_with_option(QUEUE_ID, [new_track], QueueOption.REPLACE)

    if elsewhere:
        assert await asyncio.wait_for(preview, SOON)
    else:
        assert rig.fills(rig.buffer(new_track))
        with pytest.raises(ProviderStreamLimitError):
            await preview


@pytest.mark.parametrize("dynamic", [False, True], ids=["tracks", "a dynamic mix"])
async def test_a_paused_queue_that_replaces_its_tracks_keeps_the_stream(
    rig: SlotRig, dynamic: bool
) -> None:
    """A paused speaker that plays an album of the same service keeps its stream at the swap."""
    (playing,) = rig.play("track 0")
    await rig.ask(playing)
    # what a pause command leaves behind; the speaker reports the pause until the album plays
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.queue.state = PlaybackState.PAUSED
    queue_data.halted_by_command = True
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()
    allow_starts(rig)
    new_track = rig.track("track 9")
    served_before_load: list[bool] = []

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        served_before_load.append(preview.done())
        await rig.queues._abort_superseded_source_buffers(queue_item)
        await rig.audio.get_audio_buffer(queue_item, reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    if dynamic:
        assert new_track.media_item is not None
        rig.queues._managed_pool = SimpleNamespace(  # type: ignore[assignment]
            fill=AsyncMock(return_value=[new_track.media_item])
        )
        rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
        rig.queues.update_next_item_on_player = MagicMock()  # type: ignore[method-assign]
        rig.queues.load = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda queue_id, *_args, **_kwargs: rig.queues.update_items(
                queue_id, [new_track]
            )
        )
        await rig.queues._enter_dynamic_mode(QUEUE_ID, QueueOption.REPLACE)
    else:
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_track], QueueOption.REPLACE)

    assert served_before_load == [False]
    assert rig.fills(rig.buffer(new_track))
    with pytest.raises(ProviderStreamLimitError):
        await preview
    assert not queue_data.own_play_action


@pytest.mark.parametrize("dynamic", [False, True], ids=["tracks", "a dynamic mix"])
async def test_a_paused_queue_that_plays_now_keeps_the_stream_its_next_track_held(
    rig: SlotRig, dynamic: bool
) -> None:
    """A "play now" that drops the prepared next track hands its stream to the track it starts."""
    ended = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, ended)
    playing, prepared = rig.play("track 0", "track 1")
    await rig.ask(playing)
    ended.set()
    await rig.delivered(playing)
    await rig.prepare_next(playing)
    assert rig.fills(rig.buffer(prepared))
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.queue.state = PlaybackState.PAUSED
    queue_data.halted_by_command = True
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()
    allow_starts(rig)
    new_track = rig.track("track 9")

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        await rig.queues._abort_superseded_source_buffers(queue_item)
        await rig.audio.get_audio_buffer(queue_item, reason="prepare", capacity_wait_timeout=0)

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    if dynamic:
        assert new_track.media_item is not None
        rig.queues._managed_pool = SimpleNamespace(  # type: ignore[assignment]
            fill=AsyncMock(return_value=[new_track.media_item])
        )
        rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
        rig.queues.update_next_item_on_player = MagicMock()  # type: ignore[method-assign]

        async def _swap(
            queue_id: str, _items: object, insert_at_index: int = 0, **_kwargs: object
        ) -> None:
            kept = rig.queues._queue_data[queue_id].items[:insert_at_index]
            rig.queues.update_items(queue_id, [*kept, new_track])

        rig.queues.load = AsyncMock(side_effect=_swap)  # type: ignore[method-assign]
        await rig.queues._enter_dynamic_mode(QUEUE_ID, QueueOption.PLAY)
    else:
        await rig.queues._enqueue_with_option(QUEUE_ID, [new_track], QueueOption.PLAY)

    assert rig.fills(rig.buffer(new_track))
    with pytest.raises(ProviderStreamLimitError):
        await preview
    assert not queue_data.own_play_action


async def test_a_replace_that_fails_before_it_plays_ends_its_play_action(rig: SlotRig) -> None:
    """A replace that breaks off before its track starts leaves a paused queue paused."""
    (playing,) = rig.play("track 0")
    await rig.ask(playing)
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.queue.state = PlaybackState.PAUSED
    rig.queues.load = AsyncMock(side_effect=RuntimeError("the swap broke off"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 9")], QueueOption.REPLACE)

    assert not queue_data.own_play_action


async def test_a_source_that_fails_keeps_no_stream_for_its_own_track(rig: SlotRig) -> None:
    """A track whose source broke off is not one its queue goes on with."""
    (playing,) = rig.play("track 0")
    breaks_off = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, breaks_off, fails=True)
    await rig.ask(playing)
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()

    breaks_off.set()

    assert await asyncio.wait_for(preview, SOON)


async def test_a_paused_queue_keeps_no_stream_between_two_tracks(rig: SlotRig) -> None:
    """A speaker that starts while a paused queue moves to its next track gets the stream."""
    ended = await _play_through(rig, "track 0", "track 1")
    queue_data = rig.queues._queue_data[QUEUE_ID]
    playing, upcoming = queue_data.items
    queue_data.queue.state = PlaybackState.PAUSED
    # the player fetched the playing track, so the end of its source prepares the next one
    queue_data.last_served_item_id = playing.queue_item_id
    preparing = asyncio.Event()
    may_prepare = asyncio.Event()

    async def _slow_to_load(*_args: object) -> QueueItem:
        preparing.set()
        await may_prepare.wait()
        return upcoming

    rig.queues.load_next_queue_item = AsyncMock(  # type: ignore[method-assign]
        side_effect=_slow_to_load
    )
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    await _source_ends(rig, ended)
    await asyncio.wait_for(preparing.wait(), 1)
    buffer = await rig.ask(other, wait=0)
    may_prepare.set()
    await rig.settle()

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_stream_kept_for_a_queue_is_free_at_once_when_the_queue_is_paused(
    rig: SlotRig,
) -> None:
    """A queue that is paused between two tracks no longer holds anyone back."""
    ended = await _play_through(rig, "track 0", "track 1")
    playing = rig.queues._queue_data[QUEUE_ID].items[0]
    await _source_ends(rig, ended)
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()
    assert not preview.done()
    rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    rig.queues._handle_playback_progress_report = MagicMock()  # type: ignore[method-assign]
    rig.queues._handle_sleep_timer = MagicMock()  # type: ignore[method-assign]
    paused_player: Any = SimpleNamespace(
        player_id=QUEUE_ID,
        resolve_output_protocol=lambda: SimpleNamespace(
            current_media=SimpleNamespace(
                source_id=QUEUE_ID, queue_item_id=playing.queue_item_id, uri=None
            )
        ),
        name="Queue A",
        available=True,
        playback_state=PlaybackState.PAUSED,
        fake_paused=False,
        corrected_elapsed_time=1.0,
        group_members=[],
    )

    # the speaker reports that it paused
    rig.queues._update_queue_from_player(paused_player)

    assert await asyncio.wait_for(preview, SOON)


@pytest.mark.parametrize(
    "source_ends", ["before", "after"], ids=["a source that just ended", "a source that ends then"]
)
async def test_a_queue_that_was_told_to_pause_keeps_no_stream_before_its_speaker_reports_it(
    rig: SlotRig, source_ends: str
) -> None:
    """Between a pause command and the speaker's report of it another speaker can start."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    allow_commands(rig)
    speaker = SimpleNamespace(
        player_id=QUEUE_ID,
        playback_state=PlaybackState.PLAYING,
        active_source=QUEUE_ID,
        extra_data={},
        offline_by_power_control=False,
    )
    rig.mass.players.get_player.side_effect = lambda player_id: (
        speaker if player_id == QUEUE_ID else None
    )
    rig.mass.players._handle_cmd_pause = AsyncMock()
    if source_ends == "before":
        await _source_ends(rig, ended)

    await rig.queues.pause(QUEUE_ID)
    # the speaker took the command and has not reported that it paused
    assert rig.queues._queue_data[QUEUE_ID].queue.state == PlaybackState.PLAYING
    if source_ends == "after":
        await _source_ends(rig, ended)
    buffer = await rig.ask(other, wait=0)

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_pause_that_fails_leaves_the_kept_stream_to_its_queue(rig: SlotRig) -> None:
    """A queue that plays on after a pause that did not go through still gets its next track."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    allow_commands(rig)
    rig.mass.players.get_player.return_value.offline_by_power_control = False
    rig.mass.players._handle_cmd_pause = AsyncMock(side_effect=PlayerCommandFailed("no answer"))
    await _source_ends(rig, ended)

    with pytest.raises(PlayerCommandFailed):
        await rig.queues.pause(QUEUE_ID)

    with pytest.raises(ProviderStreamLimitError):
        await _preview(rig, 0)
    next_capture = await rig.ask(upcoming, wait=0)
    assert next_capture is not None
    assert rig.fills(next_capture)


@pytest.mark.parametrize(
    "source_chosen", [True, False], ids=["their source chosen", "their source not chosen yet"]
)
async def test_a_replace_by_tracks_of_another_service_frees_the_stream_before_they_load(
    rig: SlotRig, source_chosen: bool
) -> None:
    """A queue that leaves a service keeps nothing there while its first new track still loads."""
    (playing,) = rig.play("track 0")
    await rig.ask(playing)
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()
    allow_starts(rig)
    new_track = rig.track("track 9")
    _move_elsewhere(new_track, source_chosen)
    loading = asyncio.Event()
    may_finish = asyncio.Event()

    async def _load(_queue_item: QueueItem, **_kwargs: object) -> None:
        loading.set()
        await may_finish.wait()

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    replacing = asyncio.create_task(
        rig.queues._enqueue_with_option(QUEUE_ID, [new_track], QueueOption.REPLACE)
    )
    await asyncio.wait_for(loading.wait(), SOON)

    try:
        assert await asyncio.wait_for(preview, SOON)
        buffer = await rig.ask(other, wait=0)
        assert buffer is not None
        assert rig.fills(buffer)
    finally:
        may_finish.set()
        await replacing


async def test_a_preview_that_waits_gets_the_stream_after_the_last_track(rig: SlotRig) -> None:
    """After the last track of a queue nothing is kept, so whoever waits is served at once."""
    ended = await _play_through(rig, "track 0")
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()

    await _source_ends(rig, ended)

    assert await asyncio.wait_for(preview, SOON)


@pytest.mark.parametrize(
    "source_chosen", [True, False], ids=["its source chosen", "its source not chosen yet"]
)
async def test_a_preview_that_waits_gets_the_stream_when_the_next_track_is_from_elsewhere(
    rig: SlotRig, source_chosen: bool
) -> None:
    """A queue whose next track comes from another provider keeps nothing on this one."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    _move_elsewhere(upcoming, source_chosen)
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()

    await _source_ends(rig, ended)

    assert await asyncio.wait_for(preview, SOON)


async def test_a_next_track_this_account_no_longer_finds_keeps_no_stream(rig: SlotRig) -> None:
    """A next track known on another account, but no longer found on this one, keeps nothing."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    assert upcoming.media_item is not None
    upcoming.streamdetails = None
    config = MagicMock(instance_id=SIBLING)
    config.name = "Sibling"
    config.get_value.return_value = "GLOBAL"
    sibling = SingleStreamProvider(rig.mass, rig.provider.manifest, config)
    sibling.available = True
    rig.mass.providers = [rig.provider, sibling]
    upcoming.media_item.provider_mappings = {
        ProviderMapping(
            item_id=upcoming.media_item.item_id,
            provider_domain="single",
            provider_instance=instance_id,
            available=instance_id == SIBLING,
            audio_format=PCM_FORMAT,
        )
        for instance_id in (INSTANCE, SIBLING)
    }
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()

    await _source_ends(rig, ended)

    assert await asyncio.wait_for(preview, SOON)


async def test_a_next_track_without_a_source_yet_keeps_the_stream(rig: SlotRig) -> None:
    """A next track with a copy here whose source is not chosen yet can come from here: kept."""
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
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()
    assert not preview.done()

    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)

    assert await asyncio.wait_for(preview, SOON)


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
    """A queue that is stopped between two tracks no longer holds anyone back."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()
    assert not preview.done()

    rig.queues._end_session(rig.queues._queue_data[QUEUE_ID], SESSION_ID)

    # far inside the time the slot would have been kept
    assert await asyncio.wait_for(preview, SOON)
    buffer = await rig.ask(other, wait=0)
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


@pytest.mark.parametrize(
    ("player_filter", "named"),
    [([OTHER_QUEUE_ID, QUEUE_ID], True), ([OTHER_QUEUE_ID], False)],
    ids=["may control the other speaker", "may not control the other speaker"],
)
async def test_a_start_on_another_speaker_is_refused_once_the_kept_stream_is_used(
    rig: SlotRig, player_filter: list[str], named: bool
) -> None:
    """A start between two tracks of another queue is refused when the next track has the stream."""
    ended = await _play_through(rig, "track 0", "track 1")
    upcoming = rig.queues._queue_data[QUEUE_ID].items[1]
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    other_started = count_starts(rig, "track 7")
    # no player objects behind the queues: access is decided on the queue ids
    rig.mass.players.get_player.return_value = None
    await _source_ends(rig, ended)
    set_current_user(
        User(user_id="kid", username="kid", role=UserRole.USER, player_filter=player_filter)
    )
    try:
        asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
        await _turns()
        # not refused for a stream that is only kept
        assert not asking.done()
        await rig.ask(upcoming, wait=0)
        # and refused right after the next track took it, long before its own wait is over
        with pytest.raises(ProviderStreamLimitError) as refusal:
            await asyncio.wait_for(asking, SOON)
    finally:
        set_current_user(None)

    assert isinstance(refusal.value, ProviderStreamHeldError) is named
    if named:
        assert refusal.value.data is not None
        assert refusal.value.data["queue_id"] == QUEUE_ID
    assert other_started == []


async def test_a_start_on_another_speaker_plays_when_the_kept_stream_is_not_used(
    rig: SlotRig,
) -> None:
    """A start between two tracks of another queue waits, and plays when the stream is let go."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)

    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    assert not asking.done()
    rig.provider.end_stream_slot_reservation(QUEUE_ID)
    buffer = await asyncio.wait_for(asking, SOON)

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_start_behind_a_source_without_audio_yet_is_refused_once_it_has_audio(
    rig: SlotRig,
) -> None:
    """A start that finds another queue's source on its way in is told who plays, not kept waiting."""
    (playing,) = rig.play("track 0")
    first_audio = asyncio.Event()

    async def _slow_to_start() -> AsyncGenerator[bytes]:
        await first_audio.wait()
        async for chunk in realtime_capture():
            yield chunk

    rig.sources["track 0"] = _slow_to_start
    starting = asyncio.create_task(rig.ask(playing))
    await _turns()
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    other_started = count_starts(rig, "track 7")

    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    assert not asking.done()
    first_audio.set()
    await starting

    with pytest.raises(ProviderStreamHeldError, match="already playing on Queue A"):
        await asyncio.wait_for(asking, SOON)
    assert other_started == []


async def test_a_start_that_waits_for_a_kept_stream_is_served_before_a_later_preview(
    rig: SlotRig,
) -> None:
    """A start keeps its place in line while the stream is kept, and plays when it is let go."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    preview = asyncio.create_task(_preview(rig, LONG_WAIT))
    await _turns()

    rig.provider.end_stream_slot_reservation(QUEUE_ID)
    buffer = await asyncio.wait_for(asking, SOON)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not preview.done()
    preview.cancel()
    await asyncio.gather(preview, return_exceptions=True)


async def test_a_start_behind_a_source_that_fails_is_served_before_a_later_preview(
    rig: SlotRig,
) -> None:
    """A start that waits for a source without audio yet gets the slot when that source fails."""
    (playing,) = rig.play("track 0")
    breaks_off = asyncio.Event()
    rig.sources["track 0"] = partial(source, 0, 0, breaks_off, fails=True)
    starting = asyncio.create_task(rig.ask(playing))
    await _turns()
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    preview = asyncio.create_task(_preview(rig, LONG_WAIT))
    await _turns()

    breaks_off.set()
    await asyncio.gather(starting, return_exceptions=True)
    buffer = await asyncio.wait_for(asking, SOON)

    assert buffer is not None
    assert rig.fills(buffer)
    assert not preview.done()
    preview.cancel()
    await asyncio.gather(preview, return_exceptions=True)


@pytest.mark.parametrize(
    "source_first", [False, True], ids=["the start asks first", "the source asks first"]
)
async def test_a_start_is_refused_when_the_kept_stream_is_used_in_the_turn_it_asks(
    rig: SlotRig, source_first: bool
) -> None:
    """The next track of the queue the stream is kept for takes it in the turn a start asks."""
    _playing, upcoming = rig.play("track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    async with rig.provider.acquire_stream_slot(0, queue_id=QUEUE_ID, reserve=lambda: True):
        pass
    other_started = count_starts(rig, "track 7")

    if source_first:
        starting = asyncio.create_task(rig.ask(upcoming, wait=0))
        asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    else:
        asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
        starting = asyncio.create_task(rig.ask(upcoming, wait=0))

    assert await starting is not None
    with pytest.raises(ProviderStreamHeldError, match="already playing on Queue A"):
        await asyncio.wait_for(asking, SOON)
    assert other_started == []


async def _start_behind_a_source_that_fails(rig: SlotRig) -> tuple[asyncio.Event, QueueItem]:
    """
    Let queue A's source take the slot without audio yet, and queue B's track wait for it.

    :param rig: The rig to put the queues on.
    :return: The event that lets queue A's source fail, and queue B's track.
    """
    (playing,) = rig.play("track 0")
    breaks_off = asyncio.Event()
    rig.sources["track 0"] = partial(source, 0, 0, breaks_off, fails=True)
    rig.tasks.append(asyncio.create_task(rig.ask(playing)))
    await _turns()
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    return breaks_off, other


async def test_a_start_whose_queue_was_stopped_while_it_waits_holds_nobody_up(
    rig: SlotRig,
) -> None:
    """A start that waits for a slot and is stopped keeps nothing once the slot comes free."""
    breaks_off, other = await _start_behind_a_source_that_fails(rig)
    stopped = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    rig.queues._end_session(rig.queues._queue_data[OTHER_QUEUE_ID], "session-b")
    await _turns()
    (third,) = rig.play("track 8", queue_id=THIRD_QUEUE_ID, session_id="session-c")
    asking = asyncio.create_task(rig.ask(third, wait=LONG_WAIT))
    await _turns()
    preview = asyncio.create_task(_preview(rig, BRIEF_WAIT))
    await _turns()

    breaks_off.set()
    buffer = await asyncio.wait_for(asking, SOON)

    assert await stopped is None
    assert buffer is not None
    assert rig.fills(buffer)
    assert OTHER_QUEUE_ID not in rig.provider.stream_slot_reservations
    preview.cancel()
    await asyncio.gather(preview, return_exceptions=True)


async def test_a_request_whose_player_hung_up_while_it_waits_holds_nobody_up(
    rig: SlotRig,
) -> None:
    """A speaker's request that waits for a slot and is given up keeps nothing for its queue."""
    breaks_off, other = await _start_behind_a_source_that_fails(rig)
    connected = True
    request = asyncio.create_task(
        rig.audio.get_audio_buffer(
            other,
            reason="streaming",
            capacity_wait_timeout=LONG_WAIT,
            consumer_connected=lambda: connected,
        )
    )
    await _turns()
    connected = False
    preview = asyncio.create_task(_preview(rig, SOON))
    await _turns()

    breaks_off.set()

    assert await asyncio.wait_for(preview, SOON)
    assert await request is None
    assert rig.opened["track 7"] == 0


async def test_a_start_is_not_refused_behind_a_source_that_is_given_up_before_its_audio(
    rig: SlotRig,
) -> None:
    """A source that loads and is given up before its first audio refuses nobody that waits."""
    loading, *upcoming = rig.play("track 0", "track 1", "track 2")
    first_audio = asyncio.Event()

    async def _slow_to_start_and_stop() -> AsyncGenerator[bytes]:
        try:
            await first_audio.wait()
            async for chunk in realtime_capture():
                yield chunk
        finally:
            # a connection or process behind a source takes a moment to close
            await asyncio.sleep(0.05)

    rig.sources["track 0"] = _slow_to_start_and_stop
    starting = asyncio.create_task(
        rig.audio.get_audio_buffer(loading, reason="prepare", capacity_wait_timeout=BRIEF_WAIT)
    )
    await _turns()
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    assert not asking.done()

    # the queue moved on to tracks of another service: the loading source is needed no more
    for item in upcoming:
        assert item.streamdetails is not None
        item.streamdetails.provider = ELSEWHERE
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.current_index = queue.index_in_buffer = 1
    rig.queues.release_unneeded_audio(QUEUE_ID)

    assert await starting is None
    buffer = await asyncio.wait_for(asking, SOON)
    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_start_that_is_given_up_while_it_waits_leaves_the_line(rig: SlotRig) -> None:
    """A start that is cancelled while the stream is kept leaves no wait and opens nothing."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()

    asking.cancel()
    await asyncio.gather(asking, return_exceptions=True)
    rig.provider.end_stream_slot_reservation(QUEUE_ID)
    await _turns()

    # the slot that came free is neither taken nor kept for the start that was given up
    assert rig.provider.stream_slot_holders == ()
    assert rig.provider.stream_slot_reservations == ()
    assert rig.opened["track 7"] == 0


async def test_a_start_behind_a_source_that_loads_ahead_waits_in_line_for_the_slot(
    rig: SlotRig,
) -> None:
    """Only a source at playback pace can end in a refusal, so no other one is waited out first."""
    (playing,) = rig.play("track 0", realtime=False)
    first_audio = asyncio.Event()
    rig.sources["track 0"] = partial(source, 0, 1, first_audio)
    starting = asyncio.create_task(rig.ask(playing))
    await _turns()
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    asking = asyncio.create_task(rig.ask(other, wait=LONG_WAIT))
    await _turns()
    preview = asyncio.create_task(_preview(rig, LONG_WAIT))
    await _turns()

    first_audio.set()
    await starting
    buffer = await asyncio.wait_for(asking, SOON)

    # the start asked before the preview did, and is served before it
    assert buffer is not None
    assert rig.fills(buffer)
    assert not preview.done()
    preview.cancel()
    await asyncio.gather(preview, return_exceptions=True)


async def test_a_track_another_queue_prepares_waits_while_a_stream_is_kept(rig: SlotRig) -> None:
    """A track that is prepared ahead of time waits for the slot instead of being refused."""
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)
    loop = asyncio.get_running_loop()

    asked = loop.time()
    with pytest.raises(ProviderStreamLimitError) as refusal:
        await rig.audio.get_audio_buffer(
            other,
            reason="prepare_next",
            capacity_wait_timeout=BRIEF_WAIT,
            allow_provider_match=False,
            take_over_slots=False,
        )

    assert not isinstance(refusal.value, ProviderStreamHeldError)
    assert loop.time() - asked >= BRIEF_WAIT * 0.9


async def test_another_queue_starts_once_a_kept_stream_was_not_used(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue that asks between two tracks starts within its wait when the next one never asks."""
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.2)
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")

    await _source_ends(rig, ended)
    buffer = await rig.ask(other, wait=LONG_WAIT)

    assert buffer is not None
    assert rig.fills(buffer)


async def test_a_stream_kept_for_a_queue_that_was_removed_refuses_nobody(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start behind what is kept for a removed queue plays once that has lapsed."""
    monkeypatch.setattr(music_provider_mod, "STREAM_SLOT_RESERVATION", 0.2)
    ended = await _play_through(rig, "track 0", "track 1")
    (other,) = rig.play("track 7", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    await _source_ends(rig, ended)

    # its player was removed between two tracks
    rig.queues._queue_data.pop(QUEUE_ID)
    buffer = await rig.ask(other, wait=LONG_WAIT)

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
    # a start that may wait does not wait for the slot that is kept either
    buffer = await asyncio.wait_for(rig.ask(other, wait=LONG_WAIT), SOON)

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
