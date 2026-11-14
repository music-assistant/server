"""A queue that was stopped did not end, and the check of the end of a queue has one playback."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import EventType, PlaybackState, QueueOption, SleepTimerMode
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import Playlist

from music_assistant.controllers.player_queues.constants import PLAYBACK_START_TIMEOUT
from music_assistant.controllers.players.constants import PLAYER_LOCK_TIMEOUT

from .harness import TEST_TRACK_URI
from .test_paused_queue import (
    OTHER_TRACK_URI,
    PLAYED_SECONDS,
    SETTLE_SECONDS,
    SOURCE_DELAY,
    TRACK_SECONDS,
    Position,
    _fetch_the_flow_stream,
    _forget_position,
    _pause_by_stop,
    _play_another_track,
    _play_in_flow_mode,
    _play_out,
    _play_the_first_item,
    _position,
    _resume,
    _slow_music_source,
    _speaker,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from music_assistant_models.media_items import Track
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue

    from .harness import FakeProtocol, Scenario

# seconds after the play command at which a slow speaker reports that it plays
LATE_REPORT_SECONDS = 11


def _queue(scenario: Scenario, player_id: str) -> PlayerQueue:
    """Return the queue of a player."""
    queue = scenario.queue(player_id)
    assert queue is not None
    return queue


async def _play_track_by_track(scenario: Scenario, speaker: FakeProtocol, player_id: str) -> None:
    """
    Play a track on the player as a stream of its own, which the speaker plays from its start.

    No fake protocol fetches audio, so this books what the stream server books when it serves
    the item.
    """
    await scenario.play(player_id)
    queue = _queue(scenario, player_id)
    assert queue.current_item is not None
    assert not queue.flow_mode
    scenario.mass.player_queues.mark_item_served(player_id, queue.current_item.queue_item_id)
    await scenario.report(speaker, elapsed_time=0, elapsed_time_last_updated=scenario.wall_time())


async def _stop(scenario: Scenario, speaker: FakeProtocol, player_id: str) -> None:
    """Stop a player as a client does."""
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.settle()
    await _forget_position(scenario, speaker)


def _plays_from_its_start(
    scenario: Scenario, speaker: FakeProtocol, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let the speaker report the start of every stream it is handed as its position."""

    async def _play_media(media: PlayerMedia) -> None:
        speaker._apply(
            "play_media",
            media,
            playback_state=PlaybackState.PLAYING,
            current_media=media,
            elapsed_time=0,
            elapsed_time_last_updated=scenario.wall_time(),
        )

    monkeypatch.setattr(speaker, "play_media", _play_media)


class _DynamicSource:
    """The dynamic playlist a queue was started from, and what it hands back when asked."""

    def __init__(
        self,
        scenario: Scenario,
        player_id: str,
        tracks: list[Track],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Make the queue of a player one that was started from a dynamic playlist.

        :param scenario: The scenario the queue belongs to.
        :param player_id: ID of the player, which is the id of its queue.
        :param tracks: The tracks the playlist hands back when the queue asks for more.
        :param monkeypatch: Puts the playlist in the place of the music provider's.
        """
        self.fetches = 0
        # set to let a fetch return; a fetch waits for it
        self.release = asyncio.Event()
        self.release.set()
        self._tracks = tracks
        queues = scenario.mass.player_queues
        queues.queue_data(player_id).source_items = [
            Playlist(
                item_id="mix",
                provider="test",
                name="Mix",
                is_dynamic=True,
                provider_mappings=set(),
            )
        ]
        monkeypatch.setattr(queues._media_resolver, "get_dynamic_source_tracks", self._fetch)

    async def _fetch(self, *_args: Any, **_kwargs: Any) -> list[Track]:
        self.fetches += 1
        await self.release.wait()
        return self._tracks


async def _other_track(scenario: Scenario) -> Track:
    """Return another track of the test music provider than the one play() plays."""
    return cast("Track", await scenario.mass.music.get_item_by_uri(OTHER_TRACK_URI))


@pytest.mark.parametrize("paused_first", [False, True], ids=["playing", "paused"])
@pytest.mark.parametrize("can_pause", [False, True], ids=["stops to pause", "has a pause"])
async def test_a_stop_is_not_the_end_of_its_queue(
    scenario: Scenario, can_pause: bool, paused_first: bool
) -> None:
    """
    A queue stopped on its last track keeps its place, also when it was paused before.

    :param can_pause: Whether the speaker has a pause of its own.
    :param paused_first: Whether the queue is paused before it is stopped.
    """
    speaker, player_id = await _speaker(scenario, can_pause=can_pause)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    if paused_first:
        await scenario.mass.players.cmd_pause(player_id)
        await scenario.advance(1)

    await _stop(scenario, speaker, player_id)

    stopped = _position(scenario, player_id)
    assert stopped.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)
    assert stopped == Position(
        state=PlaybackState.IDLE,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=stopped.elapsed_time,
        resume_pos=PLAYED_SECONDS,
    )
    for seconds in (SETTLE_SECONDS, 1, 60):
        await scenario.advance(seconds)
        assert _position(scenario, player_id) == stopped

    await scenario.mass.player_queues.resume(player_id)
    await scenario.settle()
    assert _position(scenario, player_id) == Position(
        state=PlaybackState.PLAYING,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=PLAYED_SECONDS,
        resume_pos=0,
    )


async def test_a_stop_does_not_refill_a_queue_from_its_dynamic_source(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new music starts after a stop on the last track of a queue with a dynamic source."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    source = _DynamicSource(scenario, player_id, [await _other_track(scenario)], monkeypatch)
    await scenario.advance(PLAYED_SECONDS)

    await _stop(scenario, speaker, player_id)
    stopped = _position(scenario, player_id)
    speaker.commands.clear()
    await scenario.advance(SETTLE_SECONDS + 1)

    assert source.fetches == 0
    assert speaker.commands == []
    assert _position(scenario, player_id) == stopped
    assert not stopped.ended


@pytest.mark.parametrize("native_pause", [False, True], ids=["stops to pause", "has a pause"])
@pytest.mark.parametrize("discard", ["clear", "clear behind a command", "transfer"])
async def test_a_queue_whose_content_went_is_not_one_a_command_halted(
    scenario: Scenario, discard: str, native_pause: bool
) -> None:
    """
    A paused queue that is cleared or moved to another player has no halted playback left.

    :param discard: How the content of the queue goes; a clear can come while another command
        holds the player, so the stop it makes of a paused speaker waits for that one.
    :param native_pause: Whether the speaker has a pause of its own, and so is still paused.
    """
    speaker, player_id = await _speaker(scenario, can_pause=native_pause)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.advance(1)
    queues = scenario.mass.player_queues
    assert queues.queue_data(player_id).halted_by_command

    if discard == "clear":
        queues.clear(player_id)
    elif discard == "clear behind a command":
        async with scenario.mass.players.get_group_and_player_lock(player_id):
            queues.clear(player_id)
            await scenario.settle()
    else:
        target = scenario.provider("sendspin").protocol("sendspin-hall", "Hall")
        await scenario.register(target)
        await scenario.idle()
        target_id = scenario.single_player(target).player_id
        await queues.transfer_queue(player_id, target_id, auto_play=False)
        assert not queues.queue_data(target_id).halted_by_command
    await scenario.advance(SETTLE_SECONDS + 1)

    assert queues.queue_data(player_id).items == []
    assert not queues.queue_data(player_id).halted_by_command


@pytest.mark.parametrize("halt", ["pause", "stop"])
@pytest.mark.parametrize("seconds_idle", [0.2, 1.2, 2.2, 3.2, 4.2])
async def test_a_queue_halted_in_a_new_playback_is_not_ended_for_the_one_before(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, seconds_idle: float, halt: str
) -> None:
    """
    A queue that played out, plays again and is paused or stopped keeps the place it has then.

    :param seconds_idle: Seconds the queue was idle at its end before it plays again.
    :param halt: The command that halts the new playback.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    _plays_from_its_start(scenario, speaker, monkeypatch)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(seconds_idle)

    await scenario.mass.player_queues.play_index(player_id, 0, seek_position=PLAYED_SECONDS)
    await scenario.settle()
    assert _position(scenario, player_id).state == PlaybackState.PLAYING
    if halt == "pause":
        await _pause_by_stop(scenario, speaker, player_id)
    else:
        await _stop(scenario, speaker, player_id)

    halted = _position(scenario, player_id)
    assert halted == Position(
        state=PlaybackState.IDLE,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=PLAYED_SECONDS,
        resume_pos=PLAYED_SECONDS,
    )
    await scenario.advance(SETTLE_SECONDS + 2)
    assert _position(scenario, player_id) == halted


@pytest.mark.parametrize("dynamic", [False, True], ids=["plain", "dynamic source"])
async def test_a_play_stopped_before_its_speaker_plays_is_not_ended_for_the_playback_before(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, dynamic: bool
) -> None:
    """
    A queue that played out, is played again and stopped before its speaker plays is left.

    :param dynamic: Whether the queue was started from a dynamic playlist that has more music.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    source = (
        _DynamicSource(scenario, player_id, [await _other_track(scenario)], monkeypatch)
        if dynamic
        else None
    )
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)

    async def _take_the_stream(media: PlayerMedia) -> None:
        speaker._apply("play_media", media)

    monkeypatch.setattr(speaker, "play_media", _take_the_stream)
    started = await scenario.command(
        scenario.mass.player_queues.play_index(player_id, 0, seek_position=PLAYED_SECONDS)
    )
    # the stop waits for the play ahead of the check of the playback before, so that check
    # next reads a queue whose new playback is over already
    await scenario.advance(0.25)
    stopped = await scenario.command(scenario.mass.player_queues.stop(player_id))
    await scenario.advance(PLAYBACK_START_TIMEOUT)
    assert started.done()
    await started
    assert stopped.done()
    await stopped
    speaker.commands.clear()
    items = _queue(scenario, player_id).items

    await scenario.advance(2 * SETTLE_SECONDS)
    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.current_index) == (
        PlaybackState.IDLE,
        False,
        0,
    )
    assert _queue(scenario, player_id).items == items
    assert speaker.commands == []
    assert source is None or source.fetches == 0


@pytest.mark.parametrize("fetched", [True, False], ids=["tracks", "nothing"])
async def test_a_queue_paused_while_its_dynamic_source_is_fetched_stays_paused(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, fetched: bool
) -> None:
    """
    Whatever a dynamic source hands back, a queue that plays again and is paused by then is left.

    :param fetched: Whether the dynamic source hands back a track.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    _plays_from_its_start(scenario, speaker, monkeypatch)
    await _play_in_flow_mode(scenario, speaker, player_id)
    tracks = [await _other_track(scenario)] if fetched else []
    source = _DynamicSource(scenario, player_id, tracks, monkeypatch)
    source.release.clear()
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(SETTLE_SECONDS + 0.5)
    assert source.fetches == 1

    await scenario.mass.player_queues.play_index(player_id, 0, seek_position=PLAYED_SECONDS)
    await scenario.settle()
    await _pause_by_stop(scenario, speaker, player_id)
    paused = _position(scenario, player_id)
    assert (paused.state, paused.ended, paused.resume_pos) == (
        PlaybackState.IDLE,
        False,
        PLAYED_SECONDS,
    )
    items = _queue(scenario, player_id).items
    speaker.commands.clear()
    source.release.set()
    await scenario.advance(2)

    assert _position(scenario, player_id) == paused
    assert _queue(scenario, player_id).items == items
    assert speaker.commands == []


@pytest.mark.parametrize("reports", [True, False], ids=["reports late", "never reports"])
@pytest.mark.parametrize(
    ("play", "uri"),
    [
        (_resume, TEST_TRACK_URI),
        (_play_another_track, OTHER_TRACK_URI),
        (_play_the_first_item, TEST_TRACK_URI),
    ],
)
async def test_a_play_on_a_slow_speaker_keeps_its_audio_as_the_queue_would_end(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    play: Callable[[Scenario, str], Coroutine[Any, Any, None]],
    uri: str,
    reports: bool,
) -> None:
    """
    A play whose speaker does not say in time that it plays is not ended as the playback before.

    :param play: The play action that starts as the queue would be taken for ended.
    :param uri: The track that plays after it.
    :param reports: Whether the speaker reports at all that it plays.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(SETTLE_SECONDS - SOURCE_DELAY / 2)
    handed: list[PlayerMedia] = []

    async def _take_the_stream(media: PlayerMedia) -> None:
        speaker._apply("play_media", media)
        handed.append(media)

    monkeypatch.setattr(speaker, "play_media", _take_the_stream)
    with _slow_music_source(scenario):
        started = await scenario.command(play(scenario, player_id))
        await scenario.advance(SOURCE_DELAY)
        assert len(handed) == 1
        item = _queue(scenario, player_id).current_item
        assert item is not None
        assert item.streamdetails is not None
        audio = item.streamdetails.buffer
        assert audio is not None
        await scenario.advance(PLAYBACK_START_TIMEOUT)
        # the play is done without an error once it waited for the speaker in vain
        assert started.done()
        await started
        await scenario.advance(LATE_REPORT_SECONDS - PLAYBACK_START_TIMEOUT)

        position = _position(scenario, player_id)
        assert (position.state, position.ended, position.uri) == (PlaybackState.IDLE, False, uri)
        assert item.streamdetails.buffer is audio
        assert not audio.cancelled
        if reports:
            await scenario.report(
                speaker,
                playback_state=PlaybackState.PLAYING,
                current_media=handed[0],
                elapsed_time=0,
                elapsed_time_last_updated=scenario.wall_time(),
            )
            await scenario.advance(1)
            position = _position(scenario, player_id)
            assert (position.state, position.ended, position.uri) == (
                PlaybackState.PLAYING,
                False,
                uri,
            )
        await scenario.advance(SETTLE_SECONDS + 1)
        assert not _position(scenario, player_id).ended
        assert item.streamdetails.buffer is audio
        assert not audio.cancelled


@pytest.mark.parametrize("reports", [True, False], ids=["reports late", "never reports"])
async def test_a_play_in_the_last_seconds_of_a_queue_keeps_its_audio_on_a_slow_speaker(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, reports: bool
) -> None:
    """
    A play that replaces a queue in its last seconds is not ended as the playback it replaced.

    :param reports: Whether the speaker reports at all that it plays.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_track_by_track(scenario, speaker, player_id)
    await scenario.advance(TRACK_SECONDS - 2)
    handed: list[PlayerMedia] = []

    async def _drop_the_stream_and_take_the_new_one(media: PlayerMedia) -> None:
        speaker._apply("play_media", media, playback_state=PlaybackState.IDLE, current_media=None)
        handed.append(media)

    monkeypatch.setattr(speaker, "play_media", _drop_the_stream_and_take_the_new_one)
    with _slow_music_source(scenario):
        started = await scenario.command(_play_another_track(scenario, player_id))
        await scenario.advance(SOURCE_DELAY)
        assert len(handed) == 1
        item = _queue(scenario, player_id).current_item
        assert item is not None
        assert item.streamdetails is not None
        audio = item.streamdetails.buffer
        assert audio is not None
        await scenario.advance(LATE_REPORT_SECONDS)
        assert started.done()
        await started

        position = _position(scenario, player_id)
        assert (position.state, position.ended, position.uri) == (
            PlaybackState.IDLE,
            False,
            OTHER_TRACK_URI,
        )
        assert item.streamdetails.buffer is audio
        assert not audio.cancelled
        if reports:
            await scenario.report(
                speaker,
                playback_state=PlaybackState.PLAYING,
                current_media=handed[0],
                elapsed_time=0,
                elapsed_time_last_updated=scenario.wall_time(),
            )
            await scenario.advance(1)
            position = _position(scenario, player_id)
            assert (position.state, position.ended, position.uri) == (
                PlaybackState.PLAYING,
                False,
                OTHER_TRACK_URI,
            )


async def test_a_play_that_looks_up_its_music_as_the_queue_would_end_plays(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue is never shown as ended while a play that still looks up its music holds it."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    queues = scenario.mass.player_queues
    resolve = queues._resolve_play_media_entry

    async def _slow_lookup(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1)
        return await resolve(*args, **kwargs)

    # whether the queue was ended in each update of it that clients got
    shown_ended: list[bool] = []
    unsubscribe = scenario.mass.subscribe(
        lambda event: shown_ended.append(event.data.ended),
        EventType.QUEUE_UPDATED,
        player_id,
    )
    speaker.commands.clear()
    monkeypatch.setattr(queues, "_resolve_play_media_entry", _slow_lookup)
    started = await scenario.command(
        queues.play_media(player_id, OTHER_TRACK_URI, option=QueueOption.REPLACE)
    )
    assert not started.done()
    # the server would take the queue for ended halfway the lookup
    await scenario.advance(1)
    assert started.done()
    await started
    unsubscribe()

    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.uri) == (
        PlaybackState.PLAYING,
        False,
        OTHER_TRACK_URI,
    )
    assert [command[0] for command in speaker.commands] == ["play_media"]
    assert shown_ended
    assert not any(shown_ended)


@pytest.mark.parametrize("stopped_while_idle", [False, True], ids=["left alone", "stopped"])
@pytest.mark.parametrize("flow_mode", [True, False], ids=["one flow stream", "track by track"])
async def test_a_queue_that_played_out_is_taken_for_ended(
    scenario: Scenario, flow_mode: bool, stopped_while_idle: bool
) -> None:
    """
    A queue whose player played its last track out is taken for ended 5 s later.

    :param flow_mode: Whether the queue plays as one flow stream.
    :param stopped_while_idle: Whether a stop arrives while the queue is idle at its end.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    if flow_mode:
        await _play_in_flow_mode(scenario, speaker, player_id)
    else:
        await _play_track_by_track(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)

    await scenario.advance(2)
    if stopped_while_idle:
        await scenario.mass.players.cmd_stop(player_id)
    await scenario.advance(SETTLE_SECONDS - 2.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)

    assert _position(scenario, player_id) == Position(
        state=PlaybackState.IDLE,
        ended=True,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=0,
        resume_pos=0,
    )


async def test_a_queue_stopped_and_started_at_once_still_ends_when_it_played_out(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that a play follows before the speaker said it stopped does not keep it from ending."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    _plays_from_its_start(scenario, speaker, monkeypatch)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)

    async def _stop_without_a_report() -> None:
        speaker._apply("stop")

    monkeypatch.setattr(speaker, "stop", _stop_without_a_report)
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.mass.player_queues.play_index(player_id, 0)
    await scenario.settle()
    await scenario.advance(1)
    assert _position(scenario, player_id).state == PlaybackState.PLAYING

    await _fetch_the_flow_stream(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)
    assert _position(scenario, player_id).ended


@pytest.mark.parametrize(
    ("paused_seconds", "queue_follows_the_pause"), [(0.0, False), (0.25, False), (1.0, True)]
)
async def test_a_queue_that_plays_on_after_a_pause_still_ends_when_it_played_out(
    scenario: Scenario, paused_seconds: float, queue_follows_the_pause: bool
) -> None:
    """
    A queue that is paused and plays on is taken for ended as any other once it played out.

    :param paused_seconds: Seconds between the pause and the play.
    :param queue_follows_the_pause: Whether the queue reads paused in between; it follows its
        player with a short delay, so it never does for a pause that is taken back at once.
    """
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.advance(paused_seconds)
    assert speaker.commands[-1] == ("pause",)
    queue_paused = _position(scenario, player_id).state == PlaybackState.PAUSED
    assert queue_paused is queue_follows_the_pause

    await scenario.mass.players.cmd_play(player_id)
    await scenario.advance(1)
    assert speaker.commands[-1] == ("play",)
    assert _position(scenario, player_id).state == PlaybackState.PLAYING

    await _play_out(scenario, speaker, player_id, TRACK_SECONDS - PLAYED_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)
    assert _position(scenario, player_id).ended


async def test_a_queue_that_played_out_on_a_player_that_stays_busy_keeps_its_place(
    scenario: Scenario,
) -> None:
    """A queue that played out is left as it is when a command holds its player for too long."""
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    played_out = _position(scenario, player_id)

    async def _hold_the_player() -> None:
        async with scenario.mass.players.get_group_and_player_lock(player_id):
            await asyncio.sleep(PLAYER_LOCK_TIMEOUT + SETTLE_SECONDS)

    scenario.expect_log("Gave up waiting")
    stuck = await scenario.command(_hold_the_player())
    await scenario.advance(PLAYER_LOCK_TIMEOUT + SETTLE_SECONDS)
    await stuck
    await scenario.advance(SETTLE_SECONDS + 1)
    assert _position(scenario, player_id) == played_out


async def test_a_queue_whose_stop_failed_still_ends_when_it_played_out(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop the speaker refuses leaves the queue playing, and it is taken for ended as any."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)

    async def _refuse() -> None:
        raise PlayerCommandFailed("The speaker refused to stop")

    monkeypatch.setattr(speaker, "stop", _refuse)
    with pytest.raises(PlayerCommandFailed, match="refused to stop"):
        await scenario.mass.players.cmd_stop(player_id)
    await scenario.settle()
    assert _position(scenario, player_id).state == PlaybackState.PLAYING

    await _play_out(scenario, speaker, player_id, TRACK_SECONDS - PLAYED_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)
    assert _position(scenario, player_id).ended


async def test_a_stop_that_fails_leaves_a_paused_queue_paused(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused queue whose stop the speaker refuses is still not ended when the speaker stops."""
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.advance(1)

    async def _refuse() -> None:
        raise PlayerCommandFailed("The speaker refused to stop")

    monkeypatch.setattr(speaker, "stop", _refuse)
    with pytest.raises(PlayerCommandFailed, match="refused to stop"):
        await scenario.mass.players.cmd_stop(player_id)
    await scenario.settle()
    assert _position(scenario, player_id).state == PlaybackState.PAUSED

    # the speaker drops its paused stream on its own
    await scenario.report(speaker, playback_state=PlaybackState.IDLE, current_media=None)
    await scenario.advance(SETTLE_SECONDS + 1)
    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.resume_pos) == (
        PlaybackState.IDLE,
        False,
        PLAYED_SECONDS,
    )


async def test_a_queue_stopped_by_its_sleep_timer_at_the_end_is_taken_for_ended(
    scenario: Scenario,
) -> None:
    """A sleep timer that stops the queue at the end of its last track ends the queue."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    scenario.mass.players.set_sleep_timer(player_id, mode=SleepTimerMode.END_OF_ITEM)

    # the speaker has more audio than the track lasts, so the server stops it
    await scenario.advance(TRACK_SECONDS)

    assert speaker.commands[-1] == ("stop",)
    position = _position(scenario, player_id)
    assert (position.state, position.ended) == (PlaybackState.IDLE, True)
    assert not scenario.mass.player_queues.queue_data(player_id).halted_by_command


async def test_a_queue_that_played_out_is_refilled_from_its_dynamic_source(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue with a dynamic source that played out plays what that source hands back."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    source = _DynamicSource(scenario, player_id, [await _other_track(scenario)], monkeypatch)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    speaker.commands.clear()

    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert source.fetches == 0
    await scenario.advance(1)

    assert source.fetches == 1
    assert [command[0] for command in speaker.commands] == ["play_media"]
    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.current_index, position.uri) == (
        PlaybackState.PLAYING,
        False,
        1,
        OTHER_TRACK_URI,
    )
