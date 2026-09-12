"""
Tests for source (de)selection and its effect on future play targets.

Regression coverage for a bug where the protocol player MA picked to actually
consume a stream (e.g. a sync group's leader) leaked into becoming the play
target for the *next* session, silently skipping the group and starving every
other member.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.ariacast_receiver import AUDIO_SOURCE_ID, AriaCastReceiver


def _selection_receiver(
    *,
    active_player_id: str | None,
    in_use_by_player: str | None = None,
    active_session_id: str | None = None,
    session_player_id: str | None = None,
) -> SimpleNamespace:
    """Build a bare receiver namespace for driving on_source_(un)selected."""
    return SimpleNamespace(
        _active_player_id=active_player_id,
        _in_use_by_player=in_use_by_player,
        _active_session_id=active_session_id,
        _session_player_id=session_player_id,
    )


async def _select(receiver: SimpleNamespace, player_id: str, owner: str, session: str) -> None:
    await AriaCastReceiver.on_source_selected(
        cast("AriaCastReceiver", receiver), AUDIO_SOURCE_ID, player_id, owner, session
    )


async def _unselect(receiver: SimpleNamespace, owner: str, session: str) -> None:
    await AriaCastReceiver.on_source_unselected(
        cast("AriaCastReceiver", receiver), AUDIO_SOURCE_ID, owner, session
    )


async def test_on_source_selected_tracks_the_stream_player_separately() -> None:
    """The consuming protocol player is recorded on its own field."""
    receiver = _selection_receiver(active_player_id="group_1")

    await _select(receiver, "leaf_1", "group_1", "session-a")

    assert receiver._session_player_id == "leaf_1"
    assert receiver._active_player_id == "group_1"
    assert receiver._in_use_by_player == "group_1"
    assert receiver._active_session_id == "session-a"


async def test_on_source_selected_ignores_other_sources() -> None:
    """A selection for a different source id is not this plugin's concern."""
    receiver = _selection_receiver(active_player_id="group_1")

    await AriaCastReceiver.on_source_selected(
        cast("AriaCastReceiver", receiver), "some_other_source", "leaf_1", "group_1", "session-a"
    )

    assert receiver._session_player_id is None
    assert receiver._active_session_id is None


async def test_on_source_unselected_clears_the_stream_player_for_the_matching_session() -> None:
    """Ending the session that owns the stream releases the tracked stream player too."""
    receiver = _selection_receiver(
        active_player_id="group_1",
        in_use_by_player="group_1",
        active_session_id="session-a",
        session_player_id="leaf_1",
    )

    await _unselect(receiver, "group_1", "session-a")

    assert receiver._active_session_id is None
    assert receiver._session_player_id is None
    assert receiver._in_use_by_player is None
    # the play target for the next session is untouched
    assert receiver._active_player_id == "group_1"


async def test_on_source_unselected_ignores_a_stale_session() -> None:
    """A teardown for a session that already got replaced must not clear the live one."""
    receiver = _selection_receiver(
        active_player_id="group_1",
        in_use_by_player="group_1",
        active_session_id="session-b",
        session_player_id="leaf_2",
    )

    await _unselect(receiver, "group_1", "session-a")

    assert receiver._active_session_id == "session-b"
    assert receiver._session_player_id == "leaf_2"
    assert receiver._in_use_by_player == "group_1"


def _pause_receiver(
    *, session_player_id: str | None, active_player_id: str | None
) -> SimpleNamespace:
    """Build a bare receiver namespace for driving _cmd_pause."""
    return SimpleNamespace(
        _session_player_id=session_player_id,
        _active_player_id=active_player_id,
        _in_use_by_player="group_1",
        _is_playing=True,
        _forward_action=AsyncMock(),
        _broadcast_meta=AsyncMock(),
        mass=MagicMock(),
        logger=MagicMock(),
    )


async def _pause(receiver: SimpleNamespace) -> None:
    receiver.mass.players.cmd_stop = AsyncMock()
    await AriaCastReceiver._cmd_pause(cast("AriaCastReceiver", receiver))


async def test_cmd_pause_stops_the_stream_player_not_the_play_target() -> None:
    """Pause stops the protocol player actually holding the stream."""
    receiver = _pause_receiver(session_player_id="leaf_1", active_player_id="group_1")

    await _pause(receiver)

    receiver.mass.players.cmd_stop.assert_awaited_once_with("leaf_1")
    assert receiver._in_use_by_player is None
    assert receiver._is_playing is False


async def test_cmd_pause_falls_back_to_the_play_target_without_a_live_session() -> None:
    """No stream-consuming player was ever tracked, so pause targets the play target."""
    receiver = _pause_receiver(session_player_id=None, active_player_id="group_1")

    await _pause(receiver)

    receiver.mass.players.cmd_stop.assert_awaited_once_with("group_1")


async def test_cmd_pause_does_not_stop_anything_without_a_known_player() -> None:
    """Pause still acks/forwards even when nothing was ever selected."""
    receiver = _pause_receiver(session_player_id=None, active_player_id=None)

    await _pause(receiver)

    receiver.mass.players.cmd_stop.assert_not_awaited()
    receiver._forward_action.assert_awaited_once_with("pause")
    receiver._broadcast_meta.assert_awaited_once()


async def test_a_second_session_replays_on_the_original_target_not_the_previous_leaf() -> None:
    """A second AriaCast session must not stick to the previous leaf player."""
    mass = MagicMock()
    receiver = SimpleNamespace(
        _is_playing=False,
        _in_use_by_player=None,
        _active_player_id=None,
        _active_session_id=None,
        _session_player_id=None,
        _get_target_player_id=MagicMock(return_value="group_1"),
        _safe_play_media=AsyncMock(),
        _broadcast_meta=AsyncMock(),
        mass=mass,
        logger=MagicMock(),
        instance_id="ariacast_receiver--test",
    )

    async def _handle_playback_state(is_playing: bool) -> None:
        recv = cast("AriaCastReceiver", receiver)
        await AriaCastReceiver._handle_playback_state(recv, is_playing)

    # --- first session: sender starts, group forms, leaf_1 ends up consuming it ---
    await _handle_playback_state(True)
    assert receiver._active_player_id == "group_1"
    # mimic the queue handing the actual stream to the group's sync leader
    await _select(cast("Any", receiver), "leaf_1", "group_1", "session-a")
    assert receiver._session_player_id == "leaf_1"

    # --- first session ends: sender disconnects, source is unselected ---
    await _unselect(cast("Any", receiver), "group_1", "session-a")
    assert receiver._in_use_by_player is None

    # --- second session: sender reconnects and starts playing again ---
    receiver._safe_play_media.reset_mock()
    await _handle_playback_state(True)

    receiver._safe_play_media.assert_called_once_with("group_1")
