"""
Tests for player grouping logic (independent of protocols).

This module tests the core grouping behavior including:
- can_group_with filtering logic
- Group member inclusion/exclusion
- Sync leader behavior
- Group state transitions
- Cache invalidation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import LinkedOutputProtocol
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_raw_player_config_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        """Return appropriate defaults for player config values."""
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_player_config_value)
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return PlayerController(mock_mass)


class SessionBoundMockPlayer(MockPlayer):
    """Mock player whose native members ride its own stream session (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members are attached to this player's own stream session."""
        return True


class TestCanGroupWithBasics:
    """Test basic can_group_with filtering logic."""

    def test_ungrouped_players_can_group(self, mock_mass: MagicMock) -> None:
        """Test that two ungrouped players can group with each other."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # Use explicit player IDs instead of provider instance ID for simpler test
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_b._attr_can_group_with = {"player_a"}

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Both players should be able to group with each other
        assert "player_b" in player_a.state.can_group_with
        assert "player_a" in player_b.state.can_group_with

    def test_unavailable_players_excluded(self, mock_mass: MagicMock) -> None:
        """Test that unavailable players are excluded from can_group_with."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_available = False  # Mark as unavailable

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Unavailable player should be excluded
        assert "player_b" not in player_a.state.can_group_with

    def test_playing_players_with_different_source_excluded(self, mock_mass: MagicMock) -> None:
        """
        Test that players playing different sources are NOT excluded (behavior changed).

        Note: Previously, players with different active sources were excluded from grouping,
        but this was removed as it was difficult to track reliably.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}
        player_a._attr_playback_state = PlaybackState.PLAYING
        player_a._attr_active_source = "player_a"

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_playback_state = PlaybackState.PLAYING
        player_b._attr_active_source = "player_b"  # Different source

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Player with different active source is now ALLOWED (behavior changed)
        assert "player_b" in player_a.state.can_group_with


class TestSyncedPlayers:
    """Test behavior with synced/grouped players."""

    def test_sync_leader_excludes_itself_from_members_can_group_with(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that sync leader doesn't appear in its members' can_group_with."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member"}
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Member")

        controller._players = {"leader": leader, "member": member}
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # Member is synced, so can_group_with should be empty
        assert member.state.can_group_with == set()

    def test_group_members_included_in_leader_can_group_with(self, mock_mass: MagicMock) -> None:
        """
        Test that group members appear in sync leader's can_group_with.

        This allows ungrouping members from the leader.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a", "member_b"}
        leader._attr_group_members = ["leader", "member_a", "member_b"]

        member_a = MockPlayer(provider, "member_a", "Member A")
        member_b = MockPlayer(provider, "member_b", "Member B")

        controller._players = {
            "leader": leader,
            "member_a": member_a,
            "member_b": member_b,
        }
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member_a.update_state(signal_event=False)
        member_b.update_state(signal_event=False)

        # Leader should be able to see its own members (for ungrouping)
        assert "member_a" in leader.state.can_group_with
        assert "member_b" in leader.state.can_group_with


class TestSyncLeaderBehavior:
    """Test sync leader specific behavior."""

    def test_sync_leader_excluded_from_can_group_with(self, mock_mass: MagicMock) -> None:
        """Test that players with group members (sync leaders) are excluded."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member", "other"}
        leader._attr_group_members = ["leader", "member"]
        leader._attr_playback_state = PlaybackState.PLAYING  # Make it playing so it gets excluded

        member = MockPlayer(provider, "member", "Member")

        other = MockPlayer(provider, "other", "Other")
        other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other._attr_can_group_with = {"leader", "member"}

        controller._players = {"leader": leader, "member": member, "other": other}
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)
        other.update_state(signal_event=False)

        # Leader should NOT appear in other's can_group_with (has group members)
        assert "leader" not in other.state.can_group_with


class TestCircularDependency:
    """Test that circular dependencies are avoided."""

    def test_no_circular_dependency_in_synced_to(self, mock_mass: MagicMock) -> None:
        """
        Test that synced_to calculation doesn't cause circular dependency.

        Regression test for: synced_to calling group_members causing infinite recursion.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Member")

        controller._players = {"leader": leader, "member": member}
        mock_mass.players = controller

        # Mark players as initialized so they are returned by all_players()
        leader.set_initialized()
        member.set_initialized()

        # Trigger synced_to calculation via update_state
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # This should not cause infinite recursion
        assert member.state.synced_to == "leader"
        assert leader.state.synced_to is None


class TestCacheInvalidation:
    """Test that caches are invalidated correctly."""

    def test_can_group_with_cache_cleared_on_update_state(self, mock_mass: MagicMock) -> None:
        """Test that can_group_with cache is cleared when update_state is called."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Update state after setting attributes and registering with controller
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Get can_group_with to populate cache
        initial = player_a.state.can_group_with
        assert "player_b" in initial

        # Modify underlying data
        player_a._attr_can_group_with = set()

        # Cache should still have old value
        assert player_a.state.can_group_with == initial

        # Clear cache via update_state
        player_a.update_state(signal_event=False)

        # Cache should be cleared, new value should be returned
        assert player_a.state.can_group_with == set()


class TestProviderInstanceIdExpansion:
    """Test expansion of provider instance IDs in can_group_with."""

    def test_provider_instance_id_expands_to_all_players(self, mock_mass: MagicMock) -> None:
        """Test that provider instance IDs expand to all available players from that provider."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"test"}  # Provider instance ID

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_c = MockPlayer(provider, "player_c", "Player C")

        controller._players = {
            "player_a": player_a,
            "player_b": player_b,
            "player_c": player_c,
        }
        mock_mass.players = controller
        # Set up get_provider to return the provider for instance ID
        mock_mass.get_provider = MagicMock(return_value=provider)

        # Mark players as initialized so they are returned by all_players()
        player_a.set_initialized()
        player_b.set_initialized()
        player_c.set_initialized()

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)
        player_c.update_state(signal_event=False)

        # Provider instance ID should expand to include all players from that provider
        can_group = player_a.state.can_group_with
        assert "player_b" in can_group
        assert "player_c" in can_group

    def test_provider_instance_id_excludes_unknown_players(self, mock_mass: MagicMock) -> None:
        """Test that players without an output type are not offered as grouping targets."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        mock_mass.get_provider = MagicMock(return_value=provider)

        leader = MockPlayer(
            provider,
            "leader",
            "Leader",
            player_type=PlayerType.PROTOCOL,
        )
        leader._attr_can_group_with = {"test"}
        public_player = MockPlayer(provider, "public", "Public Player")
        unknown_player = MockPlayer(
            provider,
            "unknown",
            "Unknown Player",
            player_type=PlayerType.UNKNOWN,
        )
        controller._players = {
            "leader": leader,
            "public": public_player,
            "unknown": unknown_player,
        }
        mock_mass.players = controller

        for player in controller._players.values():
            player.set_initialized()
        for player in controller._players.values():
            player.update_state(signal_event=False)

        assert "public" in leader.state.can_group_with
        assert "unknown" not in leader.state.can_group_with

        leader._attr_can_group_with = {"unknown"}
        leader.update_state(signal_event=False, force_update=True)

        assert leader.state.can_group_with == set()


class TestFinalActiveGroupNewModel:
    """
    The active_group derivation respects is_active_session and the powered signal.

    Verifies the post-refactor contract:

    - A group whose ``powered`` attribute is ``False`` (e.g. user explicitly
      pinned it off via Fake control) never captures its members.
    - A group whose ``powered`` is ``True`` (fake-pinned on) captures members
      even without a live session.
    - A group with ``powered=None`` (no power control assigned) only captures
      members while ``is_active_session`` is ``True`` — i.e. while it has a
      sync_leader, an active stream, or a pending idle-grace task.
    """

    def test_dormant_group_does_not_capture_members(self, mock_mass: MagicMock) -> None:
        """No power signal, no session → member's active_group is None."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        # explicitly "no opinion" on power (matches new default for groups)
        group._attr_powered = None
        # listed as a configured member, but no active session
        group._attr_group_members = ["member"]
        # is_active_session base default is False → group is dormant
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group is None

    def test_powered_true_group_captures_members(self, mock_mass: MagicMock) -> None:
        """Group with _attr_powered=True (fake pin) captures members regardless of session."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = True
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group == "g1"

    def test_powered_false_group_does_not_capture_members(self, mock_mass: MagicMock) -> None:
        """Group with _attr_powered=False (explicit off) does not capture, even with a session."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = False
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group is None

    def test_session_active_group_captures_members(self, mock_mass: MagicMock) -> None:
        """Group with powered=None but is_active_session=True captures members."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        # subclass MockPlayer to override is_active_session for this test
        class _SessionedGroup(MockPlayer):
            @property
            def is_active_session(self) -> bool:
                return True

        group = _SessionedGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None  # no opinion on power
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group == "g1"


class TestPlayerBaseIsActiveSession:
    """The Player base class defaults is_active_session to False; only groups override it."""

    def test_base_player_is_not_an_active_session(self, mock_mass: MagicMock) -> None:
        """A regular MockPlayer should never claim to hold a captured session."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "p1", "P1")
        assert player.is_active_session is False


def _make_ad_hoc_group(
    controller: PlayerController, mock_mass: MagicMock, airplay_available: bool
) -> None:
    """
    Register an ad-hoc group playing over AirPlay, with only member "b" on that domain.

    Member "a" is a plain native player, member "b" is a native player with a linked
    AirPlay protocol player whose availability is driven by ``airplay_available``.
    """
    sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
    airplay = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

    leader = MockPlayer(sonos, "leader", "Leader")
    leader_protocol = MockPlayer(
        airplay, "leader_airplay", "Leader AirPlay", player_type=PlayerType.PROTOCOL
    )
    leader.set_linked_output_protocols([_airplay_link(leader_protocol.player_id)])
    leader.set_active_output_protocol(leader_protocol.player_id)

    member_a = MockPlayer(sonos, "a", "Member A")
    member_b = MockPlayer(sonos, "b", "Member B")
    member_b_protocol = MockPlayer(
        airplay, "b_airplay", "B AirPlay", player_type=PlayerType.PROTOCOL
    )
    member_b_protocol._attr_available = airplay_available
    member_b.set_linked_output_protocols([_airplay_link(member_b_protocol.player_id)])

    for player in (leader, member_a, member_b):
        player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        player._cache.clear()

    controller._players = {
        p.player_id: p for p in (leader, leader_protocol, member_a, member_b, member_b_protocol)
    }
    mock_mass.players = controller


def _airplay_link(protocol_id: str) -> LinkedOutputProtocol:
    """Build an AirPlay link to the given protocol player."""
    return LinkedOutputProtocol(
        output_protocol_id=protocol_id,
        protocol_domain="airplay",
        priority=10,
    )


class TestAdHocLeadershipTransfer:
    """Unjoining an ad-hoc sync leader transfers leadership instead of dissolving."""

    def test_select_ad_hoc_leader_prefers_active_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """The new leader should be a member that supports the group's active protocol."""
        # member_a can't do airplay, member_b has an airplay protocol player that is up
        _make_ad_hoc_group(controller, mock_mass, airplay_available=True)

        leader = controller.get_player("leader")
        assert leader is not None
        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "b"

    def test_select_ad_hoc_leader_skips_offline_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member whose protocol player went offline must not inherit the session."""
        # member_b still claims an airplay link, but its protocol player is gone
        _make_ad_hoc_group(controller, mock_mass, airplay_available=False)

        leader = controller.get_player("leader")
        assert leader is not None

        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "a"

    def test_select_ad_hoc_leader_accepts_native_member_on_active_domain(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member that plays the active protocol natively is still a valid leader."""
        # a Chromecast speaker has no linked protocol player: it *is* the chromecast output
        chromecast = MockProvider("chromecast", instance_id="chromecast", mass=mock_mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        leader = MockPlayer(sonos, "leader", "Leader")
        leader_protocol = MockPlayer(
            chromecast, "leader_cast", "Leader Cast", player_type=PlayerType.PROTOCOL
        )
        leader.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id=leader_protocol.player_id,
                    protocol_domain="chromecast",
                    priority=30,
                )
            ]
        )
        leader.set_active_output_protocol(leader_protocol.player_id)

        member_a = MockPlayer(sonos, "a", "Member A")
        member_c = MockPlayer(chromecast, "c", "Member C")
        for player in (leader, member_a, member_c):
            player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
            player._cache.clear()

        controller._players = {
            p.player_id: p for p in (leader, leader_protocol, member_a, member_c)
        }
        mock_mass.players = controller

        assert controller._select_ad_hoc_leader(leader, ["a", "c"]) == "c"

    def test_select_ad_hoc_leader_falls_back_to_first(self, controller: PlayerController) -> None:
        """Without an active protocol to match, fall back to the first remaining member."""
        leader = MagicMock()
        leader.active_output_protocol = None
        controller._players = {"a": MagicMock(), "b": MagicMock()}

        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "a"

    async def test_handle_set_members_transfers_leader_when_playing(
        self, mock_mass: MagicMock
    ) -> None:
        """Removing the leader from itself while playing routes to a leadership transfer."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a", "member_b"}
        leader._attr_group_members = ["leader", "member_a", "member_b"]
        member_a = MockPlayer(provider, "member_a", "Member A")
        member_b = MockPlayer(provider, "member_b", "Member B")

        controller._players = {"leader": leader, "member_a": member_a, "member_b": member_b}
        mock_mass.players = controller
        leader.update_state(signal_event=False)

        playing_queue = MagicMock()
        playing_queue.state = PlaybackState.PLAYING
        controller.get_active_queue = MagicMock(return_value=playing_queue)  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once()
        called_leader, called_remaining = controller._transfer_ad_hoc_leadership.call_args.args
        assert called_leader is leader
        assert set(called_remaining) == {"member_a", "member_b"}

    async def test_handle_set_members_dissolves_leader_when_idle(
        self, mock_mass: MagicMock
    ) -> None:
        """Removing the leader from itself while idle dissolves the group and stops."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a"}
        leader._attr_group_members = ["leader", "member_a"]
        member_a = MockPlayer(provider, "member_a", "Member A")

        controller._players = {"leader": leader, "member_a": member_a}
        mock_mass.players = controller
        leader.update_state(signal_event=False)

        idle_queue = MagicMock()
        idle_queue.state = PlaybackState.IDLE
        controller.get_active_queue = MagicMock(return_value=idle_queue)  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]
        controller._handle_set_members_with_protocols = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_not_awaited()
        controller._handle_cmd_stop.assert_awaited_once_with("leader")


class TestSessionBoundLeaderJoinsAnotherGroup:
    """A leader whose members ride its own stream gives that group up when it joins another."""

    def _build(
        self, mock_mass: MagicMock, leader_class: type[MockPlayer]
    ) -> tuple[PlayerController, MockPlayer, MockPlayer, MockPlayer]:
        """Build an idle leader with one native member, plus the group it is about to join."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

        leader = leader_class(provider, "leader", "Living Room")
        leader._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        leader._attr_can_group_with = {"member", "target"}
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Kitchen")
        member._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        target = MockPlayer(provider, "target", "Study")
        target._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        target._attr_can_group_with = {"leader", "member"}

        controller._players = {p.player_id: p for p in (leader, member, target)}
        mock_mass.players = controller
        for player in controller._players.values():
            player.set_initialized()
            player.update_state(signal_event=False)
        assert member.synced_to == "leader"
        return controller, leader, member, target

    async def test_own_group_is_dissolved_before_it_joins(self, mock_mass: MagicMock) -> None:
        """
        The members are released before the leader becomes a member itself.

        A native group outlives its session, so a leader that stopped still lists its
        members. Joining another group leaves it unable to serve them, and they would be
        silent while the UI still shows them grouped.
        """
        controller, leader, member, target = self._build(mock_mass, SessionBoundMockPlayer)

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert "member" not in leader.group_members
        assert member.synced_to is None
        assert target.group_members == ["target", "leader"]

    async def test_a_leader_without_members_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that leads nothing has no group to give up."""
        controller, leader, _member, target = self._build(mock_mass, SessionBoundMockPlayer)
        leader._attr_group_members = []
        leader.update_state(signal_event=False)
        leader.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        leader.set_members.assert_not_awaited()
        assert target.group_members == ["target", "leader"]

    async def test_a_group_that_cannot_be_dissolved_keeps_the_player_out(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A leader that could not give its group up stays out instead of joining on top of it.

        Joining anyway leaves it leading a group it can no longer serve, and a player that
        is synced refuses every later member change, so the state cannot be cleaned up.
        """
        controller, leader, member, target = self._build(mock_mass, SessionBoundMockPlayer)
        leader.set_members = AsyncMock(side_effect=RuntimeError("speaker unreachable"))  # type: ignore[method-assign]

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert target.group_members == []
        assert member.synced_to == "leader"

    async def test_a_group_that_survives_the_join_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A provider whose members do not ride the leader's own stream keeps its group."""
        controller, leader, member, target = self._build(mock_mass, MockPlayer)

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert leader.group_members == ["leader", "member"]
        assert member.synced_to == "leader"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
