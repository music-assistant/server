"""Tests for the crossfade guard that waits for the player to hold the next item."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import CrossfadeMode
from music_assistant_models.errors import QueueEmpty

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.player_protocol import PlayerProtocol
from tests.controllers.streams.test_crossfade_same_album import (
    OTHER_PROVIDER_ALBUM,
    PROVIDER_ALBUM,
    _queue_item,
)
from tests.controllers.streams.test_crossfade_transition import (
    STANDARD_CROSSFADE_DURATION,
    TEST_PCM_FORMAT,
    _flow_audio,
    _install_counting_mix,
)
from tests.controllers.streams.test_crossfade_transition import (
    _install_item_streams as _install_flow_item_streams,
)
from tests.controllers.streams.test_crossfade_transition import _queue_item as _flow_item
from tests.controllers.streams.test_file_stream_crossfade import (
    _Rig,
    passthrough_encoder,  # noqa: F401
    rig,  # noqa: F401
)
from tests.controllers.streams.test_file_stream_route import PLAYER_ID
from tests.controllers.streams.test_item_voice_over import (
    CLIP_MARKER,
    _declared_track,
    _install_item_streams,
    _item_audio,
    _request,
)
from tests.controllers.streams.test_voice_over_transition import (
    CLIP_SECONDS,
    SAMPLE_SIZE,
    TRACK_SECONDS,
    _clip,
    _drain,
    _track,
)

CURRENT = _queue_item("track-1", PROVIDER_ALBUM)
NEXT = _queue_item("track-2", OTHER_PROVIDER_ALBUM)


def _player_holds_next_item(mass: MagicMock, received: bool) -> MagicMock:
    """Let the player of the item streams answer whether it holds the next item."""
    protocol = cast("MagicMock", mass.players.get_protocol.return_value)
    protocol.has_received_next_item.return_value = received
    return protocol


def _audio(*, received: bool) -> tuple[StreamsAudio, MagicMock]:
    """Build a StreamsAudio whose queue reports NEXT, and whose player answers for it."""
    mass = MagicMock()
    mass.player_queues.get.return_value = MagicMock()
    mass.player_queues.get_next_item.return_value = NEXT
    mass.config.get_raw_core_config_value.return_value = False
    _player_holds_next_item(mass, received)
    return StreamsAudio(cast("Any", mass)), mass


@pytest.mark.parametrize("received", [True, False])
def test_item_streams_crossfade_only_into_an_item_the_player_holds(received: bool) -> None:
    """A player that has not received the next item gets a gapless boundary instead."""
    audio, mass = _audio(received=received)

    allowed = audio.crossfade_allowed(
        CURRENT,
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        protocol_id="player-1",
        flow_mode=False,
        next_queue_item=NEXT,
    )

    assert allowed is received
    mass.players.get_protocol.assert_called_once_with("player-1")
    mass.players.get_protocol.return_value.has_received_next_item.assert_called_once_with(NEXT)


def test_a_flow_stream_crossfades_whatever_the_player_holds() -> None:
    """A flow stream carries the next item itself, so the player is not asked."""
    audio, mass = _audio(received=False)
    protocol = mass.players.get_protocol.return_value

    assert audio.crossfade_allowed(
        CURRENT, crossfade_mode=CrossfadeMode.SMART_CROSSFADE, flow_mode=True
    )
    protocol.has_received_next_item.assert_not_called()


def test_a_player_handed_its_next_item_by_enqueue_holds_it_by_default() -> None:
    """The default answer leaves the crossfade of every enqueueing player as it is."""

    class _EnqueuePlayer(PlayerProtocol):
        """A player that takes its next item from enqueue_next_media."""

    player = _EnqueuePlayer.__new__(_EnqueuePlayer)

    assert player.has_received_next_item(NEXT) is True


async def _stream_into_next_track(
    monkeypatch: pytest.MonkeyPatch, *, received: bool
) -> tuple[StreamsAudio, MagicMock, MagicMock, bytes]:
    """
    Stream a track that crossfades into the next one, on a player that holds it or not.

    Returns the audio, the player's protocol, the mixer's build mock and the streamed audio.
    """
    first = _track("item-1", "First")
    first.streamdetails.duration = TRACK_SECONDS
    second = _declared_track()
    audio, mass = _item_audio(monkeypatch, next_item=second, load_next=[second, QueueEmpty])
    protocol = _player_holds_next_item(mass, received)
    _install_item_streams(monkeypatch, audio)
    build = cast("MagicMock", audio.smart_fades_mixer.build)
    build.side_effect = None
    build.return_value = SimpleNamespace(
        timing_info=SimpleNamespace(
            fadein_trimmed_duration=0.0,
            crossfade_duration=float(STANDARD_CROSSFADE_DURATION),
            pre_crossfade_duration=0.0,
            post_crossfade_duration=0.0,
        )
    )
    output = await _drain(_request(audio, first, CrossfadeMode.STANDARD_CROSSFADE))
    return audio, protocol, build, output


async def test_an_item_stream_crossfades_into_a_next_item_the_player_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player that holds the next item gets the crossfade it asked for."""
    audio, _protocol, build, _output = await _stream_into_next_track(monkeypatch, received=True)

    build.assert_awaited_once()
    assert build.await_args.kwargs["mode"] == CrossfadeMode.STANDARD_CROSSFADE
    await audio._crossfade_handover.pop("queue-1").close()


async def test_an_item_stream_plays_its_tail_out_when_the_player_lacks_the_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outgoing item streams whole and plainly, and no fade waits for the next item."""
    audio, protocol, build, output = await _stream_into_next_track(monkeypatch, received=False)

    build.assert_not_awaited()
    assert output == bytes([CLIP_MARKER]) * TRACK_SECONDS * SAMPLE_SIZE
    assert "queue-1" not in audio._crossfade_handover
    assert "queue-1" not in audio._crossfade_pending
    # asked about the item the fade would have played into
    assert protocol.has_received_next_item.call_args.args[0].queue_item_id == "item-2"


@pytest.mark.parametrize("new_leader_holds_it", [True, False])
async def test_the_boundary_asks_the_player_that_renders_the_stream_by_then(
    monkeypatch: pytest.MonkeyPatch, new_leader_holds_it: bool
) -> None:
    """A group that changed its leader while the track played is mixed for its new leader."""
    first = _track("item-1", "First")
    first.streamdetails.duration = TRACK_SECONDS
    second = _declared_track()
    audio, mass = _item_audio(monkeypatch, next_item=second, load_next=[second, QueueEmpty])
    _install_item_streams(monkeypatch, audio)
    build = cast("MagicMock", audio.smart_fades_mixer.build)
    build.side_effect = None
    build.return_value = SimpleNamespace(
        timing_info=SimpleNamespace(
            fadein_trimmed_duration=0.0,
            crossfade_duration=float(STANDARD_CROSSFADE_DURATION),
            pre_crossfade_duration=0.0,
            post_crossfade_duration=0.0,
        )
    )
    # the leader that left holds nothing of the queue anymore, or still everything
    old_leader = MagicMock(protocol_id="old-leader")
    old_leader.name = "Old leader"
    old_leader.has_received_next_item.return_value = not new_leader_holds_it
    new_leader = MagicMock(protocol_id="new-leader")
    new_leader.has_received_next_item.return_value = new_leader_holds_it
    protocols = {"old-leader": old_leader, "new-leader": new_leader}
    mass.players.get_protocol.side_effect = protocols.get
    leaders = {"group-1": old_leader}
    mass.players.get_output_protocol.side_effect = leaders.get
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", old_leader),
        cast("Any", first),
        TEST_PCM_FORMAT,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=STANDARD_CROSSFADE_DURATION,
        session_id="session-1",
        player_id="group-1",
    )

    # the track plays, and then the group changes its leader
    output = bytearray(await anext(stream))
    build.assert_not_awaited()
    leaders["group-1"] = new_leader
    output += await _drain(stream)

    assert build.await_count == int(new_leader_holds_it)
    new_leader.has_received_next_item.assert_called_once()
    old_leader.has_received_next_item.assert_not_called()
    select_pcm_format = cast("MagicMock", audio.select_pcm_format)
    assert select_pcm_format.await_args.kwargs["player"] is new_leader
    if new_leader_holds_it:
        await audio._crossfade_handover.pop("queue-1").close()
    else:
        assert bytes(output) == bytes([CLIP_MARKER]) * TRACK_SECONDS * SAMPLE_SIZE


async def test_the_stream_of_a_file_is_told_the_id_the_file_is_fetched_under(
    rig: _Rig,  # noqa: F811
) -> None:
    """A file outlives a change of leader, so its stream gets the id that names the group."""
    await rig.fetch("a")

    assert rig.streams.call_args.kwargs["player_id"] == PLAYER_ID


async def test_a_declared_overlap_plays_whole_when_the_player_lacks_its_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voice over is a fade into the next item too, so it waits for the player as well."""
    clip = _clip()
    track = _declared_track()
    audio, mass = _item_audio(monkeypatch, next_item=track, load_next=[track, QueueEmpty])
    _player_holds_next_item(mass, received=False)
    _install_item_streams(monkeypatch, audio)

    output = await _drain(_request(audio, clip, CrossfadeMode.VOICE_OVER))

    cast("Any", audio.smart_fades_mixer.build).assert_not_awaited()
    assert output == bytes([CLIP_MARKER]) * CLIP_SECONDS * SAMPLE_SIZE
    assert "queue-1" not in audio._crossfade_handover


async def test_a_flow_stream_mixes_the_boundary_while_the_player_lacks_the_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is for item streams only: a flow stream carries the next item itself."""
    first_item = _flow_item("item-1", "First")
    second_item = _flow_item("item-2", "Second")
    audio, queue, mass = _flow_audio(
        monkeypatch, next_item=second_item, load_next=[second_item, QueueEmpty]
    )
    # the real rule decides whether the boundary is mixed
    del audio.crossfade_allowed
    protocol = _player_holds_next_item(mass, received=False)
    _install_flow_item_streams(monkeypatch, audio, {"item-1": 40, "item-2": 20})
    mix_calls = _install_counting_mix(monkeypatch, audio)

    await _drain(
        audio.get_queue_flow_stream(
            cast("Any", queue), cast("Any", first_item), TEST_PCM_FORMAT, session_id="session-1"
        )
    )

    assert len(mix_calls) == 1
    protocol.has_received_next_item.assert_not_called()
