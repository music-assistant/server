"""
Unit tests for the volume and mute state held by the Sendspin bridge player role.

The role's copy of the volume and mute is what the visible Sendspin player
reports, and it moves in two directions: a command coming down from Sendspin,
which the bridge has to forward to its player, and state observed on the player
itself, which has to reach the Sendspin side without being pushed back at the
player it was just read from.
"""

from unittest.mock import MagicMock

from aiosendspin.server import VolumeChangedEvent

from music_assistant.providers.sendspin.bridge_role import BridgePlayerRole


def _make_role(
    *, initial_volume: int = 40, initial_muted: bool = False
) -> tuple[BridgePlayerRole, MagicMock, MagicMock, MagicMock]:
    """Build a wired-up bridge role with its client mock and volume/mute callbacks."""
    client = MagicMock()
    role = BridgePlayerRole(client=client)
    on_volume_change = MagicMock()
    on_mute_change = MagicMock()
    role.set_callbacks(
        on_audio_chunk=MagicMock(),
        on_volume_change=on_volume_change,
        on_mute_change=on_mute_change,
        on_stream_start=MagicMock(),
        on_stream_end=MagicMock(),
        initial_volume=initial_volume,
        initial_muted=initial_muted,
    )
    client.reset_mock()
    return role, client, on_volume_change, on_mute_change


def _emitted_volume_events(client: MagicMock) -> list[VolumeChangedEvent]:
    """Return the volume-changed events the role emitted on its client."""
    return [
        call.args[0]
        for call in client._signal_event.call_args_list
        if isinstance(call.args[0], VolumeChangedEvent)
    ]


def test_wiring_the_callbacks_seeds_the_state_the_player_is_in() -> None:
    """A bridge registering a muted player starts out reporting it as muted."""
    role, _, _, _ = _make_role(initial_volume=35, initial_muted=True)

    assert role.get_player_volume() == 35
    assert role.get_player_muted() is True


def test_a_command_from_sendspin_is_forwarded_to_the_bridge() -> None:
    """A volume or mute command is applied and handed to the bridge to carry out."""
    role, client, on_volume_change, on_mute_change = _make_role()

    role.set_player_volume(70)
    role.set_player_mute(True)

    assert role.get_player_volume() == 70
    assert role.get_player_muted() is True
    on_volume_change.assert_called_once_with(70)
    on_mute_change.assert_called_once_with(True)
    assert [(event.volume, event.muted) for event in _emitted_volume_events(client)] == [
        (70, False),
        (70, True),
    ]


def test_state_observed_on_the_player_is_not_pushed_back_at_it() -> None:
    """
    State read off the player reaches Sendspin without being sent back down.

    Forwarding it would hand the player a value it just reported, which for a
    device that answers every volume command with feedback never settles.
    """
    role, client, on_volume_change, on_mute_change = _make_role()

    role.update_player_state(volume=55, muted=True)

    assert role.get_player_volume() == 55
    assert role.get_player_muted() is True
    on_volume_change.assert_not_called()
    on_mute_change.assert_not_called()
    assert [(event.volume, event.muted) for event in _emitted_volume_events(client)] == [(55, True)]


def test_only_the_state_that_is_known_is_adopted() -> None:
    """A player that cannot report a level keeps the level the role already has."""
    role, _, _, _ = _make_role(initial_volume=40)

    role.update_player_state(volume=None, muted=True)

    assert role.get_player_volume() == 40
    assert role.get_player_muted() is True


def test_unchanged_state_is_not_announced_again() -> None:
    """Re-reading the same volume and mute off the player is not an update."""
    role, client, _, _ = _make_role(initial_volume=40, initial_muted=False)

    role.update_player_state(volume=40, muted=False)

    assert _emitted_volume_events(client) == []
