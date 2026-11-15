"""A queue that was paused did not end, and a play that meets the end of a queue plays."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.controllers.streams.audio import StreamsAudio

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI, FakeProtocol, Scenario

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator

    from music_assistant_models.media_items import AudioFormat

# another track of the server's test music provider than the one play() plays
OTHER_TRACK_URI = "test://track/1_1_1"
# seconds a track of the test music provider lasts
TRACK_SECONDS = 60
# seconds the queue plays before it is paused
PLAYED_SECONDS = 5
# seconds the server waits after its player stopped before it takes a queue for ended
SETTLE_SECONDS = 5
# seconds the music source takes to deliver its first audio
SOURCE_DELAY = 1.0

# read when the module loads, which is before the harness of any scenario patches it away
_get_audio_buffer = StreamsAudio.get_audio_buffer


class Position(NamedTuple):
    """Where a queue is, as a client sees it."""

    state: PlaybackState
    ended: bool
    current_index: int | None
    uri: str | None
    elapsed_time: float
    resume_pos: float


def _position(scenario: Scenario, player_id: str) -> Position:
    """Return where the queue of a player is."""
    queue = scenario.queue(player_id)
    assert queue is not None
    item = queue.current_item
    return Position(
        state=queue.state,
        ended=queue.ended,
        current_index=queue.current_index,
        uri=item.media_item.uri if item and item.media_item else None,
        elapsed_time=queue.elapsed_time,
        resume_pos=queue.resume_pos,
    )


async def _speaker(scenario: Scenario, *, can_pause: bool) -> tuple[FakeProtocol, str]:
    """
    Return a Sendspin speaker and the id of its player.

    :param can_pause: Whether the speaker has a pause of its own; without one a pause stops it.
    """
    sendspin = await scenario.add_provider("sendspin")
    features = set(DEFAULT_FEATURES if can_pause else DEFAULT_FEATURES - {PlayerFeature.PAUSE})
    speaker = sendspin.protocol("sendspin-kitchen", "Kitchen", supported_features=features)
    await scenario.register(speaker)
    await scenario.idle()
    return speaker, scenario.single_player(speaker).player_id


async def _fetch_the_flow_stream(scenario: Scenario, speaker: FakeProtocol, player_id: str) -> None:
    """
    Let the speaker fetch the flow stream of its queue to the end, and play it from its start.

    No fake protocol fetches audio, so this books what the stream server books while it serves
    a flow stream that ends with the queue's current item: the queue is in flow mode, the item
    is served and streamed in full, and the stream is complete. A short track is in the buffer
    of its player like that within a second.
    """
    queues = scenario.mass.player_queues
    queue_data = queues.queue_data(player_id)
    queue = queue_data.queue
    item = queue.current_item
    assert item is not None
    assert item.streamdetails is not None
    seek_position = item.streamdetails.seek_position
    queue.flow_mode = True
    queue_data.flow_mode_stream_log = [
        PlayLogEntry(
            item.queue_item_id,
            duration=TRACK_SECONDS,
            seconds_streamed=float(TRACK_SECONDS - seek_position),
            seek_position=seek_position,
        )
    ]
    queues.mark_item_served(player_id, item.queue_item_id)
    queues.queue_buffer_completed(player_id, queue_exhausted=True)
    await scenario.report(speaker, elapsed_time=0, elapsed_time_last_updated=scenario.wall_time())


async def _play_in_flow_mode(scenario: Scenario, speaker: FakeProtocol, player_id: str) -> None:
    """Play a track on the player, as one flow stream that the speaker fetched in full."""
    await scenario.play(player_id)
    await _fetch_the_flow_stream(scenario, speaker, player_id)


async def _forget_position(scenario: Scenario, speaker: FakeProtocol) -> None:
    """Let a speaker that stopped report no position anymore, so its next stream starts at 0."""
    assert speaker.commands[-1] == ("stop",)
    await scenario.report(speaker, elapsed_time=None, elapsed_time_last_updated=None)


async def _pause_by_stop(scenario: Scenario, speaker: FakeProtocol, player_id: str) -> None:
    """Pause a player whose speaker has no pause, as a client does: the speaker stops."""
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.settle()
    await _forget_position(scenario, speaker)


async def _play_out(
    scenario: Scenario, speaker: FakeProtocol, player_id: str, seconds: float
) -> None:
    """
    Let the speaker play the rest of its stream and stop; returns once its queue is idle.

    :param seconds: Seconds of the stream that are left to play.
    """
    await scenario.advance(seconds)
    await scenario.report(speaker, playback_state=PlaybackState.IDLE, current_media=None)
    queue = scenario.queue(player_id)
    assert queue is not None
    for _ in range(16):
        if queue.state == PlaybackState.IDLE:
            return
        # the queue follows its player with a short delay
        await scenario.advance(0.125)
    pytest.fail("the queue did not follow its player to idle")


@contextlib.contextmanager
def _slow_music_source(scenario: Scenario) -> Iterator[None]:
    """
    Let the audio of a queue item be prepared for real, from a source that takes a second.

    The queue then waits for the first audio of an item before it starts it on the player, as
    it does on a real server; the audio itself is silence that no ffmpeg decodes or analyses.
    """

    async def _source(
        _audio: StreamsAudio, _streamdetails: Any, pcm_format: AudioFormat, **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        await asyncio.sleep(SOURCE_DELAY)
        for _ in range(3):
            yield bytes(pcm_format.pcm_sample_size)

    async def _no_analysis(*_args: Any) -> None:
        return

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(StreamsAudio, "get_audio_buffer", _get_audio_buffer)
        patches.setattr(StreamsAudio, "get_media_stream", _source)
        patches.setattr(scenario.mass.streams.audio_analysis, "start_analysis", _no_analysis)
        yield


async def test_a_pause_on_a_player_that_can_not_pause_is_not_the_end_of_its_queue(
    scenario: Scenario,
) -> None:
    """A queue paused on its last track stays where it was paused, however long it is paused."""
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)

    await _pause_by_stop(scenario, speaker, player_id)

    paused = _position(scenario, player_id)
    assert paused.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)
    assert paused == Position(
        state=PlaybackState.IDLE,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=paused.elapsed_time,
        resume_pos=PLAYED_SECONDS,
    )
    for seconds in (SETTLE_SECONDS, 1, 60):
        await scenario.advance(seconds)
        assert _position(scenario, player_id) == paused


@pytest.mark.parametrize("paused_seconds", [4.5, 5.0, 5.5, 40.0])
async def test_a_queue_paused_on_a_player_that_can_not_pause_plays_on_from_there(
    scenario: Scenario, paused_seconds: float
) -> None:
    """
    A resume plays on from where the queue was paused, whenever it comes.

    Once it has played to its end after that, the queue is taken for ended as any other.

    :param paused_seconds: Seconds between the pause and the resume.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)
    await _pause_by_stop(scenario, speaker, player_id)
    await scenario.advance(paused_seconds)

    with _slow_music_source(scenario):
        resume = await scenario.command(scenario.mass.player_queues.resume(player_id))
        await scenario.advance(SOURCE_DELAY)
        assert resume.done()
        await resume

    assert speaker.commands[-1][0] == "play_media"
    assert _position(scenario, player_id) == Position(
        state=PlaybackState.PLAYING,
        ended=False,
        current_index=0,
        uri=TEST_TRACK_URI,
        elapsed_time=PLAYED_SECONDS,
        resume_pos=0,
    )

    await _fetch_the_flow_stream(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS - PLAYED_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)
    assert _position(scenario, player_id).ended


async def test_a_paused_queue_that_is_stopped_at_the_pause_timeout_is_not_ended(
    scenario: Scenario,
) -> None:
    """A queue that stays paused on its last track until the server stops it plays on from there."""
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)

    await scenario.mass.players.cmd_pause(player_id)
    await scenario.advance(1)
    assert speaker.commands[-1] == ("pause",)
    assert _position(scenario, player_id).state == PlaybackState.PAUSED
    # the pause timeout is 30 s
    await scenario.advance(60)

    await _forget_position(scenario, speaker)
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


async def test_a_queue_whose_pause_failed_still_ends_when_it_played_out(
    scenario: Scenario,
) -> None:
    """A pause the speaker refuses leaves the queue playing, and it is taken for ended as any."""
    speaker, player_id = await _speaker(scenario, can_pause=True)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await scenario.advance(PLAYED_SECONDS)

    async def _refuse() -> None:
        raise PlayerCommandFailed("The speaker refused to pause")

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(speaker, "pause", _refuse)
        with pytest.raises(PlayerCommandFailed, match="refused to pause"):
            await scenario.mass.players.cmd_pause(player_id)
    await scenario.settle()
    assert _position(scenario, player_id).state == PlaybackState.PLAYING

    await _play_out(scenario, speaker, player_id, TRACK_SECONDS - PLAYED_SECONDS)
    await scenario.advance(SETTLE_SECONDS - 0.5)
    assert not _position(scenario, player_id).ended
    await scenario.advance(1)
    assert _position(scenario, player_id).ended


def _resume(scenario: Scenario, player_id: str) -> Coroutine[Any, Any, None]:
    """Resume the queue."""
    return scenario.mass.player_queues.resume(player_id)


def _play_another_track(scenario: Scenario, player_id: str) -> Coroutine[Any, Any, None]:
    """Play another track on the queue."""
    return scenario.mass.player_queues.play_media(player_id, OTHER_TRACK_URI)


def _play_the_first_item(scenario: Scenario, player_id: str) -> Coroutine[Any, Any, None]:
    """Play the first item of the queue."""
    return scenario.mass.player_queues.play_index(player_id, 0)


@pytest.mark.parametrize(
    ("play", "uri"),
    [
        (_resume, TEST_TRACK_URI),
        (_play_another_track, OTHER_TRACK_URI),
        (_play_the_first_item, TEST_TRACK_URI),
    ],
)
async def test_a_play_that_starts_as_an_ended_queue_is_settled_plays(
    scenario: Scenario,
    play: Callable[[Scenario, str], Coroutine[Any, Any, None]],
    uri: str,
) -> None:
    """
    A play that is still starting when the server takes its queue for ended plays.

    :param play: The play action that meets the end of the queue.
    :param uri: The track that plays after it.
    """
    speaker, player_id = await _speaker(scenario, can_pause=False)
    await _play_in_flow_mode(scenario, speaker, player_id)
    await _play_out(scenario, speaker, player_id, TRACK_SECONDS)
    await scenario.advance(SETTLE_SECONDS - SOURCE_DELAY / 2)

    speaker.commands.clear()
    with _slow_music_source(scenario):
        started = await scenario.command(play(scenario, player_id))
        assert not started.done()
        # the server would take the queue for ended halfway the wait for the source
        await scenario.advance(SOURCE_DELAY)
        assert started.done()
        await started

    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.uri) == (PlaybackState.PLAYING, False, uri)
    await scenario.advance(SETTLE_SECONDS)
    position = _position(scenario, player_id)
    assert (position.state, position.ended, position.uri) == (PlaybackState.PLAYING, False, uri)
    # the speaker was started once, by the play
    assert [command[0] for command in speaker.commands] == ["play_media"]
