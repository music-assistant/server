"""Tests for the inbound /control command handling of the AriaCast receiver."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.ariacast_receiver import AriaCastReceiver


def _receiver(in_use_by_queue: str | None = "queue_1") -> SimpleNamespace:
    """Build a bare receiver namespace for driving _handle_inbound_control."""
    return SimpleNamespace(
        _in_use_by_player=in_use_by_queue,
        _forward_action=AsyncMock(),
        _cmd_play=AsyncMock(),
        _cmd_pause=AsyncMock(),
        mass=MagicMock(),
        logger=MagicMock(),
    )


async def _handle(receiver: SimpleNamespace, ws: Any, payload: dict[str, Any]) -> None:
    """Run the inbound control handler on the bare receiver."""
    await AriaCastReceiver._handle_inbound_control(cast("AriaCastReceiver", receiver), ws, payload)


async def test_inbound_next_relays_to_the_other_senders() -> None:
    """
    A control client's next/previous is relayed to the other control clients.

    It must never be routed through player_queues: the queue delegates transport
    commands for an active AudioSource back to this plugin, which would echo the
    command straight back to its originator.
    """
    receiver = _receiver()
    ws = AsyncMock()

    await _handle(receiver, ws, {"command": "next"})
    await _handle(receiver, ws, {"action": "previous"})

    assert receiver._forward_action.await_args_list == [
        (("next",), {"exclude": ws}),
        (("previous",), {"exclude": ws}),
    ]
    receiver.mass.player_queues.next.assert_not_called()
    receiver.mass.player_queues.previous.assert_not_called()


async def test_ack_shaped_payloads_are_not_dispatched_as_commands() -> None:
    """
    A reply/ack payload (carrying "success") is ignored, not relayed as a command.

    A client acking a broadcast action with the server's own ack shape would
    otherwise trigger a fresh relay — and loop on a client that acks the ack.
    """
    receiver = _receiver()
    ws = AsyncMock()

    await _handle(receiver, ws, {"action": "next", "success": True})
    await _handle(receiver, ws, {"command": "play", "success": False})

    receiver._forward_action.assert_not_awaited()
    receiver._cmd_play.assert_not_awaited()
    ws.send_json.assert_not_awaited()


async def test_inbound_next_is_ignored_while_the_source_is_not_in_use() -> None:
    """Next/previous do nothing while no MA queue is playing the source."""
    receiver = _receiver(in_use_by_queue=None)
    ws = AsyncMock()

    await _handle(receiver, ws, {"command": "next"})

    receiver._forward_action.assert_not_awaited()


async def test_forward_action_excludes_the_originating_sender() -> None:
    """The relayed action reaches every control client except the one that sent it."""
    originator = AsyncMock()
    other = AsyncMock()
    receiver = SimpleNamespace(_control_senders={originator, other})

    await AriaCastReceiver._forward_action(
        cast("AriaCastReceiver", receiver), "next", exclude=originator
    )

    other.send_json.assert_awaited_once_with({"action": "next"})
    originator.send_json.assert_not_awaited()


async def test_forward_action_without_exclusion_reaches_all_senders() -> None:
    """An MA-initiated action (no originator) is broadcast to every control client."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    receiver = SimpleNamespace(_control_senders={ws1, ws2})

    await AriaCastReceiver._forward_action(cast("AriaCastReceiver", receiver), "next")

    ws1.send_json.assert_awaited_once_with({"action": "next"})
    ws2.send_json.assert_awaited_once_with({"action": "next"})


def _playback_receiver(*, active_player_id: str | None, owner: str | None) -> SimpleNamespace:
    """Build a bare receiver namespace for driving _handle_playback_state."""
    return SimpleNamespace(
        instance_id="ariacast_receiver--test",
        _is_playing=True,
        _in_use_by_player=owner,
        _active_player_id=active_player_id,
        _get_target_player_id=MagicMock(return_value=None),
        _safe_play_media=AsyncMock(),
        _broadcast_meta=AsyncMock(),
        mass=MagicMock(),
        logger=MagicMock(),
    )


async def _playback(receiver: SimpleNamespace, is_playing: bool) -> None:
    """Run the playback-state handler on the bare receiver."""
    await AriaCastReceiver._handle_playback_state(cast("AriaCastReceiver", receiver), is_playing)


async def test_the_sender_stopping_gives_the_source_back_to_its_owner() -> None:
    """
    The owner is released, not the player the audio was consumed over.

    _active_player_id can be an ephemeral protocol player; the source session hangs
    off the owner, so deselecting the bridge would leave that session alive and the
    owner's own queue stuck inactive.
    """
    receiver = _playback_receiver(active_player_id="spb_bridge_1", owner="owner-player")

    await _playback(receiver, False)

    receiver.mass.players.deselect_source.assert_called_once_with(
        "owner-player", provider_instance_id=receiver.instance_id
    )
    assert receiver._in_use_by_player is None


async def test_a_sender_stopping_while_not_in_use_releases_nothing() -> None:
    """Without an owner there is no session to give back."""
    receiver = _playback_receiver(active_player_id="some-player", owner=None)

    await _playback(receiver, False)

    receiver.mass.players.deselect_source.assert_not_called()
