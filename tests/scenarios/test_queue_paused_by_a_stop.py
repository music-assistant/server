"""A queue paused on a speaker that can not pause shows as paused, ready to play on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import (
    EventType,
    PlaybackState,
    PlayerFeature,
    QueueOption,
    SleepTimerMode,
)

from music_assistant.controllers.player_queues.constants import DEFAULT_PAUSE_TIMEOUT
from music_assistant.providers.sync_group.constants import IDLE_GRACE_SECONDS

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI
from .test_commands import THIRD_TRACK_URI
from .test_group_power_on import ANOTHER_TRACK_URI, _announce
from .test_grouping import _group
from .test_paused_live_source import _grouping_commands, _names, _seen_states, _speakers, _state
from .test_paused_queue import (
    OTHER_TRACK_URI,
    PLAYED_SECONDS,
    Position,
    _forget_position,
    _pause_by_stop,
    _play_in_flow_mode,
    _position,
    _speaker,
)
from .test_stopped_queue import _play_track_by_track

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.player import PlayerMedia

    from .harness import FakeProtocol, Scenario


def _watch_queue_states(scenario: Scenario, queue_id: str) -> Callable[[], list[PlaybackState]]:
    """
    Record the states of a queue that clients are told from now on.

    Return a function that returns them, each once it changed.

    :param queue_id: The queue to watch.
    """
    states: list[PlaybackState] = []

    def _record(event: Any) -> None:
        if not states or states[-1] != event.data.state:
            states.append(event.data.state)

    unsubscribe = scenario.mass.subscribe(_record, EventType.QUEUE_UPDATED, queue_id)

    def _states() -> list[PlaybackState]:
        unsubscribe()
        return states

    return _states


def _is_at_the_pause(scenario: Scenario, player_id: str, state: PlaybackState) -> bool:
    """
    Return whether the queue of a player is in a state at the place of its pause.

    :param state: The state the queue is to be in.
    """
    position = _position(scenario, player_id)
    # the queue last saw its player play within a second before the pause
    return position.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1) and position == Position(
        state=state,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=position.elapsed_time,
        resume_pos=PLAYED_SECONDS,
    )


async def _paused_speaker(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return the Kitchen speaker, which can not pause, paused 5 s into its queue in flow mode."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await _pause_by_stop(scenario, speaker, player_id)
    return speaker, player_id


async def test_a_queue_paused_with_a_stop_shows_as_paused_until_the_pause_timeout(
    scenario: Scenario,
) -> None:
    """
    The player and its queue go from playing to paused, and to idle at the pause timeout.

    The speaker is stopped once, the sleep timer waits for the queue to play on until the pause
    timeout ends it, and play still plays the queue on from the pause after that.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    scenario.mass.players.set_sleep_timer(player_id, mode=SleepTimerMode.END_OF_ITEM)
    await scenario.advance(PLAYED_SECONDS)
    scenario.clear_events()
    queue_states = _watch_queue_states(scenario, player_id)

    await _pause_by_stop(scenario, speaker, player_id)

    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    assert _state(scenario, player_id).sleep_timer_mode == SleepTimerMode.END_OF_ITEM
    assert _position(scenario, player_id).state == PlaybackState.PAUSED
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 1)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    assert _state(scenario, player_id).sleep_timer_mode == SleepTimerMode.END_OF_ITEM
    await scenario.advance(2)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, player_id).sleep_timer_mode is None
    assert _seen_states(scenario, player_id) == [PlaybackState.PAUSED, PlaybackState.IDLE]
    assert queue_states() == [PlaybackState.PAUSED, PlaybackState.IDLE]
    assert _names(speaker) == ["play_media", "stop"]
    speaker.commands.clear()
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    assert _names(speaker) == ["play_media"]
    assert _position(scenario, player_id) == Position(
        state=PlaybackState.PLAYING,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=PLAYED_SECONDS,
        resume_pos=0,
    )


async def test_play_on_a_queue_paused_with_a_stop_plays_on_from_the_pause(
    scenario: Scenario,
) -> None:
    """Play goes from paused to playing at the pause, and the pause timeout no longer counts."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(10)
    speaker.commands.clear()
    scenario.clear_events()
    queue_states = _watch_queue_states(scenario, player_id)

    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()

    assert _names(speaker) == ["play_media"]
    assert _position(scenario, player_id) == Position(
        state=PlaybackState.PLAYING,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=PLAYED_SECONDS,
        resume_pos=0,
    )
    # past the pause timeout of the pause
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _names(speaker) == ["play_media"]
    for states in (_seen_states(scenario, player_id), queue_states()):
        assert states[-1] == PlaybackState.PLAYING
        assert PlaybackState.IDLE not in states


async def test_a_resume_shortly_before_the_pause_timeout_plays_on(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that is slow to start shows the pause until it plays, past the pause timeout."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 2)
    speaker.commands.clear()
    scenario.clear_events()

    async def _starts_late(media: PlayerMedia) -> None:
        speaker._apply("play_media", media, current_media=media)

    monkeypatch.setattr(speaker, "play_media", _starts_late)
    play = await scenario.command(scenario.mass.players.cmd_play(player_id))
    await scenario.advance(3)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    await scenario.report(
        speaker,
        playback_state=PlaybackState.PLAYING,
        elapsed_time=0,
        elapsed_time_last_updated=scenario.wall_time(),
    )
    await scenario.advance(1)
    assert play.done()
    await play
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)

    assert _names(speaker) == ["play_media"]
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert PlaybackState.IDLE not in _seen_states(scenario, player_id)


async def test_a_resume_that_never_plays_leaves_its_player_idle(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that takes the resume and stays silent is idle a pause timeout after it."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(5)
    monkeypatch.setattr(speaker, "play_media", AsyncMock())
    play = await scenario.command(scenario.mass.players.cmd_play(player_id))
    # the play waits 5 s for the speaker to report that it plays
    await scenario.advance(10)
    assert play.done()
    await play
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED

    # the pause timeout counts from the resume
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 11)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    await scenario.advance(2)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


async def test_a_pause_during_a_resume_stops_the_start_and_stays_paused(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause before the speaker plays the resume stops it, and the queue stays at its pause."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(5)
    speaker.commands.clear()

    async def _starts_late(media: PlayerMedia) -> None:
        speaker._apply("play_media", media, current_media=media)

    monkeypatch.setattr(speaker, "play_media", _starts_late)
    play = await scenario.command(scenario.mass.players.cmd_play(player_id))
    await scenario.advance(1)
    pause = await scenario.command(scenario.mass.players.cmd_pause(player_id))
    await scenario.advance(10)
    for command in (play, pause):
        assert command.done()
        await command

    assert _names(speaker) == ["play_media", "stop"]
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    monkeypatch.undo()
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    playing = _position(scenario, player_id)
    assert (playing.state, playing.current_index) == (PlaybackState.PLAYING, 0)
    assert playing.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)


@pytest.mark.parametrize("flow_mode", [True, False], ids=["flow stream", "track by track"])
@pytest.mark.parametrize(
    "keeps_media", [False, True], ids=["forgets its stream", "keeps naming its stream"]
)
async def test_a_queue_paused_with_a_stop_keeps_its_position(
    scenario: Scenario, flow_mode: bool, keeps_media: bool
) -> None:
    """
    What the stopped speaker reports does not move the queue away from its pause.

    :param flow_mode: Whether the queue plays as one flow stream or track by track.
    :param keeps_media: Whether the speaker keeps naming the stream it stopped, at its start,
        or reports no stream and no position anymore.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    if flow_mode:
        await _play_in_flow_mode(scenario, speaker, player_id)
    else:
        await _play_track_by_track(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    media = speaker.current_media
    queue_states = _watch_queue_states(scenario, player_id)

    await scenario.mass.players.cmd_pause(player_id)
    await scenario.settle()
    if keeps_media:
        await scenario.report(
            speaker, current_media=media, elapsed_time=0, elapsed_time_last_updated=None
        )
    else:
        await _forget_position(scenario, speaker)
    for _ in range(4):
        await scenario.advance(5)
        await scenario.report(speaker, elapsed_time=0, elapsed_time_last_updated=None)
        assert _is_at_the_pause(scenario, player_id, PlaybackState.PAUSED)

    assert queue_states() == [PlaybackState.PAUSED]
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    playing = _position(scenario, player_id)
    assert (playing.state, playing.current_index) == (PlaybackState.PLAYING, 0)
    assert playing.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)


@pytest.mark.parametrize("ends", ["stop", "clear", "clear without a stop"])
async def test_a_stop_or_a_clear_ends_the_pause_at_once(scenario: Scenario, ends: str) -> None:
    """
    The player is idle at once, and nothing happens at the pause timeout.

    :param ends: What the user does with the paused player.
    """
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(5)
    speaker.commands.clear()
    queues = scenario.mass.player_queues

    if ends == "stop":
        await scenario.mass.players.cmd_stop(player_id)
    else:
        queues.clear(player_id, skip_stop=ends == "clear without a stop")
    await scenario.advance(1)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    assert _position(scenario, player_id).state == PlaybackState.IDLE
    if ends == "stop":
        # the queue keeps the place of its pause
        assert _is_at_the_pause(scenario, player_id, PlaybackState.IDLE)
    else:
        assert queues.items(player_id) == []
    await scenario.idle()
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    assert _names(speaker) == []


async def test_other_media_takes_the_place_of_the_pause(scenario: Scenario) -> None:
    """Another track played on the paused queue plays at once, also past the pause timeout."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(5)
    speaker.commands.clear()
    scenario.clear_events()

    await scenario.mass.player_queues.play_media(player_id, OTHER_TRACK_URI)
    await scenario.settle()

    assert _names(speaker) == ["play_media"]
    assert _position(scenario, player_id).uri == OTHER_TRACK_URI
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    assert _names(speaker) == ["play_media"]
    assert _position(scenario, player_id).state == PlaybackState.PLAYING
    assert _seen_states(scenario, player_id) == [PlaybackState.PLAYING]


async def test_an_announcement_ends_the_pause_and_the_queue_plays_on_from_it(
    scenario: Scenario,
) -> None:
    """After the clip the player is idle, and play plays the queue on from its pause."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(5)

    await _announce(scenario, player_id, None, [speaker])
    await scenario.advance(1)

    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    assert _is_at_the_pause(scenario, player_id, PlaybackState.IDLE)
    speaker.commands.clear()
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    assert _names(speaker) == ["play_media"]
    playing = _position(scenario, player_id)
    assert playing.state == PlaybackState.PLAYING
    assert playing.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)


async def test_a_second_pause_does_not_lengthen_the_pause(scenario: Scenario) -> None:
    """A pause of a paused player sends nothing, and the pause timeout runs from the first."""
    speaker, player_id = await _paused_speaker(scenario)
    await scenario.advance(20)
    speaker.commands.clear()

    await scenario.mass.players.cmd_pause(player_id)
    await scenario.settle()

    assert speaker.commands == []
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 20 - 1)
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    await scenario.advance(2)
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE


@pytest.mark.parametrize("play_next", ["enqueue", "move"])
async def test_play_next_on_a_paused_queue_plays_right_after_the_paused_track(
    scenario: Scenario, play_next: str
) -> None:
    """
    A track played next goes right after the paused track, which plays on first.

    The speaker had the track after it in its buffer before the pause, which it no longer has.

    :param play_next: Whether a new track is played next, or a track of the queue is moved there.
    """
    sendspin = await scenario.add_provider("sendspin")
    speaker = sendspin.protocol(
        "sendspin-kitchen",
        "Kitchen",
        supported_features={*DEFAULT_FEATURES, PlayerFeature.ENQUEUE} - {PlayerFeature.PAUSE},
    )
    await scenario.register(speaker)
    await scenario.idle()
    player_id = scenario.single_player(speaker).player_id
    await _play_track_by_track(scenario, speaker, player_id)
    queues = scenario.mass.player_queues
    await queues.play_media(player_id, [OTHER_TRACK_URI, THIRD_TRACK_URI], option=QueueOption.ADD)
    items = queues.items(player_id)
    # the speaker fetched the track that follows
    queues.track_loaded_in_buffer(player_id, items[1].queue_item_id)
    await scenario.advance(PLAYED_SECONDS)
    await _pause_by_stop(scenario, speaker, player_id)

    if play_next == "enqueue":
        await queues.play_media(player_id, ANOTHER_TRACK_URI, option=QueueOption.NEXT)
        expected = [TEST_TRACK_URI, ANOTHER_TRACK_URI, OTHER_TRACK_URI, THIRD_TRACK_URI]
    else:
        queues.move_item(player_id, items[2].queue_item_id, pos_shift=0)
        expected = [TEST_TRACK_URI, THIRD_TRACK_URI, OTHER_TRACK_URI]
    await scenario.settle()

    assert [item.uri for item in queues.items(player_id)] == expected
    speaker.commands.clear()
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    playing = _position(scenario, player_id)
    assert (playing.state, playing.uri) == (PlaybackState.PLAYING, TEST_TRACK_URI)
    assert playing.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)


async def _paused_sync_group(
    scenario: Scenario, stops_on_pause: bool = False
) -> tuple[FakeProtocol, list[str], str]:
    """
    Pause the queue of the sync group of the Kitchen and Office speakers, which can not pause.

    Return the Kitchen speaker, which leads the group, the ids of both players and that of the
    group.

    :param stops_on_pause: The Kitchen speaker has a pause, but one that would stop it.
    """
    (kitchen, _), player_ids = await _speakers(scenario, "Kitchen", "Office")
    if stops_on_pause:
        await scenario.report(
            kitchen,
            supported_features={*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS},
            stops_on_pause=True,
        )
    group_id = await _group(scenario, "sync_group", *player_ids)
    await scenario.play(group_id)
    await scenario.advance(PLAYED_SECONDS)
    assert _state(scenario, player_ids[1]).synced_to == player_ids[0]
    kitchen.commands.clear()
    scenario.clear_events()
    await scenario.mass.players.cmd_pause(group_id)
    await scenario.settle()
    return kitchen, player_ids, group_id


@pytest.mark.parametrize("stops_on_pause", [False, True], ids=["no pause", "pause that stops"])
async def test_a_paused_sync_group_keeps_its_speakers(
    scenario: Scenario, stops_on_pause: bool
) -> None:
    """
    The speakers are silent and paused, and the group holds them past its idle grace.

    :param stops_on_pause: The leading speaker has a pause, but one that would stop it.
    """
    kitchen, player_ids, group_id = await _paused_sync_group(scenario, stops_on_pause)

    await scenario.advance(IDLE_GRACE_SECONDS + 5)

    assert _names(kitchen) == ["stop"]
    assert _grouping_commands(kitchen) == []
    for player_id in (group_id, *player_ids):
        assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
        assert PlaybackState.IDLE not in _seen_states(scenario, player_id)
    for player_id in player_ids:
        assert _state(scenario, player_id).active_group == group_id


@pytest.mark.parametrize("stops_on_pause", [False, True], ids=["no pause", "pause that stops"])
async def test_play_on_a_paused_sync_group_plays_on_with_the_same_speakers(
    scenario: Scenario, stops_on_pause: bool
) -> None:
    """
    Play plays the queue of the group on, without grouping its speakers again.

    :param stops_on_pause: The leading speaker has a pause, but one that would stop it.
    """
    kitchen, player_ids, group_id = await _paused_sync_group(scenario, stops_on_pause)
    await scenario.advance(IDLE_GRACE_SECONDS + 5)
    kitchen.commands.clear()

    play = await scenario.command(scenario.mass.players.cmd_play(group_id))
    await scenario.advance(5)
    assert play.done()
    await play

    assert _names(kitchen) == ["play_media"]
    assert _grouping_commands(kitchen) == []
    for player_id in (group_id, *player_ids):
        assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
        assert PlaybackState.IDLE not in _seen_states(scenario, player_id)
    await scenario.mass.players.cmd_stop(group_id)
    await scenario.advance(5)


@pytest.mark.parametrize("ends", ["pause timeout", "stop"])
async def test_a_paused_sync_group_lets_its_speakers_go_when_the_pause_ends(
    scenario: Scenario, ends: str
) -> None:
    """
    At the pause timeout or on a stop the group and its speakers are idle and free.

    :param ends: What ends the pause.
    """
    _, player_ids, group_id = await _paused_sync_group(scenario)
    await scenario.advance(5)

    if ends == "stop":
        await scenario.mass.players.cmd_stop(group_id)
        await scenario.advance(1)
    else:
        await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 5 - 1)
        assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
        await scenario.advance(2)

    for player_id in (group_id, *player_ids):
        assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    for player_id in player_ids:
        assert _state(scenario, player_id).active_group is None
    assert _position(scenario, group_id).resume_pos == pytest.approx(PLAYED_SECONDS, abs=1)


async def test_a_speaker_that_would_stop_for_a_pause_shows_its_queue_as_paused(
    scenario: Scenario,
) -> None:
    """A speaker with a pause that can not resume what it plays is stopped, and shows the pause."""
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await scenario.report(speaker, stops_on_pause=True)
    scenario.clear_events()

    await _pause_by_stop(scenario, speaker, player_id)

    assert ("pause",) not in speaker.commands
    assert _state(scenario, player_id).playback_state == PlaybackState.PAUSED
    assert PlaybackState.IDLE not in _seen_states(scenario, player_id)
    assert _is_at_the_pause(scenario, player_id, PlaybackState.PAUSED)
    speaker.commands.clear()
    await scenario.mass.players.cmd_play(player_id)
    await scenario.settle()
    assert _names(speaker) == ["play_media"]
    playing = _position(scenario, player_id)
    assert (playing.state, playing.current_index) == (PlaybackState.PLAYING, 0)
    assert playing.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)
