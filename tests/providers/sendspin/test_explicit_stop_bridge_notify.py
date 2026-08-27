"""
Tests for bridge players being told about an explicit stop (support#6195).

A bridge player buffers seconds of audio on its downstream protocol and keeps
that transport warm across stream ends, so seeks and track changes can reuse
it. At the Sendspin server level a user stop is indistinguishable from those:
the truthful signal lives in the player commands, so SendspinPlayer.stop() and
set_members() notify the bridge roles, which is what lets a bridge silence its
device at once instead of playing out its buffer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sendspin.bridge_role import BridgePlayerRole
from music_assistant.providers.sendspin.player import SendspinPlayer


def _player_mock() -> MagicMock:
    """Create a mock to bind the real methods under test to."""
    mock = MagicMock()
    mock.playback_session.cancel = AsyncMock()
    mock.api.group.stop = AsyncMock()
    mock.api.group.remove_client = AsyncMock()
    return mock


def _bridge_role(on_explicit_stop: MagicMock | None) -> BridgePlayerRole:
    """Build a bridge role with only the explicit-stop callback of interest."""
    role = BridgePlayerRole(client=MagicMock())
    role.set_callbacks(
        on_audio_chunk=MagicMock(),
        on_volume_change=MagicMock(),
        on_mute_change=MagicMock(),
        on_stream_start=MagicMock(),
        on_stream_end=MagicMock(),
        on_explicit_stop=on_explicit_stop,
    )
    return role


async def test_stop_notifies_bridges_between_group_stop_and_session_cancel() -> None:
    """
    The notify lands after the stream ended and before the session teardown.

    Before group.stop() the bridge would still see itself streaming and ignore
    the signal; after the session cancel it would have spent that long playing
    out its buffer.
    """
    mock = _player_mock()
    order: list[str] = []
    mock.api.group.stop.side_effect = lambda: order.append("group_stop")
    mock._notify_bridges_explicit_stop = MagicMock(side_effect=lambda _: order.append("notify"))
    mock.playback_session.cancel.side_effect = lambda _reason: order.append("cancel")

    await SendspinPlayer.stop(mock)

    assert order == ["group_stop", "notify", "cancel"]
    mock._notify_bridges_explicit_stop.assert_called_once_with(mock.api.group.clients)


async def test_stop_notifies_bridges_even_when_the_group_stop_fails() -> None:
    """A failing group stop must not leave a bridge playing out its buffer."""
    mock = _player_mock()
    mock.api.group.stop = AsyncMock(side_effect=RuntimeError("transport gone"))

    with pytest.raises(RuntimeError):
        await SendspinPlayer.stop(mock)

    mock._notify_bridges_explicit_stop.assert_called_once()
    mock.playback_session.cancel.assert_awaited_once_with("stop command")


async def test_removing_a_member_notifies_its_bridge_after_the_removal() -> None:
    """
    An ungrouped member is told to stop once its stream has been ended.

    The removal is what fires the stream end for the member's roles; notifying
    before it would find the bridge still streaming and be ignored.
    """
    mock = _player_mock()
    member = MagicMock()
    mock.mass.players.get_player.return_value = member
    order: list[str] = []
    mock.api.group.remove_client.side_effect = lambda _client: order.append("remove")
    mock._notify_bridges_explicit_stop = MagicMock(side_effect=lambda _: order.append("notify"))

    await SendspinPlayer.set_members(mock, player_ids_to_remove=["member1"])

    assert order == ["remove", "notify"]
    mock._notify_bridges_explicit_stop.assert_called_once_with([member.api])


def test_notify_reaches_only_bridge_roles() -> None:
    """Native player roles have their own stream/end handling and are left alone."""
    mock = _player_mock()
    on_explicit_stop = MagicMock()
    native_role = MagicMock()
    client = MagicMock()
    client.roles_by_family.return_value = [native_role, _bridge_role(on_explicit_stop)]

    SendspinPlayer._notify_bridges_explicit_stop(mock, [client])

    on_explicit_stop.assert_called_once_with()
    native_role.notify_explicit_stop.assert_not_called()


def test_a_failing_bridge_does_not_keep_the_stop_from_the_others() -> None:
    """One bridge raising must not leave the next member playing out its buffer."""
    mock = _player_mock()
    failing_client = MagicMock()
    failing_client.roles_by_family.return_value = [
        _bridge_role(MagicMock(side_effect=RuntimeError("bridge broke")))
    ]
    on_explicit_stop = MagicMock()
    healthy_client = MagicMock()
    healthy_client.roles_by_family.return_value = [_bridge_role(on_explicit_stop)]

    SendspinPlayer._notify_bridges_explicit_stop(mock, [failing_client, healthy_client])

    on_explicit_stop.assert_called_once_with()
    mock.logger.exception.assert_called_once()


def test_a_role_without_the_callback_ignores_the_notify() -> None:
    """A bridge that did not wire the callback (the Cast bridge) is unaffected."""
    role = _bridge_role(None)

    role.notify_explicit_stop()
