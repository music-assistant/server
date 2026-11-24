"""
Tests for the audio a queue keeps and the audio it gives up.

A queue keeps the audio of the track it plays, of the track that plays next, of a track one of
its streams decided to play and all of the audio of the track before. A buffer that a stream
still reads is kept as well. Everything else is given up the moment it leaves that scope, on
every provider and whether its source still fills or has delivered everything.
"""

from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    CrossfadeMode,
    PlaybackState,
    QueueOption,
    RepeatMode,
    SleepTimerMode,
)
from music_assistant_models.errors import AudioError, PlayerUnavailableError

import music_assistant.controllers.streams.audio as audio_mod
import music_assistant.controllers.streams.audio_buffer as buffer_mod
from music_assistant.controllers.player_queues.state import SleepTimerTarget
from tests.controllers.streams.stream_slot_rig import (
    OTHER_QUEUE_ID,
    PCM_FORMAT,
    QUEUE_ID,
    SESSION_ID,
    SingleStreamProvider,
    SlotRig,
    TwoStreamProvider,
    UnlimitedProvider,
    allow_commands,
    allow_starts,
    count_starts,
    drain,
    given_up,
    install_mixer,
    source,
)

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem


EVERY_PROVIDER = pytest.mark.parametrize(
    "rig",
    [SingleStreamProvider, TwoStreamProvider, UnlimitedProvider],
    ids=["one slot", "a slot to spare", "no limit"],
    indirect=True,
)
NO_LIMIT = pytest.mark.parametrize("rig", [UnlimitedProvider], indirect=True)


def _move_to(rig: SlotRig, index: int) -> None:
    """Put the rig's queue on another track, as the test's starting point."""
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.queue.current_index = queue_data.queue.index_in_buffer = index
    queue_data.queue.current_item = queue_data.items[index]


async def _edit(rig: SlotRig, edit: str, prepared: QueueItem) -> None:
    """Change the queue so that the prepared track no longer plays next."""
    if edit == "replace next":
        await rig.queues._enqueue_with_option(
            QUEUE_ID, [rig.track("track 5")], QueueOption.REPLACE_NEXT
        )
    elif edit == "play next":
        await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], QueueOption.NEXT)
    elif edit == "remove":
        rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    else:
        rig.queues.move_item(QUEUE_ID, prepared.queue_item_id, pos_shift=1)


# --- a change of the queue


@EVERY_PROVIDER
@pytest.mark.parametrize("edit", ["replace next", "play next", "remove", "move"])
@pytest.mark.parametrize("complete", [False, True], ids=["still filling", "fully loaded"])
async def test_a_prepared_track_that_no_longer_plays_next_gives_its_audio_up_at_the_change(
    rig: SlotRig, edit: str, complete: bool
) -> None:
    """A queue change releases the prepared track at once, on every provider, loaded or not."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2", realtime=not complete)
    if complete:
        rig.sources["track 1"] = partial(source, 30)
    capture = await rig.prepare_next(playing)
    if complete:
        await rig.delivered(prepared)
        assert capture.seconds_available == 30

    await _edit(rig, edit, prepared)

    assert await given_up(capture)
    assert not rig.fills(capture)


@NO_LIMIT
async def test_a_change_that_keeps_the_next_track_releases_nothing(rig: SlotRig) -> None:
    """Items added behind the prepared track leave its audio, loaded or not, where it is."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    rig.sources["track 0"] = partial(source, 30)
    rig.sources["track 1"] = partial(source, 30)
    played = await rig.ask(playing)
    await rig.delivered(playing)
    capture = await rig.prepare_next(playing)
    await rig.delivered(prepared)

    await rig.queues._enqueue_with_option(QUEUE_ID, [rig.track("track 5")], QueueOption.ADD)
    await rig.settle()

    assert played is not None
    assert not played.cancelled
    assert not capture.cancelled
    assert capture.seconds_available == 30


@NO_LIMIT
async def test_a_track_a_stream_decided_to_play_keeps_its_audio_until_that_stream_lets_go(
    rig: SlotRig,
) -> None:
    """A track a stream took to play survives its removal, and goes when the stream ends."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    claim = audio_mod._ItemClaim("flow stream of Queue A", SESSION_ID)
    rig.audio._claim_item(claim, prepared)

    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    await rig.settle()
    assert rig.fills(capture)
    rig.audio._claim_item(claim, None)

    assert await given_up(capture)


# --- a buffer that another one replaces


@NO_LIMIT
async def test_a_replaced_buffer_that_is_read_goes_when_its_reader_leaves(rig: SlotRig) -> None:
    """A stream keeps the buffer it reads when another one takes its place on the item."""
    (track,) = rig.play("track 0")
    first = await rig.ask(track)
    assert first is not None
    reader = first.get_raw_stream()
    await anext(reader)

    second = await rig.audio.get_audio_buffer(track, seek_position_ms=60_000, reason="prepare")

    assert second is not first
    assert not first.cancelled
    await anext(reader)
    await reader.aclose()
    assert await given_up(first)
    assert second is not None
    assert rig.fills(second)


@pytest.mark.parametrize(
    "rig",
    [TwoStreamProvider, UnlimitedProvider],
    ids=["a slot to spare", "no limit"],
    indirect=True,
)
async def test_a_replaced_buffer_nobody_reads_goes_at_once(rig: SlotRig) -> None:
    """A buffer nobody reads gives its audio up the moment another one takes its place."""
    (track,) = rig.play("track 0")
    first = await rig.ask(track)
    assert first is not None

    second = await rig.audio.get_audio_buffer(track, seek_position_ms=60_000, reason="prepare")

    assert await given_up(first)
    assert second is not None
    assert rig.fills(second)


@NO_LIMIT
async def test_a_replaced_buffer_whose_source_broke_off_long_ago_goes_at_once(
    rig: SlotRig,
) -> None:
    """Audio a failed source left behind is given up when the track gets a new buffer."""
    (track,) = rig.play("track 0")
    breaks_off = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, breaks_off, fails=True)
    failed = await rig.ask(track)
    assert failed is not None
    breaks_off.set()
    while not failed.has_error:
        await asyncio.sleep(0)
    assert failed.seconds_available == 3
    failed._last_access_time -= 3600
    rig.sources["track 0"] = partial(source, 3)

    second = await rig.ask(track)
    await rig.settle()

    assert second is not None
    assert second is not failed
    assert failed.cancelled
    assert failed.seconds_available == 0
    assert not rig.fills(failed)


# --- no source is opened for a track that left the scope


async def test_a_track_removed_while_its_format_is_probed_gets_no_buffer(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue change during the wait before a buffer is made leaves nothing to fetch."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    probing = asyncio.Event()
    probed = asyncio.Event()

    async def _probe(*_args: object) -> None:
        probing.set()
        await probed.wait()

    monkeypatch.setattr(buffer_mod, "_needs_format_probe", lambda _details: True)
    monkeypatch.setattr(buffer_mod, "_probe_source_format", _probe)
    rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    await asyncio.wait_for(probing.wait(), 2)

    await _edit(rig, "replace next", prepared)
    probed.set()
    await rig.settle()

    assert prepared.streamdetails is not None
    assert prepared.streamdetails.buffer is None
    assert rig.opened["track 1"] == 0


async def test_a_track_removed_while_its_probe_waits_for_a_slot_is_not_probed(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A format probe that got its slot after the track left the queue opens no source."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    ended = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, ended)
    await rig.ask(playing)
    assert not rig.slot_free()
    assert prepared.streamdetails is not None
    prepared.streamdetails.path = "http://source.invalid/track-1"
    open_source = AsyncMock(return_value=(["http://source.invalid/track-1"], []))
    rig.audio.open_local_files = open_source  # type: ignore[method-assign]
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(buffer_mod, "_needs_format_probe", lambda _details: True)
    monkeypatch.setattr(buffer_mod, "probe_audio_stream", probe)
    rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    await asyncio.sleep(0.05)
    assert prepared.streamdetails.buffer is None

    await _edit(rig, "replace next", prepared)
    ended.set()
    await rig.settle()

    open_source.assert_not_awaited()
    probe.assert_not_awaited()
    assert prepared.streamdetails.buffer is None


async def test_a_track_removed_while_its_source_waits_for_a_slot_is_let_go_at_the_change(
    rig: SlotRig,
) -> None:
    """A preparation that only waits keeps no buffer: its wait ends and no source opens."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    ended = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, ended)
    started = count_starts(rig, "track 1")
    await rig.ask(playing)
    preparation = rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    assert preparation is not None
    await asyncio.sleep(0.05)
    waiting = rig.buffer(prepared)
    assert list(waiting.users) == ["prepare_next"]

    await _edit(rig, "replace next", prepared)

    assert await given_up(waiting)
    await asyncio.wait_for(preparation, 1)
    ended.set()
    await rig.settle()
    assert started == []


async def test_a_wait_for_audio_that_is_given_up_meanwhile_gets_nothing(rig: SlotRig) -> None:
    """Whoever only waited for audio that left the scope is told there is none."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2", realtime=False)
    ended = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, ended)
    await rig.ask(playing)
    waiting = asyncio.create_task(
        rig.audio.get_audio_buffer(prepared, reason="prepare_next", capacity_wait_timeout=5)
    )
    await asyncio.sleep(0.05)
    assert not waiting.done()

    await _edit(rig, "replace next", prepared)

    assert await asyncio.wait_for(waiting, 1) is None
    ended.set()
    await rig.settle()


async def test_a_source_that_gets_its_slot_after_its_track_left_is_not_opened(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is asked again once it has its slot, so a change nothing noticed costs nothing."""
    playing, prepared, later = rig.play("track 0", "track 1", "track 2", realtime=False)
    ended = asyncio.Event()
    rig.sources["track 0"] = partial(source, 3, 0, ended)
    started = count_starts(rig, "track 1")
    arrived: list[str] = []
    monkeypatch.setattr(
        rig.queues, "track_fully_buffered", lambda _queue_id, item_id: arrived.append(item_id)
    )
    await rig.ask(playing)
    rig.queues.prepare_next_audio_buffer(QUEUE_ID, playing.queue_item_id)
    await asyncio.sleep(0.05)
    waiting = rig.buffer(prepared)
    # the track leaves the queue behind the queue's back
    rig.queues._queue_data[QUEUE_ID].items = [playing, later]
    assert not await given_up(waiting)

    ended.set()
    await rig.settle()

    assert started == []
    # only the track that played reports that all of its audio arrived
    assert arrived == [playing.queue_item_id]
    assert await given_up(waiting)
    assert rig.slot_free()


@NO_LIMIT
async def test_audio_that_left_the_scope_unnoticed_goes_before_the_queue_gets_other_audio(
    rig: SlotRig,
) -> None:
    """Whatever left the scope is given up first when the queue asks for audio."""
    playing, prepared, later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    # the tracks swap places behind the queue's back
    rig.queues._queue_data[QUEUE_ID].items = [playing, later, prepared]
    assert rig.fills(capture)

    buffer = await rig.ask(later)

    assert buffer is not None
    assert await given_up(capture)


# --- a queue that does not play


@EVERY_PROVIDER
async def test_a_queue_without_a_playback_session_gets_no_audio(rig: SlotRig) -> None:
    """Nothing is fetched for a queue that is not playing, not for the track it is on either."""
    playing, _upcoming = rig.play("track 0", "track 1")
    rig.queues._queue_data[QUEUE_ID].session_id = None

    buffer = await rig.audio.get_audio_buffer(playing, reason="prepare_next")

    assert buffer is None
    assert rig.opened["track 0"] == 0


async def test_a_request_that_outlives_its_queue_gets_no_audio(rig: SlotRig) -> None:
    """A speaker that still asks for a track of a queue that is gone is served nothing."""
    playing, _upcoming = rig.play("track 0", "track 1")
    del rig.queues._queue_data[QUEUE_ID]

    buffer = await rig.ask(playing)

    assert buffer is None
    assert rig.opened["track 0"] == 0


@NO_LIMIT
async def test_the_end_of_a_session_releases_a_removed_track_a_stream_had_decided_on(
    rig: SlotRig,
) -> None:
    """When playback ends, a track off the queue that only a stream of it held goes at once."""
    playing, prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    claim = audio_mod._ItemClaim("flow stream of Queue A", SESSION_ID)
    rig.audio._claim_item(claim, prepared)
    rig.queues.delete_item(QUEUE_ID, prepared.queue_item_id)
    await rig.settle()
    assert rig.fills(capture)

    rig.queues._end_session(rig.queues._queue_data[QUEUE_ID], SESSION_ID)

    assert await given_up(capture)
    rig.audio._claim_item(claim, None)
    await rig.settle()


# --- the position of the player


@NO_LIMIT
async def test_a_player_that_jumps_back_lets_go_of_the_track_before_the_one_it_left(
    rig: SlotRig,
) -> None:
    """The fully loaded track before the playing one is kept only while it sits there."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3", "track 4", realtime=False)
    _move_to(rig, 3)
    rig.sources["track 3"] = partial(source, 5)
    before = await rig.ask(tracks[3])
    assert before is not None
    await rig.delivered(tracks[3])
    _move_to(rig, 4)
    rig.queues.release_unneeded_audio(QUEUE_ID)
    assert not before.cancelled

    # the player starts fetching the first track by itself
    rig.queues.track_loaded_in_buffer(QUEUE_ID, tracks[0].queue_item_id)

    assert await given_up(before)
    await rig.settle()


@NO_LIMIT
async def test_a_player_that_fetches_the_next_track_lets_go_of_the_track_two_back(
    rig: SlotRig,
) -> None:
    """Of the tracks behind the one the player fetches, only the last keeps its audio."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3", realtime=False)
    for name in ("track 0", "track 1"):
        rig.sources[name] = partial(source, 5)
    two_back = await rig.ask(tracks[0])
    assert two_back is not None
    await rig.delivered(tracks[0])
    _move_to(rig, 1)
    before = await rig.ask(tracks[1])
    assert before is not None
    await rig.delivered(tracks[1])
    assert not two_back.cancelled

    rig.queues.track_loaded_in_buffer(QUEUE_ID, tracks[2].queue_item_id)

    assert await given_up(two_back)
    assert not before.cancelled
    assert before.seconds_available == 5
    await rig.settle()


@NO_LIMIT
async def test_a_track_two_back_that_is_still_read_keeps_its_audio_until_its_reader_leaves(
    rig: SlotRig,
) -> None:
    """A stream that still reads a track the player moved on from is not cut off."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3", realtime=False)
    rig.sources["track 0"] = partial(source, 5)
    two_back = await rig.ask(tracks[0])
    assert two_back is not None
    await rig.delivered(tracks[0])
    reader = two_back.get_raw_stream()
    await anext(reader)
    _move_to(rig, 1)

    rig.queues.track_loaded_in_buffer(QUEUE_ID, tracks[2].queue_item_id)
    await rig.settle()

    assert not two_back.cancelled
    await anext(reader)
    await reader.aclose()
    assert await given_up(two_back)


@NO_LIMIT
async def test_a_track_whose_source_still_fills_goes_when_the_player_reports_the_next_one(
    rig: SlotRig,
) -> None:
    """A track the player moved on from keeps only audio that has fully arrived."""
    playing, upcoming, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.ask(playing)
    assert capture is not None
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.queue.index_in_buffer = 1
    rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    rig.queues._handle_playback_progress_report = MagicMock()  # type: ignore[method-assign]
    rig.queues._handle_sleep_timer = MagicMock()  # type: ignore[method-assign]
    player: Any = SimpleNamespace(
        player_id=QUEUE_ID,
        resolve_output_protocol=lambda: SimpleNamespace(
            current_media=SimpleNamespace(
                source_id=QUEUE_ID, queue_item_id=upcoming.queue_item_id, uri=None
            )
        ),
        name="Queue A",
        available=True,
        playback_state=PlaybackState.PLAYING,
        corrected_elapsed_time=1.0,
        group_members=[],
    )

    rig.queues._update_queue_from_player(player)

    assert queue_data.queue.current_index == 1
    assert await given_up(capture)


@NO_LIMIT
async def test_a_fade_into_the_next_track_lets_go_of_the_track_before_the_playing_one(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a stream takes the next track for its fade, the track two back is out of scope."""
    before, playing, incoming = rig.play("track 0", "track 1", "track 2", realtime=False)
    for name in ("track 0", "track 1", "track 2"):
        rig.sources[name] = partial(source, 30)
    earlier = await rig.ask(before)
    assert earlier is not None
    await rig.delivered(before)
    _move_to(rig, 1)
    install_mixer(rig, monkeypatch)
    await rig.ask(playing)
    await rig.delivered(playing)
    rig.audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    assert not earlier.cancelled

    await drain(
        rig.audio.get_queue_item_stream_with_smartfade(
            MagicMock(), playing, PCM_FORMAT, CrossfadeMode.STANDARD_CROSSFADE, 8, SESSION_ID
        )
    )

    assert rig.queues._queue_data[QUEUE_ID].queue.index_in_buffer == 2
    assert await given_up(earlier)
    assert not rig.buffer(incoming).cancelled
    rig.audio.clear_crossfade_handover(QUEUE_ID)
    await rig.settle()


@NO_LIMIT
async def test_a_fade_into_a_track_that_was_already_prepared_lets_go_of_the_track_before(
    rig: SlotRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fade that takes a next track whose audio is in hand releases as one that loads it."""
    before, playing, incoming = rig.play("track 0", "track 1", "track 2", realtime=False)
    for name in ("track 0", "track 1", "track 2"):
        rig.sources[name] = partial(source, 30)
    earlier = await rig.ask(before)
    assert earlier is not None
    await rig.delivered(before)
    _move_to(rig, 1)
    await rig.ask(playing)
    await rig.delivered(playing)
    prepared = await rig.prepare_next(playing)
    await rig.delivered(incoming)
    install_mixer(rig, monkeypatch)
    rig.audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    rig.audio.select_pcm_format = AsyncMock(return_value=PCM_FORMAT)  # type: ignore[method-assign]
    assert not earlier.cancelled

    await drain(
        rig.audio.get_queue_item_stream_with_smartfade(
            MagicMock(), playing, PCM_FORMAT, CrossfadeMode.STANDARD_CROSSFADE, 8, SESSION_ID
        )
    )

    assert rig.queues._queue_data[QUEUE_ID].queue.index_in_buffer == 2
    # the fade read the audio that was there: no source was opened a second time
    assert rig.opened["track 2"] == 1
    assert rig.buffer(incoming) is prepared
    assert await given_up(earlier)
    rig.audio.clear_crossfade_handover(QUEUE_ID)
    await rig.settle()


# --- what plays next changes while the queue and its position stay


@NO_LIMIT
async def test_repeating_the_playing_track_releases_the_track_prepared_to_follow_it(
    rig: SlotRig,
) -> None:
    """Repeat one makes the playing track its own successor, so the prepared one goes."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)
    allow_commands(rig)

    await rig.queues.set_repeat(QUEUE_ID, RepeatMode.ONE)

    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is playing
    assert await given_up(capture)


@NO_LIMIT
async def test_a_sleep_timer_at_the_end_of_the_track_releases_the_prepared_track(
    rig: SlotRig,
) -> None:
    """Nothing follows a track a sleep timer stops at, so the prepared track goes."""
    playing, _prepared, _later = rig.play("track 0", "track 1", "track 2")
    capture = await rig.prepare_next(playing)

    rig.queues.set_sleep_timer_target(QUEUE_ID, SleepTimerMode.END_OF_ITEM)

    assert rig.queues.get_next_item(QUEUE_ID, playing.queue_item_id) is None
    assert await given_up(capture)


@NO_LIMIT
async def test_a_sleep_timer_that_moves_to_the_playing_track_releases_the_prepared_track(
    rig: SlotRig,
) -> None:
    """A sleep timer that follows the queue to another track stops what follows that one."""
    earlier, playing, _later = rig.play("track 0", "track 1", "track 2", current=1)
    queue_data = rig.queues._queue_data[QUEUE_ID]
    queue_data.sleep_timer_target = SleepTimerTarget(
        SleepTimerMode.END_OF_ITEM, earlier.queue_item_id, 198
    )
    capture = await rig.prepare_next(playing)
    assert rig.fills(capture)

    rig.queues._repin_sleep_timer(QUEUE_ID)

    assert queue_data.sleep_timer_target.queue_item_id == playing.queue_item_id
    assert await given_up(capture)


# --- the start of a track


@NO_LIMIT
async def test_a_start_releases_what_only_a_stream_of_the_playback_before_it_held(
    rig: SlotRig,
) -> None:
    """A track that only a stream of the earlier playback had decided on goes at a new start."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3", "track 4")
    claim = audio_mod._ItemClaim("stream of the playback before", SESSION_ID)
    rig.audio._claim_item(claim, tracks[3])
    held = await rig.audio.get_audio_buffer(tracks[3], reason="streaming")
    assert held is not None
    assert rig.fills(held)
    allow_starts(rig)
    rig.queues._load_item = AsyncMock()  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 0)

    assert rig.queues._queue_data[QUEUE_ID].session_id != SESSION_ID
    assert await given_up(held)
    rig.audio._claim_item(claim, None)


async def test_a_start_that_skips_failing_tracks_wants_the_audio_of_the_track_it_tries(
    rig: SlotRig,
) -> None:
    """Every track a start tries, also after two that failed, is one the queue wants audio for."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3", "track 4")
    allow_starts(rig)
    tried: list[tuple[str, bool]] = []

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        tried.append((queue_item.name, rig.queues.wants_audio(queue_item)))
        if len(tried) < 3:
            raise AudioError("the source broke off")

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]

    await rig.queues.play_index(QUEUE_ID, 1)

    assert tried == [("track 1", True), ("track 2", True), ("track 3", True)]
    assert rig.queues._queue_data[QUEUE_ID].queue.current_item is tracks[3]


async def test_a_start_its_player_refuses_lets_go_of_the_audio_it_prepared(rig: SlotRig) -> None:
    """A start the player refuses ends its session, so another queue gets the stream at once."""
    tracks = rig.play("track 0", "track 1", "track 2")
    allow_starts(rig)

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        await rig.audio.get_audio_buffer(queue_item, reason="prepare")

    rig.queues._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    rig.mass.players.play_media.side_effect = PlayerUnavailableError("no member can play")

    with pytest.raises(PlayerUnavailableError):
        await rig.queues.play_index(QUEUE_ID, 1)
    await rig.settle()

    queue_data = rig.queues._queue_data[QUEUE_ID]
    assert queue_data.session_id is None
    assert queue_data.items == tracks
    other = rig.play("track 5", queue_id=OTHER_QUEUE_ID, session_id="session-b")
    assert rig.fills(await rig.capture(other[0]))
