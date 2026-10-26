"""Tests for PlayerController.get_playback_locks, the lock set a queue transfer holds."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import ResourceBusyError

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import controller as players_controller
from music_assistant.controllers.players.constants import PlayerLockPurpose
from tests.common import MockPlayer, MockProvider

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, key, default=None: {"min_volume": 0, "max_volume": 100}.get(
            key, default
        )
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.get_providers = MagicMock(return_value=[])
    return mass


def _controller_with_synced_pair(mock_mass: MagicMock) -> PlayerController:
    """
    Return a controller with leader "c", its synced member "a", solo player "b" and idle group "z".

    The ids are chosen so that the member sorts before the solo player, which sorts before
    the leader, which sorts before the group: an order by player id alone would take the
    member before its leader and the group last.
    """
    controller = PlayerController(mock_mass)
    provider = MockProvider("test", instance_id="test", mass=mock_mass)
    leader = MockPlayer(provider, "c", "Leader")
    leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
    leader._attr_group_members = ["c", "a"]
    controller._players = {
        "a": MockPlayer(provider, "a", "Member"),
        "b": MockPlayer(provider, "b", "Solo"),
        "c": leader,
    }
    group = MockPlayer(provider, "z", "Group", player_type=PlayerType.GROUP)
    group._attr_supported_features = {PlayerFeature.SET_MEMBERS}
    controller._players["z"] = group
    mock_mass.players = controller
    for player in controller._players.values():
        player.set_initialized()
    # a member reads its leader from the other initialized players, so derive the states
    # only once every player is in place
    for player in controller._players.values():
        player._cache.clear()
        player.update_state(signal_event=False)
    assert controller._players["a"].state.synced_to == "c"
    return controller


def _spy_on_lock_order(controller: PlayerController) -> list[str]:
    """Replace the controller's get_player_lock with a recording wrapper."""
    lock_keys: list[str] = []
    acquire_lock = controller.get_player_lock

    def _record(
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
        strict: bool = False,
    ) -> AbstractAsyncContextManager[None]:
        lock_keys.append(f"{purpose.value}_{player_id}")
        return acquire_lock(player_id, purpose, strict=strict)

    controller.get_player_lock = _record  # type: ignore[assignment]
    return lock_keys


@pytest.mark.parametrize("player_ids", [("a", "b"), ("b", "a")])
async def test_playback_locks_are_taken_in_the_same_order_whatever_the_argument_order(
    mock_mass: MagicMock, player_ids: tuple[str, str]
) -> None:
    """
    Two transfers over the same players take their locks in one order, so neither waits on the other.

    The order follows the group holding each player: the solo player sorts before the
    leader, and the member comes right after its leader, never before it.

    :param player_ids: The players to lock, in the order the command names them.
    """
    controller = _controller_with_synced_pair(mock_mass)
    lock_keys = _spy_on_lock_order(controller)

    async with controller.get_playback_locks(*player_ids):
        pass

    assert lock_keys == ["playback_b", "playback_c", "playback_a"]


async def test_playback_locks_hold_every_player_until_the_end(mock_mass: MagicMock) -> None:
    """Each lock stays held for the whole body and is free again afterwards."""
    controller = _controller_with_synced_pair(mock_mass)

    async with controller.get_playback_locks("a", "b"):
        for player_id in ("a", "b", "c"):
            assert controller._player_command_locks[f"playback_{player_id}"].locked()

    for player_id in ("a", "b", "c"):
        assert not controller._player_command_locks[f"playback_{player_id}"].locked()


@pytest.mark.parametrize("busy", ["a", "c"])
async def test_strict_playback_locks_fail_on_a_player_that_stays_busy(
    mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch, busy: str
) -> None:
    """
    A strict acquisition never runs beside the holder of one of the locks; it fails instead.

    :param busy: The player whose lock another task holds: the member, locked last, or the
        leader, locked in the middle.
    """
    monkeypatch.setattr(players_controller, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
    monkeypatch.setattr(players_controller, "PLAYER_LOCK_TIMEOUT", 0.02)
    monkeypatch.setattr(players_controller, "PLAYER_LOCK_STRICT_TIMEOUT", 0.05)
    controller = _controller_with_synced_pair(mock_mass)
    release = asyncio.Event()

    async def _hold() -> None:
        async with controller.get_player_lock(busy, PlayerLockPurpose.PLAYBACK):
            await release.wait()

    # the order is solo player, leader, member
    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0)
    try:
        with pytest.raises(ResourceBusyError):
            async with controller.get_playback_locks("a", "b", strict=True):
                pytest.fail("the body ran while another task held a lock")
    finally:
        release.set()
        await holder
    # the locks taken before the busy one were handed back, and none was taken after it
    for player_id in ("a", "b", "c"):
        lock = controller._player_command_locks.get(f"playback_{player_id}")
        assert lock is None or not lock.locked()


async def test_playback_locks_take_a_group_before_the_players_it_may_capture(
    mock_mass: MagicMock,
) -> None:
    """
    An idle group player is locked before any other player, whatever its id.

    A group starts its members from under its own lock, so a command holding a member
    while it waits for the group would lock the group out until the member's timeout.
    """
    controller = _controller_with_synced_pair(mock_mass)
    lock_keys = _spy_on_lock_order(controller)

    async with controller.get_playback_locks("b", "z"):
        pass

    assert lock_keys == ["playback_z", "playback_b"]


async def test_playback_locks_hand_back_a_players_locks_early(mock_mass: MagicMock) -> None:
    """Releasing one player frees its own lock and its leader's, and keeps the others' held."""
    controller = _controller_with_synced_pair(mock_mass)

    async with controller.get_playback_locks("a", "b") as release:
        await release("a")
        assert not controller._player_command_locks["playback_a"].locked()
        assert not controller._player_command_locks["playback_c"].locked()
        assert controller._player_command_locks["playback_b"].locked()

    for player_id in ("a", "b", "c"):
        assert not controller._player_command_locks[f"playback_{player_id}"].locked()


async def test_playback_locks_keep_a_lock_another_given_player_needs(
    mock_mass: MagicMock,
) -> None:
    """A player's leader is also one of the players given: releasing the member keeps the leader."""
    controller = _controller_with_synced_pair(mock_mass)

    async with controller.get_playback_locks("a", "c") as release:
        await release("a")
        assert not controller._player_command_locks["playback_a"].locked()
        assert controller._player_command_locks["playback_c"].locked()

    assert not controller._player_command_locks["playback_c"].locked()


async def test_playback_locks_keep_the_lock_of_a_player_given_twice(mock_mass: MagicMock) -> None:
    """The same player given as two of the players stays locked until both are done with."""
    controller = _controller_with_synced_pair(mock_mass)

    async with controller.get_playback_locks("b", "b") as release:
        await release("b")
        assert controller._player_command_locks["playback_b"].locked()
        await release("b")
        assert not controller._player_command_locks["playback_b"].locked()
