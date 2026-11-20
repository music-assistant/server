"""Tests for the Sendspin calibration session orchestration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import OutputProtocol

from music_assistant.providers.sendspin.calibration_session import (
    CalibrationSession,
    CalibrationSessionState,
    is_eligible,
    resolve_sendspin_protocol,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from music_assistant.providers.sendspin.calibration_session import StaticDelaySnapshot

ANCHOR_ID = "virtual_calibration"


async def test_start_groups_every_target_and_starts_the_track_once() -> None:
    """A session attaches all targets to the anchor and plays the track a single time."""
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])

    shared = _shared_session(session)
    assert [call.args[0] for call in shared.add_guest_listener.await_args_list] == ["a", "b", "c"]
    mass.player_queues.play_media.assert_awaited_once()
    assert mass.player_queues.play_media.await_args.args[0] == ANCHOR_ID


async def test_the_track_does_not_start_until_begin_is_called() -> None:
    """Commandeering the speakers and starting the track are separate steps."""
    mass = _make_mass(_make_player("a"))

    with patch(
        "music_assistant.providers.sendspin.calibration_session.SharedPlaybackSession"
    ) as shared_cls:
        shared_cls.create_remote = AsyncMock(return_value=_make_shared_session())
        session = await CalibrationSession.create(_sendspin(mass), ["a"])

    mass.player_queues.play_media.assert_not_awaited()
    await session.begin(MagicMock())
    mass.player_queues.play_media.assert_awaited_once()


async def test_soloing_never_restarts_the_stream() -> None:
    """
    Walking every speaker leaves the single calibration stream untouched.

    This is the property the whole measurement depends on: the chirp train is a
    metronome on the server clock and a restart resets its timeline.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])
    mass.player_queues.play_media.reset_mock()

    for player_id in ("a", "b", "c", "a"):
        await session.solo(player_id)

    mass.player_queues.play_media.assert_not_awaited()
    mass.player_queues.stop.assert_not_awaited()


async def test_start_silences_every_member() -> None:
    """
    A started session is inaudible until a speaker is soloed.

    Otherwise the whole group chirps at once from the moment the track starts, which
    tells the user nothing and is merely startling.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))

    session = await _start(mass, ["a", "b", "c"])

    assert _silenced(mass) == {"a", "b", "c"}
    assert session.soloed_player_id is None


async def test_every_member_is_silenced_before_the_stream_starts() -> None:
    """
    The silencing lands while the track is not yet running, not after its first chirp.

    A member silenced once the chirp train was already playing would be audible for as
    long as the commands took to land.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"))

    with patch(
        "music_assistant.providers.sendspin.calibration_session.SharedPlaybackSession"
    ) as shared_cls:
        shared_cls.create_remote = AsyncMock(return_value=_make_shared_session())
        await CalibrationSession.create(_sendspin(mass), ["a", "b"])

    assert _silenced(mass) == {"a", "b"}
    mass.player_queues.play_media.assert_not_awaited()


async def test_the_first_solo_makes_exactly_one_member_audible() -> None:
    """From the all-silent start a solo only has to bring its own target back up."""
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])
    # the start silenced every member, so what remains to assert is the solo's own doing
    mass.players.cmd_volume_mute.reset_mock()

    await session.solo("b")

    assert _silenced(mass) == {"a", "c"}
    assert _mute_calls(mass) == [("b", False)]
    assert session.soloed_player_id == "b"


async def test_start_refuses_a_member_it_cannot_silence() -> None:
    """
    A speaker that stays in the mix is fatal to the start, not something to log and go on.

    The probe attributes each arrival to the one speaker it believes is audible, so a
    member left audible corrupts every other member's measurement rather than only its
    own - unlike a solo, where silencing what is not being measured is best effort.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))
    _fail_for(mass.players.cmd_volume_mute, "b", PlayerUnavailableError("gone"))
    shared = _make_shared_session()

    with pytest.raises(ActionUnavailable, match="silenced"):
        await _start(mass, ["a", "b", "c"], shared=shared)

    # 'a' and 'b' were grouped and are released again, both audible; 'c' was never reached
    assert _silenced(mass) == set()
    assert {call.args[0] for call in shared.remove_guest_listener.await_args_list} == {"a", "b"}
    mass.player_queues.play_media.assert_not_awaited()


async def test_silencing_at_start_never_touches_the_stream() -> None:
    """Taking the members out of the mix is a per-player command, never a queue one."""
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))

    await _start(mass, ["a", "b", "c"])

    mass.player_queues.play_media.assert_awaited_once()
    mass.player_queues.stop.assert_not_awaited()


async def test_solo_hands_the_next_target_over_unmuted() -> None:
    """The speaker whose turn it is comes out of a previous solo's mute before its peers go in."""
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a", "b"])

    await session.solo("a")
    assert _silenced(mass) == {"b"}
    await session.solo("b")

    assert _silenced(mass) == {"a"}


async def test_start_rejects_a_muted_player() -> None:
    """
    A speaker the user had already muted can not be calibrated.

    The session refuses to claim a mute it did not make, so such a speaker could be
    soloed but never actually heard - better to say so up front than to hand back a
    session with a member that yields no measurement.
    """
    mass = _make_mass(_make_player("a"), _make_player("b", volume_muted=True))

    with pytest.raises(ActionUnavailable, match="muted"):
        await _start(mass, ["a", "b"])


async def test_start_rejects_a_player_turned_all_the_way_down() -> None:
    """A speaker at volume zero is as inaudible as a muted one, force flag or not."""
    mass = _make_mass(_make_player("a", volume_level=0))

    with pytest.raises(ActionUnavailable):
        await _start(mass, ["a"], force=True)


async def test_a_player_without_a_mute_control_is_silenced_by_volume() -> None:
    """A Sendspin client that does not implement mute is taken out of the mix by volume."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b", mute_control=PLAYER_CONTROL_NONE, volume_level=55),
    )
    session = await _start(mass, ["a", "b"])

    await session.solo("a")

    assert ("b", 0) in _volume_calls(mass)
    assert "b" not in _mute_command_targets(mass)

    await session.stop()
    assert ("b", 55) in _volume_calls(mass)


async def test_a_volume_silenced_player_stays_tracked_when_its_state_lags() -> None:
    """
    A speaker whose volume command has not landed yet is still re-silenced.

    Sendspin's volume_set only reports back once the client acknowledges it, so a
    live read of volume_level can still show the previous solo's zero. Deciding
    ownership from that read would drop the speaker from the session's bookkeeping
    and let it rise back to full volume mid-measurement.
    """
    mass = _make_mass(
        _make_player("a", mute_control=PLAYER_CONTROL_NONE),
        _make_player("b", mute_control=PLAYER_CONTROL_NONE, volume_level=55),
        optimistic_volume=False,
    )
    session = await _start(mass, ["a", "b"])

    await session.solo("a")
    await session.solo("b")
    mass.players.cmd_volume_set.reset_mock()
    await session.solo("a")

    assert ("b", 0) in _volume_calls(mass)


async def test_a_player_that_was_only_muted_keeps_a_volume_change() -> None:
    """The restore undoes the control it drove, so a volume the user moved is left alone."""
    player = _make_player("b")
    mass = _make_mass(_make_player("a"), player)
    session = await _start(mass, ["a", "b"])
    await session.solo("a")
    # the user turns 'b' down while it is muted for the measurement
    player.state.volume_level = 15

    await session.stop()

    assert "b" not in {player_id for player_id, _ in _volume_calls(mass)}
    assert ("b", False) in _mute_calls(mass)


async def test_solo_rejects_a_player_outside_the_session() -> None:
    """Only members of the running session can be isolated."""
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a"])

    with pytest.raises(InvalidDataError):
        await session.solo("b")


async def test_solo_rejects_a_stopped_session() -> None:
    """A session that has been torn down can not be driven any further."""
    mass = _make_mass(_make_player("a"))
    session = await _start(mass, ["a"])
    await session.stop()

    with pytest.raises(ActionUnavailable):
        await session.solo("a")


async def test_the_state_a_client_renders() -> None:
    """The payload the API hands a UI describes the anchor, the members and the stream."""
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a", "b"])

    await session.solo("b")

    assert session.state == CalibrationSessionState(
        anchor_player_id=ANCHOR_ID,
        queue_id=ANCHOR_ID,
        player_ids=["a", "b"],
        soloed_player_id="b",
        streaming=True,
    )


async def test_a_stream_that_is_no_longer_the_calibration_track_is_not_streaming() -> None:
    """The reported stream state follows the anchor's queue, not the session's intent."""
    mass = _make_mass(_make_player("a"))
    session = await _start(mass, ["a"])
    assert session.streaming

    mass.player_queues.get.return_value.state = PlaybackState.IDLE
    assert not session.streaming


async def test_stop_restores_volume_mute_grouping_and_playback() -> None:
    """Every target gets its volume, mute, group membership and playback put back."""
    mass = _make_mass(
        _make_player("a", volume_level=30, synced_to="leader"),
        _make_player(
            "b", volume_level=70, active_group="group", playback_state=PlaybackState.PLAYING
        ),
        _make_player(
            "c",
            volume_level=55,
            playback_state=PlaybackState.PLAYING,
            active_source="c_queue",
        ),
    )
    session = await _start(mass, ["a", "b", "c"], force=True)
    await session.solo("a")

    await session.stop()

    shared = _shared_session(session)
    mass.player_queues.stop.assert_awaited_once_with(ANCHOR_ID)
    assert {call.args[0] for call in shared.remove_guest_listener.await_args_list} == {
        "a",
        "b",
        "c",
    }
    assert ("b", False) in _mute_calls(mass)
    assert ("c", False) in _mute_calls(mass)
    assert mass.players.cmd_set_members.await_args_list[0].args[0] == "leader"
    assert mass.players.cmd_set_members.await_args_list[1].args[0] == "group"
    # 'c' was playing its own content standalone, so it is resumed on the source it
    # was on; 'b' is left to the group it was rejoined to, which restarts it itself
    mass.players.cmd_resume.assert_awaited_once()
    assert mass.players.cmd_resume.await_args.args[0] == "c"
    assert mass.players.cmd_resume.await_args.args[1] == "c_queue"
    shared.close.assert_awaited_once()


async def test_stop_restores_a_session_that_was_never_soloed() -> None:
    """
    A session that dies before the first measurement still hands the speakers back.

    Starting one silences every member, so a phone that walks away between the start
    and the first tap would otherwise leave the whole group inaudible.
    """
    mass = _make_mass(
        _make_player("a", volume_level=30),
        _make_player("b", mute_control=PLAYER_CONTROL_NONE, volume_level=70),
    )
    session = await _start(mass, ["a", "b"])
    assert _silenced(mass) == {"a", "b"}

    await session.stop()

    assert _silenced(mass) == set()
    assert ("a", False) in _mute_calls(mass)
    assert ("b", 70) in _volume_calls(mass)


async def test_stop_restarts_a_group_that_cannot_take_members_back() -> None:
    """A group player without SET_MEMBERS is restarted instead of re-joined member by member."""
    leader = _make_player("group", supported_features=set(), type=PlayerType.GROUP)
    mass = _make_mass(
        _make_player("a", active_group="group", playback_state=PlaybackState.PLAYING), leader
    )
    session = await _start(mass, ["a"], force=True)

    await session.stop()

    mass.players.cmd_set_members.assert_not_awaited()
    mass.players.cmd_play.assert_awaited_once_with("group")


async def test_stop_powers_a_player_back_off() -> None:
    """Grouping powers a speaker on, so one that was off is switched off again."""
    mass = _make_mass(_make_player("a", powered=False, power_control=PLAYER_CONTROL_NATIVE))
    session = await _start(mass, ["a"])

    await session.stop()

    mass.players.cmd_power.assert_awaited_once_with("a", False)
    mass.players.cmd_resume.assert_not_awaited()


async def test_stop_leaves_a_player_without_power_control_alone() -> None:
    """A speaker with no power control has no power state to restore."""
    mass = _make_mass(_make_player("a", powered=None))
    session = await _start(mass, ["a"])

    await session.stop()

    mass.players.cmd_power.assert_not_awaited()


async def test_stop_leaves_an_idle_player_stopped() -> None:
    """A speaker that was not playing is not started up by the restore."""
    mass = _make_mass(_make_player("a"))
    session = await _start(mass, ["a"])

    await session.stop()

    mass.players.cmd_resume.assert_not_awaited()


async def test_stop_is_idempotent() -> None:
    """Stopping an already stopped session restores nothing a second time."""
    mass = _make_mass(_make_player("a"))
    session = await _start(mass, ["a"])
    await session.stop()
    mass.player_queues.stop.reset_mock()

    await session.stop()

    assert session.stopped
    mass.player_queues.stop.assert_not_awaited()


async def test_stop_restores_the_players_when_the_anchor_is_already_gone() -> None:
    """
    A vanished anchor queue does not skip the restore.

    The anchor lives in the Sendspin provider's memory, so a Sendspin reload takes
    it (and its queue) with it while the session still holds the speakers.
    """
    mass = _make_mass(_make_player("a", volume_level=30))
    session = await _start(mass, ["a"])
    await session.solo("a")
    mass.player_queues.get.return_value = None

    await session.stop()

    mass.player_queues.stop.assert_not_awaited()
    _shared_session(session).remove_guest_listener.assert_awaited_once_with("a")


async def test_stop_restores_the_players_when_stopping_the_stream_fails() -> None:
    """A stream that refuses to stop may not cost the user their speaker state."""
    mass = _make_mass(_make_player("a", volume_level=30), _make_player("b", volume_level=70))
    session = await _start(mass, ["a", "b"])
    await session.solo("a")
    mass.player_queues.stop.side_effect = RuntimeError("queue is gone")

    await session.stop()

    assert ("b", False) in _mute_calls(mass)
    _shared_session(session).close.assert_awaited_once()


async def test_stop_restores_the_remaining_players_when_one_fails() -> None:
    """A player that has gone away mid-session can not strand its peers muted."""
    peer = _make_player("b")
    mass = _make_mass(_make_player("a"), peer, _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])
    await session.solo("c")
    assert peer.state.volume_muted
    _fail_for(mass.players.cmd_volume_mute, "a", PlayerUnavailableError("gone"))

    await session.stop()

    assert not peer.state.volume_muted


async def test_stop_restores_the_remaining_players_when_one_cannot_be_ungrouped() -> None:
    """An ungroup that fails outside the MA error hierarchy still leaves the peers restored."""
    peer = _make_player("b")
    mass = _make_mass(_make_player("a"), peer, _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])
    await session.solo("c")
    shared = _shared_session(session)
    _fail_for(shared.remove_guest_listener, "a", RuntimeError("client exploded"))

    await session.stop()

    assert not peer.state.volume_muted
    shared.close.assert_awaited_once()


async def test_a_player_that_cannot_be_ungrouped_is_still_unmuted() -> None:
    """Each restore step is attempted on its own, so a stuck ungroup leaves nothing silent."""
    stuck = _make_player("a")
    mass = _make_mass(stuck, _make_player("b"))
    session = await _start(mass, ["a", "b"])
    await session.solo("b")
    assert stuck.state.volume_muted
    _fail_for(_shared_session(session).remove_guest_listener, "a", RuntimeError("stuck"))

    await session.stop()

    assert not stuck.state.volume_muted


async def test_start_measures_every_writable_speaker_from_zero() -> None:
    """
    A session takes MA's static delay off each member before anything is measured.

    A delay advances a client, so one the size of a factory default moves the speaker's
    arrival outside the window a chirp period can place it in and the reading folds to a
    plausible wrong value. Folding the delay back in afterwards can not recover that.
    """
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )

    await _start(mass, ["a", "b"])

    assert _static_delays(mass) == {"a": 0, "b": 0}


async def test_start_zeroes_the_client_behind_a_physical_speaker() -> None:
    """The delay belongs to the hidden Sendspin client, not the visible player."""
    mass = _make_mass(_make_speaker("living_room"), static_delays_ms={"living_room_client": 120})

    await _start(mass, ["living_room"])

    assert _delay_writes(mass) == [("living_room_client", 0)]


async def test_start_leaves_a_speaker_that_refuses_a_delay_alone() -> None:
    """
    A member MA can not write to has nothing to zero and is never written to.

    Whatever its firmware applies is inside the arrival that gets measured, and MA can
    neither read nor remove it.
    """
    mass = _make_mass(
        _make_player("a"),
        _make_player("fixed"),
        static_delays_ms={"a": 250},
        non_adjustable={"fixed"},
    )

    session = await _start(mass, ["a", "fixed"])
    await session.stop()

    assert _delay_writes(mass) == [("a", 0), ("a", 250)]


async def test_start_writes_nothing_to_a_speaker_that_is_already_at_zero() -> None:
    """
    A speaker with no delay to take off is not written to at all.

    Its reading can not fold, and writing a 0 it already has would pin that value into
    the config of a speaker that was still tracking the default its client advertises.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"), static_delays_ms={"a": 250})

    session = await _start(mass, ["a", "b"])
    await session.stop()

    assert _delay_writes(mass) == [("a", 0), ("a", 250)]


async def test_the_delays_are_zeroed_before_the_stream_starts() -> None:
    """
    Taking the delay off shifts the client's timing, so it happens outside the measurement.

    A session that zeroed once its chirp train was already running would move the very
    phase reference the first measurements are taken against.
    """
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})

    with patch(
        "music_assistant.providers.sendspin.calibration_session.SharedPlaybackSession"
    ) as shared_cls:
        shared_cls.create_remote = AsyncMock(return_value=_make_shared_session())
        await CalibrationSession.create(_sendspin(mass), ["a"])

    assert _static_delays(mass) == {"a": 0}
    mass.player_queues.play_media.assert_not_awaited()


async def test_stop_puts_every_zeroed_delay_back() -> None:
    """
    A session that ends leaves the speakers carrying what they carried before it.

    Anything else is worse than the state the user started in, and one they may not
    notice until the music sounds wrong.
    """
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    session = await _start(mass, ["a", "b"])
    await session.solo("a")

    await session.stop()

    assert _static_delays(mass) == {"a": 250, "b": 20}


async def test_stop_puts_the_delays_back_when_stopping_the_stream_fails() -> None:
    """A stream that refuses to stop may not cost the user their static delays."""
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})
    session = await _start(mass, ["a"])
    mass.player_queues.stop.side_effect = RuntimeError("queue is gone")

    await session.stop()

    assert _static_delays(mass) == {"a": 250}


async def test_the_delay_goes_back_before_the_player_leaves_the_group() -> None:
    """
    The delay is written while the speaker is still certain to be reachable.

    Releasing a player from the calibration group and putting its power back can take
    its Sendspin client offline, and a write to a client that has gone raises.
    """
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})
    session = await _start(mass, ["a"])
    seen: list[int] = []
    _shared_session(session).remove_guest_listener.side_effect = lambda _: seen.append(
        _static_delays(mass)["a"]
    )

    await session.stop()

    assert seen == [250]


async def test_stop_restores_the_remaining_delays_when_one_write_fails() -> None:
    """One speaker that will not take its delay back can not strand the rest at zero."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    session = await _start(mass, ["a", "b"])
    # not a client that is merely away, whose delay goes into what is stored of it
    _fail_for(_sendspin(mass).set_player_static_delay, "a", RuntimeError("no answer"))

    await session.stop()

    assert _static_delays(mass) == {"a": 0, "b": 20}


async def test_the_delay_goes_back_to_the_carrier_it_came_off() -> None:
    """
    A bridged speaker gets its delay back for the protocol that carried it at the start.

    Each carrier of a speaker has a static delay of its own, so a delay taken off while
    AirPlay carried the output is no delay for the protocol that took it over since.
    """
    mass = _make_mass(_make_speaker("a", client_id="a_client"), static_delays_ms={"a_client": 250})
    _sendspin(mass).get_player_static_delay_carrier.return_value = "a_airplay"
    session = await _start(mass, ["a"])

    await session.stop()

    _sendspin(mass).set_player_static_delay.assert_awaited_with(
        "a_client", 250, carrier_id="a_airplay"
    )


async def test_a_member_is_measured_over_the_carrier_it_started_on() -> None:
    """A session knows what carried each member when it took it, also one at zero."""
    mass = _make_mass(_make_speaker("a", client_id="a_client"))
    _sendspin(mass).get_player_static_delay_carrier.return_value = "a_airplay"
    session = await _start(mass, ["a"])

    assert session.is_measured_over("a", "a_airplay") is True
    assert session.is_measured_over("a", "a_squeezelite") is False


async def test_a_member_that_vanished_gets_its_delay_back_in_what_is_stored_of_it() -> None:
    """
    A speaker that went away mid-session does not come back measuring from zero.

    The zero is persisted in its config, so it would stay advanced by nothing until
    something set it again - and the user has no reason to suspect it. Its client is
    not there to take the delay, so it goes into what is stored of the speaker.
    """
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    session = await _start(mass, ["a", "b"])
    _vanish(mass, "a")
    _sendspin(mass).away.add("a")

    await session.stop()

    assert _static_delays(mass) == {"a": 250, "b": 20}
    _sendspin(mass).store_player_static_delay.assert_called_once_with("a", 250, carrier_id=None)


async def test_a_delay_that_could_not_go_back_at_all_is_named() -> None:
    """A speaker left at zero is reported with the delay it should carry."""
    logger = MagicMock()
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})
    session = await _start(mass, ["a"], logger=logger)
    delays_ms = _static_delays(mass)
    _sendspin(mass).set_player_static_delay = AsyncMock(side_effect=RuntimeError("refused"))

    await session.stop()

    assert delays_ms == {"a": 0}
    assert any(call.args[1:] == ("a", 250) for call in logger.warning.call_args_list), (
        logger.warning.call_args_list
    )


async def test_a_vanished_member_nothing_is_stored_of_is_owed_nothing() -> None:
    """
    A browser tab that closed mid-session took its zero with it.

    Nothing is stored of such an output, so there is no delay left at zero to warn about.
    """
    logger = MagicMock()
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})
    session = await _start(mass, ["a"], logger=logger)
    _vanish(mass, "a")
    _sendspin(mass).away.add("a")
    _sendspin(mass).unstored.add("a")

    await session.stop()

    assert not any("static delay" in call.args[0] for call in logger.warning.call_args_list)


async def test_the_owner_is_told_which_delays_are_still_owed() -> None:
    """
    Every delay taken off is reported before the zero is written, and again once it is back.

    The delays are only held in memory, and the zero in their place is stored with the
    speaker, so the owner has to know about one before a server can go down over it.
    """
    owed_at_each_write: list[dict[str, int]] = []
    reported: list[dict[str, int]] = []
    mass = _make_mass(_make_player("a"), _make_player("b"), static_delays_ms={"a": 250})
    writes = _sendspin(mass).set_player_static_delay
    written = writes.side_effect

    async def _write(player_id: str, delay_ms: int, **kwargs: object) -> None:
        owed_at_each_write.append(dict(reported[-1]) if reported else {})
        await written(player_id, delay_ms, **kwargs)

    writes.side_effect = _write

    session = await _start(
        mass,
        ["a", "b"],
        on_owed_delays_changed=lambda owed: reported.append(
            {player_id: snapshot.delay_ms for player_id, snapshot in owed.items()}
        ),
    )
    assert reported[-1] == {"a": 250}
    await session.stop()

    # the zero of 'a' was written with its delay already reported, and so was its restore
    assert owed_at_each_write == [{"a": 250}, {"a": 250}]
    assert reported[-1] == {}


async def test_a_stop_that_was_cancelled_is_finished_by_the_next_one() -> None:
    """
    A server going down cancels the task a stop runs in, and the unload stops it again.

    The second stop picks up where the first was cut short, so no speaker stays silent
    or at zero, and nothing that was already put back is put back twice.
    """
    mass = _make_mass(
        _make_player("a", playback_state=PlaybackState.PLAYING),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    shared = _make_shared_session()
    session = await _start(mass, ["a", "b"], force=True, shared=shared)
    mass.players.cmd_volume_mute.side_effect = _cancel_once_for(
        mass.players.cmd_volume_mute.side_effect, "b"
    )

    with pytest.raises(asyncio.CancelledError):
        await session.stop()
    assert _silenced(mass) == {"b"}

    await session.stop()

    assert _silenced(mass) == set()
    assert _static_delays(mass) == {"a": 250, "b": 20}
    mass.players.cmd_resume.assert_awaited_once()
    shared.close.assert_awaited_once()


async def test_a_start_that_is_cancelled_gives_back_what_it_had_taken() -> None:
    """Nothing else owns the speakers of a session that never finished starting."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    shared = _make_shared_session()
    _fail_for(shared.add_guest_listener, "b", cast("Exception", asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _start(mass, ["a", "b"], shared=shared)

    assert _silenced(mass) == set()
    assert _static_delays(mass) == {"a": 250, "b": 20}
    # 'b' was cut short while joining, so it is released in case it had
    assert {call.args[0] for call in shared.remove_guest_listener.await_args_list} == {"a", "b"}


async def test_every_member_leaves_the_group_before_any_is_put_back() -> None:
    """
    A member that was synced to another one can only rejoin it once that one is out.

    Restored in the order they were taken, the follower would otherwise ask its leader
    to take it back while the leader still sits in the calibration group.
    """
    mass = _make_mass(_make_player("b", synced_to="a"), _make_player("a"))
    shared = _make_shared_session()
    order: list[str] = []
    shared.remove_guest_listener.side_effect = lambda player_id: order.append(f"out:{player_id}")
    mass.players.cmd_set_members.side_effect = lambda leader_id, **_kwargs: order.append(
        f"join:{leader_id}"
    )
    session = await _start(mass, ["b", "a"], shared=shared)

    await session.stop()

    assert order == ["out:b", "out:a", "join:a"]


async def test_a_solo_fails_when_another_member_stays_audible() -> None:
    """
    A reading taken with two speakers audible is wrong with nothing to show it.

    The target is up, so the session says so, but the caller is told the others are not
    all down instead of being handed a solo to measure in.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a", "b"])
    await session.solo("a")
    original = mass.players.cmd_volume_mute.side_effect

    async def _mute(player_id: str, muted: bool) -> None:
        if player_id == "a" and muted:
            raise PlayerUnavailableError("no answer")
        await original(player_id, muted)

    mass.players.cmd_volume_mute.side_effect = _mute

    with pytest.raises(ActionUnavailable, match="silenced"):
        await session.solo("b")

    assert session.soloed_player_id == "b"


async def test_a_solo_goes_ahead_when_another_member_has_gone() -> None:
    """A speaker that dropped off can not be silenced, and is not heard either."""
    mass = _make_mass(_make_player("a"), _make_player("b"), _make_player("c"))
    session = await _start(mass, ["a", "b", "c"])
    await session.solo("a")
    _vanish(mass, "a")

    await session.solo("b")

    assert session.soloed_player_id == "b"


async def test_a_solo_of_a_member_that_has_gone_fails() -> None:
    """A speaker that is not there yields no measurement, so its solo may not succeed."""
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a", "b"])
    _vanish(mass, "b")

    with pytest.raises(PlayerUnavailableError):
        await session.solo("b")


async def test_a_member_that_took_no_delay_at_the_start_is_not_held_to_a_carrier() -> None:
    """
    Nothing was taken off such a member, so no carrier is on record for it.

    A client that starts accepting a delay mid-session is corrected from the delay it
    carries, and is not told its output changed.
    """
    mass = _make_mass(_make_speaker("a", client_id="a_client"), non_adjustable={"a_client"})
    session = await _start(mass, ["a"])

    assert session.is_measured_over("a", "a_airplay") is True


async def test_a_correction_that_was_applied_survives_the_restore() -> None:
    """
    A written correction replaces the zero, so the pre-session delay is not put over it.

    The correction was computed to replace exactly that value; restoring it afterwards
    would land the measurement on the wrong baseline.
    """
    mass = _make_mass(_make_player("a"), static_delays_ms={"a": 250})
    session = await _start(mass, ["a"])
    await _sendspin(mass).set_player_static_delay("a", 40)
    session.keep_applied_static_delay("a")

    await session.stop()

    assert _static_delays(mass) == {"a": 40}


async def test_a_member_no_correction_landed_on_still_gets_its_delay_back() -> None:
    """Only the members a correction was actually written to are taken out of the restore."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    session = await _start(mass, ["a", "b"])
    await _sendspin(mass).set_player_static_delay("a", 40)
    session.keep_applied_static_delay("a")

    await session.stop()

    assert _static_delays(mass) == {"a": 40, "b": 20}


async def test_a_member_whose_delay_could_not_be_zeroed_is_owed_nothing_back() -> None:
    """
    A zero that never landed leaves the speaker on the delay it already had.

    The takeover fails, so the members before it are restored; the one that raised is
    only released, since putting a delay back over the one it still carries would be
    writing a value the session never took off.
    """
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    _fail_for(_sendspin(mass).set_player_static_delay, "b", PlayerUnavailableError("gone"))

    with pytest.raises(PlayerUnavailableError):
        await _start(mass, ["a", "b"])

    assert _static_delays(mass) == {"a": 250, "b": 20}
    assert _delay_writes(mass) == [("a", 0), ("b", 0), ("a", 250)]


async def test_a_failure_while_grouping_puts_back_what_it_had_already_zeroed() -> None:
    """A takeover that dies halfway leaves no speaker measuring from a delay of zero."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        static_delays_ms={"a": 250, "b": 20},
    )
    shared = _make_shared_session()
    _fail_for(shared.add_guest_listener, "b", PlayerUnavailableError("gone"))

    with pytest.raises(PlayerUnavailableError):
        await _start(mass, ["a", "b"], shared=shared)

    assert _static_delays(mass) == {"a": 250, "b": 20}


async def test_a_solo_that_cannot_make_its_target_audible_fails() -> None:
    """
    A target that can not be unmuted is an error, not a session reporting it isolated.

    An inaudible target yields no measurement at all, so the caller has to know.
    """
    mass = _make_mass(_make_player("a"), _make_player("b"))
    session = await _start(mass, ["a", "b"])
    await session.solo("a")
    _fail_for(mass.players.cmd_volume_mute, "b", PlayerUnavailableError("gone"))

    with pytest.raises(PlayerUnavailableError):
        await session.solo("b")

    assert session.soloed_player_id == "a"


async def test_a_failure_while_grouping_releases_only_what_was_taken() -> None:
    """An exception mid-takeover releases the players it grouped and leaves the rest alone."""
    mass = _make_mass(
        _make_player("a"),
        _make_player("b"),
        _make_player("c", playback_state=PlaybackState.PLAYING),
    )
    shared = _make_shared_session()
    _fail_for(shared.add_guest_listener, "b", PlayerUnavailableError("gone"))

    with pytest.raises(PlayerUnavailableError):
        await _start(mass, ["a", "b", "c"], force=True, shared=shared)

    # 'a' was grouped and is released; 'b' failed to group and 'c' was never reached,
    # so neither is restored - in particular 'c' keeps playing untouched
    assert {call.args[0] for call in shared.remove_guest_listener.await_args_list} == {"a"}
    mass.players.cmd_resume.assert_not_awaited()
    shared.close.assert_awaited_once()
    mass.player_queues.play_media.assert_not_awaited()


async def test_start_refuses_a_playing_player() -> None:
    """A session may not silently hijack music the user is listening to."""
    mass = _make_mass(_make_player("a"), _make_player("b", playback_state=PlaybackState.PLAYING))

    with pytest.raises(ActionUnavailable, match="currently playing"):
        await _start(mass, ["a", "b"])

    mass.player_queues.play_media.assert_not_awaited()


async def test_start_refuses_a_paused_player() -> None:
    """A paused speaker is holding the user's content just as much as a playing one."""
    mass = _make_mass(_make_player("a", playback_state=PlaybackState.PAUSED))

    with pytest.raises(ActionUnavailable):
        await _start(mass, ["a"])


async def test_start_takes_a_playing_player_over_when_forced() -> None:
    """An explicit force flag is what it takes to interrupt playback."""
    mass = _make_mass(_make_player("a", playback_state=PlaybackState.PLAYING))

    session = await _start(mass, ["a"], force=True)

    assert session.player_ids == ["a"]
    mass.player_queues.play_media.assert_awaited_once()


async def test_start_rejects_a_non_sendspin_player() -> None:
    """Only Sendspin speakers can be calibrated, enforced rather than documented."""
    mass = _make_mass(_make_player("a"), _make_player("b", domain="sonos"))

    with pytest.raises(UnsupportedFeaturedException):
        await _start(mass, ["a", "b"])


async def test_start_rejects_an_unknown_player() -> None:
    """A player id that resolves to nothing is an error, not a silently skipped target."""
    mass = _make_mass(_make_player("a"))

    with pytest.raises(PlayerUnavailableError):
        await _start(mass, ["a", "nope"])


async def test_start_rejects_an_empty_target_list() -> None:
    """A session needs at least one speaker to calibrate."""
    mass = _make_mass(_make_player("a"))

    with pytest.raises(InvalidDataError):
        await _start(mass, [])


async def test_start_rejects_a_duplicated_target() -> None:
    """A speaker may only appear once in a session."""
    mass = _make_mass(_make_player("a"))

    with pytest.raises(InvalidDataError):
        await _start(mass, ["a", "a"])


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"type": PlayerType.STEREO_PAIR}, True),
        ({"domain": "sonos"}, False),
        ({"available": False}, False),
        ({"type": PlayerType.GROUP}, False),
        ({"type": PlayerType.PROTOCOL}, False),
        ({"type": PlayerType.DISPLAY}, False),
        ({"type": PlayerType.VISUALIZER}, False),
        ({"type": PlayerType.LIGHT}, False),
        ({"mute_control": PLAYER_CONTROL_NONE}, True),
        ({"mute_control": PLAYER_CONTROL_NONE, "volume_control": PLAYER_CONTROL_NONE}, False),
    ],
)
async def test_eligibility(overrides: dict[str, Any], expected: bool) -> None:
    """Only available, controllable, audio-rendering Sendspin speakers are eligible."""
    player = _make_player("a", **overrides)
    assert is_eligible(_make_mass(player), player) is expected


async def test_a_physical_speaker_is_eligible_and_resolves_to_its_sendspin_client() -> None:
    """
    A speaker that plays over Sendspin qualifies, whichever provider presents it.

    A speaker is a player made up of the protocols that reach its device, which is
    the object a user picks and the session drives; its delay is written to the
    Sendspin protocol among them.
    """
    speaker = _make_speaker("living_room", client_id="ss_client")

    assert is_eligible(_make_mass(speaker), speaker) is True
    resolved = resolve_sendspin_protocol(speaker)
    assert resolved is not None
    assert resolved.protocol_id == "ss_client"


async def test_a_web_player_resolves_to_itself() -> None:
    """A Sendspin web player renders through its own protocol, which carries its delay."""
    player = _make_player("browser")

    resolved = resolve_sendspin_protocol(player)
    assert resolved is not None
    assert resolved.protocol_id == "browser"


async def test_a_speaker_that_does_not_play_over_sendspin_is_not_eligible() -> None:
    """A speaker reachable over other protocols only can not be given a Sendspin delay."""
    player = _make_player("airplay_only", domain="universal_player")

    assert resolve_sendspin_protocol(player) is None
    assert is_eligible(_make_mass(player), player) is False


async def test_a_speaker_whose_sendspin_client_cannot_take_a_stream_is_not_eligible() -> None:
    """
    A Sendspin output that is not ready to be played to is not one to calibrate against.

    Its client has gone or still needs setting up, so it can neither be measured nor
    take the correction that would come out of it.
    """
    speaker = _make_speaker("garage", client_ready=False)

    assert resolve_sendspin_protocol(speaker) is None
    assert is_eligible(_make_mass(speaker), speaker) is False


async def test_a_physical_speaker_with_neither_control_is_still_refused() -> None:
    """The isolation requirement holds for a wrapped speaker as much as for a web player."""
    speaker = _make_speaker(
        "kitchen", mute_control=PLAYER_CONTROL_NONE, volume_control=PLAYER_CONTROL_NONE
    )

    assert is_eligible(_make_mass(speaker), speaker) is False


async def test_soloing_a_physical_speaker_silences_only_the_others() -> None:
    """
    A session over wrapped speakers isolates them by the visible player it holds.

    Mute and volume resolve through the visible player to the Sendspin client, so the
    commands are issued against the id the user picked, and the stream is left alone.
    """
    mass = _make_mass(
        _make_speaker("living_room"), _make_speaker("kitchen"), _make_speaker("study")
    )
    session = await _start(mass, ["living_room", "kitchen", "study"])
    shared = _shared_session(session)
    assert [call.args[0] for call in shared.add_guest_listener.await_args_list] == [
        "living_room",
        "kitchen",
        "study",
    ]
    mass.player_queues.play_media.reset_mock()
    # the start muted all three, so the solo's own commands are what this asserts on
    mass.players.cmd_volume_mute.reset_mock()

    await session.solo("kitchen")

    assert _silenced(mass) == {"living_room", "study"}
    assert ("kitchen", True) not in _mute_calls(mass)
    mass.player_queues.play_media.assert_not_awaited()
    mass.player_queues.stop.assert_not_awaited()


async def test_a_session_mixes_wrapped_speakers_and_web_players() -> None:
    """Both kinds of Sendspin speaker are members of one session on equal terms."""
    mass = _make_mass(_make_speaker("living_room"), _make_player("browser"))

    session = await _start(mass, ["living_room", "browser"])
    await session.solo("browser")

    assert session.player_ids == ["living_room", "browser"]
    assert _silenced(mass) == {"living_room"}


async def test_the_session_anchor_is_not_itself_eligible() -> None:
    """The hidden player that leads a calibration group is not a speaker to calibrate."""
    player = _make_player(ANCHOR_ID)
    mass = _make_mass(player)
    mass.get_provider.return_value.is_virtual_player = MagicMock(return_value=True)
    assert is_eligible(mass, player) is False


async def test_a_speaker_that_cannot_take_a_static_delay_is_still_eligible() -> None:
    """
    A speaker whose client carries no static delay is measured like any other.

    Offsets are relative, so leaving it out would take it out of the picture the rest
    are normalised against rather than merely deny it a correction.
    """
    player = _make_player("a")
    mass = _make_mass(player)
    mass.get_provider.return_value.supports_player_static_delay = MagicMock(return_value=False)

    assert is_eligible(mass, player) is True


async def test_start_accepts_a_speaker_that_cannot_take_a_static_delay() -> None:
    """A session runs over such a speaker; the result comes back for the user to apply."""
    mass = _make_mass(_make_player("a"))
    mass.get_provider.return_value.supports_player_static_delay = MagicMock(return_value=False)

    session = await _start(mass, ["a"])

    assert session.player_ids == ["a"]


async def _start(
    mass: MagicMock,
    player_ids: list[str],
    force: bool = False,
    shared: MagicMock | None = None,
    logger: MagicMock | None = None,
    on_owed_delays_changed: Callable[[Mapping[str, StaticDelaySnapshot]], None] | None = None,
) -> CalibrationSession:
    """
    Return a started, streaming session anchored on a stubbed shared playback session.

    :param mass: The stub MusicAssistant the session runs against.
    :param player_ids: The players to calibrate.
    :param force: Take over players that are busy playing.
    :param shared: Anchor stub to use, so a caller can inspect it after a failed start.
    :param logger: Logger stub to use, so a caller can inspect what the session reported.
    :param on_owed_delays_changed: Told which static delays the session still owes.
    """
    with patch(
        "music_assistant.providers.sendspin.calibration_session.SharedPlaybackSession"
    ) as shared_cls:
        shared_cls.create_remote = AsyncMock(return_value=shared or _make_shared_session())
        sendspin = _sendspin(mass)
        if logger is not None:
            sendspin.logger = logger
        session = await CalibrationSession.create(
            sendspin,
            player_ids,
            force=force,
            on_owed_delays_changed=on_owed_delays_changed,
        )
    await session.begin(MagicMock())
    return session


def _vanish(mass: MagicMock, player_id: str) -> None:
    """
    Make one player unknown to the given stub MusicAssistant, as a disconnect does.

    :param mass: The stub MusicAssistant to take the player out of.
    :param player_id: The player that has gone.
    """
    known = mass.players.get_player.side_effect

    def _get_player(wanted_id: str, *args: object) -> MagicMock | None:
        return None if wanted_id == player_id else cast("MagicMock | None", known(wanted_id, *args))

    mass.players.get_player = MagicMock(side_effect=_get_player)


def _fail_for(mock: AsyncMock, player_id: str, error: Exception) -> None:
    """
    Make a player command raise for one player id, keeping its behaviour for the rest.

    :param mock: The stubbed command to make fail.
    :param player_id: The player the command should raise for.
    :param error: The error to raise for that player.
    """
    original = mock.side_effect

    async def _side_effect(target_id: str, *args: object, **kwargs: object) -> None:
        if target_id == player_id:
            raise error
        if original is not None:
            await original(target_id, *args, **kwargs)

    mock.side_effect = _side_effect


def _cancel_once_for(original: Any, player_id: str) -> Any:
    """
    Return a player command that is cancelled the first time it is sent to one player.

    :param original: The stubbed command to wrap.
    :param player_id: The player whose first command is cut short.
    """
    pending = [player_id]

    async def _side_effect(target_id: str, *args: object) -> None:
        if target_id in pending:
            pending.remove(target_id)
            raise asyncio.CancelledError
        await original(target_id, *args)

    return _side_effect


def _make_shared_session() -> MagicMock:
    """Return a stub of the shared playback session that anchors a calibration group."""
    shared = MagicMock()
    shared.player_id = ANCHOR_ID
    shared.queue_id = ANCHOR_ID
    shared.add_guest_listener = AsyncMock()
    shared.remove_guest_listener = AsyncMock()
    shared.close = AsyncMock()
    return shared


def _make_speaker(
    player_id: str,
    *,
    client_id: str | None = None,
    client_ready: bool = True,
    **overrides: object,
) -> MagicMock:
    """
    Return a stub of a physical speaker: a player with a Sendspin protocol among its outputs.

    A session holds the player and resolves the Sendspin protocol it renders through
    for the static delay.

    :param player_id: Id and display name of the player.
    :param client_id: Id of the Sendspin protocol of the player.
    :param client_ready: Whether that protocol can currently take a stream.
    :param overrides: PlayerState attributes to set on the player.
    """
    player = _make_player(player_id, domain="universal_player", **overrides)
    client_id = client_id or f"{player_id}_client"
    protocol = OutputProtocol(
        output_protocol_id=client_id,
        name="Sendspin",
        protocol_domain="sendspin",
        available=client_ready,
    )
    client = MagicMock()
    client.protocol_id = client_id
    player.get_output_protocol_by_domain = MagicMock(
        side_effect=lambda domain: protocol if domain == "sendspin" else None
    )
    player.get_output_protocol = MagicMock(
        side_effect=lambda protocol_id: client if protocol_id == client_id else None
    )
    return player


def _make_player(player_id: str, *, domain: str = "sendspin", **overrides: object) -> MagicMock:
    """
    Return a stub player carrying just the state a calibration session reads.

    :param player_id: Id and display name of the stub player.
    :param domain: Domain of the player provider the stub belongs to.
    :param overrides: PlayerState attributes to set on top of an idle, unmuted speaker.
    """
    player = MagicMock()
    player.player_id = player_id
    player.extra_data = {}
    # a Sendspin web or app player renders through its own protocol, which has its id.
    # A player of another provider renders over Sendspin only if something says so;
    # _make_speaker is what wires that up
    native = OutputProtocol(
        output_protocol_id="native", name="Sendspin", protocol_domain=domain, is_native=True
    )
    own = MagicMock()
    own.protocol_id = player_id
    is_sendspin = domain == "sendspin"
    player.get_output_protocol_by_domain = MagicMock(
        side_effect=lambda asked: native if is_sendspin and asked == "sendspin" else None
    )
    player.get_output_protocol = MagicMock(
        side_effect=lambda output_id: own if is_sendspin and output_id == "native" else None
    )
    state: dict[str, object] = {
        "name": player_id,
        "available": True,
        "type": PlayerType.PLAYER,
        "playback_state": PlaybackState.IDLE,
        "powered": True,
        "power_control": PLAYER_CONTROL_NATIVE,
        "active_source": None,
        "current_media": None,
        "volume_muted": False,
        "volume_level": 42,
        "synced_to": None,
        "active_group": None,
        "mute_control": PLAYER_CONTROL_NATIVE,
        "volume_control": PLAYER_CONTROL_NATIVE,
        "supported_features": {PlayerFeature.SET_MEMBERS},
    }
    if "powered" in overrides and overrides["powered"] is None:
        # a player without power control reports no power state either
        state["power_control"] = PLAYER_CONTROL_NONE
    for key, value in (state | overrides).items():
        setattr(player.state, key, value)
    return player


def _make_mass(
    *players: MagicMock,
    optimistic_volume: bool = True,
    static_delays_ms: dict[str, int] | None = None,
    non_adjustable: set[str] | None = None,
) -> MagicMock:
    """
    Return a stub MusicAssistant whose mute and volume commands move player state.

    :param players: The stub players to register.
    :param optimistic_volume: Whether a volume command lands in player state immediately.
        Sendspin's does not - it waits for the client to acknowledge it.
    :param static_delays_ms: The static delay each *Sendspin* player starts out carrying,
        keyed by that player's id and defaulting to 0. Reads and writes hit the same
        mapping, so :func:`_static_delays` reports what a session left behind.
    :param non_adjustable: Sendspin players whose client refuses a static delay. Reading
        one raises, exactly as the real provider does.
    """
    registry = {player.player_id: player for player in players}
    mass = MagicMock()

    async def _mute(player_id: str, muted: bool) -> None:
        registry[player_id].state.volume_muted = muted

    async def _set_volume(player_id: str, volume_level: int) -> None:
        if not optimistic_volume:
            return
        registry[player_id].state.volume_level = volume_level
        registry[player_id].state.volume_muted = False

    mass.players.get_player = MagicMock(side_effect=lambda player_id, *_: registry.get(player_id))
    mass.players.all_players = MagicMock(return_value=list(players))
    mass.players.cmd_volume_mute = AsyncMock(side_effect=_mute)
    mass.players.cmd_volume_set = AsyncMock(side_effect=_set_volume)
    mass.players.cmd_set_members = AsyncMock()
    mass.players.cmd_power = AsyncMock()
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_resume = AsyncMock()
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.stop = AsyncMock()
    mass.player_queues.get = MagicMock(return_value=_make_anchor_queue())
    refused = non_adjustable or set()
    delays_ms = dict(static_delays_ms or {})

    def _get_delay(player_id: str) -> int:
        if player_id in refused:
            raise UnsupportedFeaturedException(player_id)
        return delays_ms.get(player_id, 0)

    away: set[str] = set()
    unstored: set[str] = set()

    async def _set_delay(player_id: str, delay_ms: int, **_kwargs: object) -> None:
        if player_id in away:
            raise PlayerUnavailableError(player_id)
        if player_id in refused:
            raise UnsupportedFeaturedException(player_id)
        delays_ms[player_id] = delay_ms

    def _store_delay(player_id: str, delay_ms: int, **_kwargs: object) -> bool:
        if player_id in unstored:
            return False
        delays_ms[player_id] = delay_ms
        return True

    sendspin = MagicMock()
    # Sendspin outputs whose client is gone, so a delay can only go into what is stored
    # of them, and those nothing is stored of
    sendspin.away = away
    sendspin.unstored = unstored
    sendspin.store_player_static_delay = MagicMock(side_effect=_store_delay)
    sendspin.is_virtual_player = MagicMock(return_value=False)
    sendspin.supports_player_static_delay = MagicMock(side_effect=lambda p: p not in refused)
    sendspin.get_player_static_delay = MagicMock(side_effect=_get_delay)
    sendspin.get_player_static_delay_carrier = MagicMock(return_value=None)
    sendspin.set_player_static_delay = AsyncMock(side_effect=_set_delay)
    sendspin.static_delays_ms = delays_ms
    sendspin.mass = mass
    mass.get_provider = MagicMock(return_value=sendspin)
    return mass


def _make_anchor_queue() -> MagicMock:
    """Return a stub anchor queue that is playing the calibration track."""
    queue = MagicMock()
    queue.state = PlaybackState.PLAYING
    queue.current_item.media_type = MediaType.AUDIO_SOURCE
    return queue


def _shared_session(session: CalibrationSession) -> MagicMock:
    """Return the stubbed shared playback session backing the given session."""
    return cast("MagicMock", session._shared_session)


def _sendspin(mass: MagicMock) -> MagicMock:
    """Return the stub Sendspin provider static delays are read and written through."""
    return cast("MagicMock", mass.get_provider.return_value)


def _static_delays(mass: MagicMock) -> dict[str, int]:
    """Return the static delay every Sendspin player of the given stub now carries."""
    return cast("dict[str, int]", _sendspin(mass).static_delays_ms)


def _delay_writes(mass: MagicMock) -> list[tuple[str, int]]:
    """Return every (sendspin_player_id, delay_ms) pair written, in order."""
    return [
        (call.args[0], call.args[1])
        for call in _sendspin(mass).set_player_static_delay.await_args_list
    ]


def _mute_calls(mass: MagicMock) -> list[tuple[str, bool]]:
    """Return every (player_id, muted) pair passed to cmd_volume_mute."""
    return [(call.args[0], call.args[1]) for call in mass.players.cmd_volume_mute.await_args_list]


def _volume_calls(mass: MagicMock) -> list[tuple[str, int]]:
    """Return every (player_id, volume_level) pair passed to cmd_volume_set."""
    return [(call.args[0], call.args[1]) for call in mass.players.cmd_volume_set.await_args_list]


def _mute_command_targets(mass: MagicMock) -> set[str]:
    """Return the ids of every player a mute command was issued against."""
    return {player_id for player_id, _ in _mute_calls(mass)}


def _silenced(mass: MagicMock) -> set[str]:
    """Return the ids of every player that is currently inaudible."""
    return {
        player.player_id
        for player in _registered_players(mass)
        if player.state.volume_muted or player.state.volume_level == 0
    }


def _registered_players(mass: MagicMock) -> Iterable[MagicMock]:
    """Return the stub players registered on the given stub MusicAssistant."""
    return cast("list[MagicMock]", mass.players.all_players.return_value)
