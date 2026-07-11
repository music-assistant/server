"""Tests for the shared playback session helper."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import SetupFailedError, UnsupportedFeaturedException

from music_assistant.helpers.shared_playback import SharedPlaybackMode, SharedPlaybackSession

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider


def _create_mock_mass(venue_player: MagicMock | None) -> MagicMock:
    """Create a mock MusicAssistant with a mocked players controller."""
    mass = MagicMock()
    mass.players.get_player.return_value = venue_player
    mass.players.cmd_set_members = AsyncMock()
    mass.players.register_audio_only_player = MagicMock()
    mass.players.unregister_audio_only_player = MagicMock()
    return mass


def _create_venue_player(
    *,
    can_group_with: set[str] | None = None,
    group_members: list[str] | None = None,
    supports_set_members: bool = True,
) -> MagicMock:
    """Create a mock venue player with the given grouping capabilities."""
    player = MagicMock()
    player.state.available = True
    player.state.supported_features = {PlayerFeature.SET_MEMBERS} if supports_set_members else set()
    player.state.can_group_with = can_group_with or set()
    player.state.group_members = group_members or []
    return player


# ==================== VENUE mode ====================


async def test_create_venue_session() -> None:
    """A venue session exposes the venue player's player/queue id."""
    mass = _create_mock_mass(_create_venue_player())

    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.mode == SharedPlaybackMode.VENUE
    assert session.player_id == "venue_player"
    assert session.queue_id == "venue_player"


async def test_create_venue_unknown_player() -> None:
    """Creating a venue session for an unknown player raises."""
    mass = _create_mock_mass(None)

    with pytest.raises(SetupFailedError):
        await SharedPlaybackSession.create_venue(mass, "unknown_player")


async def test_venue_can_listen_in_feature_detection() -> None:
    """Listen-in is only possible when the venue player can group with the web player."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is True
    assert session.can_listen_in("web_player_2") is False


async def test_venue_can_listen_in_already_grouped() -> None:
    """A web player that is already a group member can (still) listen in."""
    venue_player = _create_venue_player(group_members=["web_player_1"])
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is True


async def test_venue_can_listen_in_no_set_members() -> None:
    """Listen-in is not possible when the venue player does not support grouping."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"}, supports_set_members=False)
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    assert session.can_listen_in("web_player_1") is False


async def test_venue_add_and_remove_guest_listener() -> None:
    """Guest listeners are attached/detached via the players controller."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    await session.add_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_add=["web_player_1"]
    )

    await session.remove_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_remove=["web_player_1"]
    )
    mass.players.register_audio_only_player.assert_not_called()
    mass.players.unregister_audio_only_player.assert_not_called()


async def test_audio_only_registration_wraps_group_membership() -> None:
    """Audio-only registration starts before grouping and ends after ungrouping."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    events: list[tuple[str, bool | None]] = []
    mass.players.register_audio_only_player.side_effect = lambda _player_id: events.append(
        ("audio_only", True)
    )
    mass.players.unregister_audio_only_player.side_effect = lambda _player_id: events.append(
        ("audio_only", False)
    )

    async def record_grouping(
        _player_id: str,
        *,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        assert (player_ids_to_add is None) != (player_ids_to_remove is None)
        events.append(("group", player_ids_to_remove is None))

    mass.players.cmd_set_members.side_effect = record_grouping
    session = await SharedPlaybackSession.create_venue(mass, "venue_player", audio_only=True)

    await session.add_guest_listener("web_player_1")
    await session.add_guest_listener("web_player_1")
    await session.remove_guest_listener("web_player_1")
    await session.remove_guest_listener("web_player_1")

    assert events == [
        ("audio_only", True),
        ("group", True),
        ("group", False),
        ("audio_only", False),
    ]


async def test_audio_only_registration_rolls_back_on_grouping_failure() -> None:
    """A failed group operation immediately restores normal metadata behavior."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    mass.players.cmd_set_members.side_effect = RuntimeError("grouping failed")
    session = await SharedPlaybackSession.create_venue(mass, "venue_player", audio_only=True)

    with pytest.raises(RuntimeError, match="grouping failed"):
        await session.add_guest_listener("web_player_1")

    mass.players.register_audio_only_player.assert_called_once_with("web_player_1")
    mass.players.unregister_audio_only_player.assert_called_once_with("web_player_1")


async def test_audio_only_close_restores_listeners_after_detach() -> None:
    """Closing an audio-only venue session restores every tracked guest."""
    venue_player = _create_venue_player(can_group_with={"web_player_1", "web_player_2"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player", audio_only=True)
    await session.add_guest_listener("web_player_1")
    await session.add_guest_listener("web_player_2")
    mass.players.unregister_audio_only_player.reset_mock()

    await session.close()

    removed = mass.players.cmd_set_members.await_args.kwargs["player_ids_to_remove"]
    assert set(removed) == {"web_player_1", "web_player_2"}
    assert {call.args for call in mass.players.unregister_audio_only_player.call_args_list} == {
        ("web_player_1",),
        ("web_player_2",),
    }


async def test_venue_add_guest_listener_unsupported() -> None:
    """Attaching an incompatible web player raises."""
    venue_player = _create_venue_player(can_group_with=set())
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    with pytest.raises(UnsupportedFeaturedException):
        await session.add_guest_listener("web_player_1")
    mass.players.cmd_set_members.assert_not_awaited()


async def test_venue_close_detaches_only_tracked_listeners() -> None:
    """Closing a venue session only detaches the guest listeners it added."""
    venue_player = _create_venue_player(can_group_with={"web_player_1"})
    mass = _create_mock_mass(venue_player)
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")
    await session.add_guest_listener("web_player_1")

    await session.close()

    mass.players.cmd_set_members.assert_awaited_with(
        "venue_player", player_ids_to_remove=["web_player_1"]
    )


async def test_venue_close_without_listeners() -> None:
    """Closing a venue session without listeners never touches the venue player."""
    mass = _create_mock_mass(_create_venue_player())
    session = await SharedPlaybackSession.create_venue(mass, "venue_player")

    await session.close()

    mass.players.cmd_set_members.assert_not_awaited()


# ==================== REMOTE mode ====================


async def test_create_remote_session(mass: MusicAssistant) -> None:
    """A remote session creates a hidden virtual player that owns a queue."""
    sendspin = cast("SendspinProvider | None", mass.get_provider("sendspin"))
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass, owner_instance_id=sendspin.instance_id, display_name="Test Party"
    )

    assert session.mode == SharedPlaybackMode.REMOTE
    player = mass.players.get_player(session.player_id)
    assert player is not None
    assert player.hidden_by_default is True
    assert mass.player_queues.get(session.queue_id) is not None
    # listen-in is only possible for real, groupable sendspin players
    guest_player_id = await sendspin.create_virtual_player(
        owner_instance_id=sendspin.instance_id, display_name="Guest"
    )
    # refresh the host player's calculated state (normally debounced)
    player.update_state(signal_event=False)
    assert session.can_listen_in(guest_player_id) is True
    assert session.can_listen_in("nonexistent_player") is False
    await sendspin.remove_virtual_player(guest_player_id)

    await session.close()
    assert mass.players.get_player(session.player_id) is None
    # with the virtual host player gone, listen-in is no longer possible
    assert session.can_listen_in(guest_player_id) is False


async def test_create_remote_session_deterministic_id(mass: MusicAssistant) -> None:
    """Re-creating a remote session with the same session_id yields the same player."""
    sendspin = mass.get_provider("sendspin")
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass,
        owner_instance_id=sendspin.instance_id,
        display_name="Test Party",
        session_id="my_party",
    )
    player_id = session.player_id
    await session.close()

    session = await SharedPlaybackSession.create_remote(
        mass,
        owner_instance_id=sendspin.instance_id,
        display_name="Test Party",
        session_id="my_party",
    )
    assert session.player_id == player_id
    await session.close()


async def test_create_remote_session_no_sendspin() -> None:
    """Creating a remote session without the Sendspin provider raises."""
    mass = MagicMock()
    mass.get_provider.return_value = None

    with pytest.raises(SetupFailedError):
        await SharedPlaybackSession.create_remote(
            mass, owner_instance_id="some_plugin", display_name="Test Party"
        )


async def test_remote_close_is_idempotent(mass: MusicAssistant) -> None:
    """Closing a remote session twice does not raise."""
    sendspin = mass.get_provider("sendspin")
    assert sendspin is not None

    session = await SharedPlaybackSession.create_remote(
        mass, owner_instance_id=sendspin.instance_id, display_name="Test Party"
    )
    await session.close()
    await session.close()
