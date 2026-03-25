"""E2E tests for player registration, state management, and control commands."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature

from tests.support.harness import MusicAssistantHarness
from tests.support.mock_player_provider import MockPlayerProvider, TrackingMockPlayer


def _make_player(_harness: MusicAssistantHarness, player_id: str, name: str) -> TrackingMockPlayer:
    """Create a TrackingMockPlayer.

    MockPlayerProvider must use a MagicMock for mass so that Player.__init__
    can set mock config attributes during construction; the real mass is used
    only when registering the player via harness.add_player().
    """
    provider = MockPlayerProvider(domain="mock_ctrl_player", mass=MagicMock())
    return TrackingMockPlayer(provider=provider, player_id=player_id, name=name)


@pytest.mark.asyncio
async def test_player_is_registered_and_retrievable(harness: MusicAssistantHarness) -> None:
    """Given a TrackingMockPlayer, when registered, it is retrievable from the players."""
    # Given a mock player
    player = _make_player(harness, "ctrl-player-1", "Control Player 1")

    # When the player is registered via the harness
    await harness.add_player(player)

    # Then the player can be retrieved from the players controller
    retrieved = harness.mass.players.get_player("ctrl-player-1")
    assert retrieved is not None
    assert retrieved.player_id == "ctrl-player-1"


@pytest.mark.asyncio
async def test_player_queue_is_created_after_registration(harness: MusicAssistantHarness) -> None:
    """Given a registered player, a matching player queue is automatically created."""
    # Given a registered player
    player = _make_player(harness, "ctrl-player-2", "Control Player 2")
    await harness.add_player(player)

    # When looking up the player queue
    queue = harness.mass.player_queues.get("ctrl-player-2")

    # Then a queue exists for this player
    assert queue is not None
    assert queue.queue_id == "ctrl-player-2"


@pytest.mark.asyncio
async def test_multiple_players_registered_independently(harness: MusicAssistantHarness) -> None:
    """Given two players with different IDs, when registered, each is independently retrievable."""
    # Given two players
    player_a = _make_player(harness, "ctrl-player-3a", "Control Player 3A")
    player_b = _make_player(harness, "ctrl-player-3b", "Control Player 3B")

    # When both are registered
    await harness.add_player(player_a)
    await harness.add_player(player_b)

    # Then each is retrievable independently
    retrieved_a = harness.mass.players.get_player("ctrl-player-3a")
    retrieved_b = harness.mass.players.get_player("ctrl-player-3b")
    assert retrieved_a is not None
    assert retrieved_b is not None
    assert retrieved_a.player_id != retrieved_b.player_id


@pytest.mark.asyncio
async def test_cmd_stop_succeeds_on_idle_player(harness: MusicAssistantHarness) -> None:
    """Given an idle player, when a stop command is issued, it completes without error."""
    # Given an idle registered player
    player = _make_player(harness, "ctrl-player-4", "Control Player 4")
    await harness.add_player(player)

    # When a stop command is issued on the idle player
    await harness.mass.players.cmd_stop("ctrl-player-4")

    # Then the player remains idle (stop on idle player is a no-op)
    assert player.playback_state == PlaybackState.IDLE


@pytest.mark.asyncio
async def test_player_state_name_reflects_registration(harness: MusicAssistantHarness) -> None:
    """Given a player registered with a display name, the state name matches the provided name."""
    # Given a player with a specific name
    player = _make_player(harness, "ctrl-player-5", "My Named Player")
    await harness.add_player(player)

    # When the player state is retrieved
    retrieved = harness.mass.players.get_player("ctrl-player-5")
    assert retrieved is not None

    # Then the display name matches what was registered
    assert "My Named Player" in retrieved.state.name


@pytest.mark.asyncio
async def test_player_grouping_tracked_via_set_members(harness: MusicAssistantHarness) -> None:
    """Given two grouping-capable players, when one joins the other, group members are updated."""
    # Given a provider with two players that support grouping
    provider = MockPlayerProvider(domain="group_ctrl_player", mass=MagicMock())
    leader = TrackingMockPlayer(provider=provider, player_id="group-leader-1", name="Group Leader")
    member = TrackingMockPlayer(provider=provider, player_id="group-member-1", name="Group Member")

    # And both players support grouping
    leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
    member._attr_supported_features = {PlayerFeature.SET_MEMBERS}
    leader._attr_can_group_with = {"group-member-1"}
    leader._cache.clear()
    member._cache.clear()

    await harness.add_player(leader)
    await harness.add_player(member)

    # When the member is added to the leader's group via set_members
    await leader.set_members(player_ids_to_add=["group-member-1"])

    # Then the leader's group_members list contains the member
    assert "group-member-1" in leader.group_members
