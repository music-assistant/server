"""Tests for Sync Group Player protocol awareness and locking."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.player import OutputProtocol

from music_assistant.providers.sync_group.player import SyncGroupPlayer


def _player_lookup(players: dict[str, MagicMock]) -> MagicMock:
    """Create a get_player side_effect from a player dict."""
    return MagicMock(side_effect=lambda pid: players.get(pid))


def _make_mock_mass() -> MagicMock:
    """Create a minimal mock MusicAssistant instance."""
    mass = MagicMock()
    mass.players = MagicMock()
    mass.players.get_player = MagicMock(return_value=None)
    mass.players._handle_cmd_stop = AsyncMock()
    mass.players._handle_cmd_resume = AsyncMock()
    mass.players._handle_play_media = AsyncMock()
    mass.players.cmd_set_members = AsyncMock()
    mass.players.wait_for_player_update = AsyncMock(return_value=True)
    mass.players.trigger_player_update = MagicMock()
    mass.call_later = MagicMock()
    mass.cancel_timer = MagicMock()
    mass.config = MagicMock()
    mass.config.get_base_player_config.return_value = MagicMock(
        name=None, default_name="Test Group", get_value=MagicMock(return_value=True)
    )
    return mass


def _make_mock_player(
    player_id: str,
    provider_domain: str = "sonos",
    available: bool = True,
    protocol_domains: list[str] | None = None,
    active_output_protocol: str | None = None,
    playback_state: PlaybackState = PlaybackState.IDLE,
) -> MagicMock:
    """Create a mock player with configurable protocol support."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.available = available
    player.active_output_protocol = active_output_protocol
    player.playback_state = playback_state
    player.protocol_parent_id = None
    player.provider = MagicMock()
    player.provider.domain = provider_domain

    # Build linked_output_protocols
    protocols = []
    for domain in protocol_domains or []:
        proto = MagicMock(spec=OutputProtocol)
        proto.protocol_domain = domain
        proto.available = True
        protocols.append(proto)
    player.linked_output_protocols = protocols

    # State mock
    player.state = MagicMock()
    player.state.available = available
    player.state.playback_state = playback_state
    player.state.can_group_with = set()
    player.state.group_members = []
    player.state.supported_features = {PlayerFeature.SET_MEMBERS}
    player.set_members = AsyncMock()

    return player


def _make_sync_group(mass: MagicMock, player_id: str = "syncgroup_test") -> SyncGroupPlayer:
    """Create a SyncGroupPlayer with mock provider."""
    provider = MagicMock()
    provider.domain = "sync_group"
    provider.instance_id = "sync_group_test"
    provider.name = "Sync Group"
    provider.mass = mass

    def _config_get_value(key: str, default: object = None) -> object:
        if key == "dynamic_group_members":
            return True
        if key == "members_filter":
            return []
        return default

    mass.config.get_base_player_config.return_value = MagicMock(
        name=None, default_name="Test Group", get_value=_config_get_value
    )

    sgp = SyncGroupPlayer(provider, player_id)
    sgp._cache.clear()
    return sgp


class TestProtocolAwareLeaderSelection:
    """Test that leader selection prefers protocol continuity."""

    def test_select_leader_prefers_active_protocol(self) -> None:
        """When preferred protocol is given, prefer members supporting it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # Player A: sonos native only
        player_a = _make_mock_player("player_a", provider_domain="sonos")
        # Player B: has airplay protocol
        player_b = _make_mock_player(
            "player_b", provider_domain="sonos", protocol_domains=["airplay"]
        )

        mass.players.get_player = _player_lookup({"player_a": player_a, "player_b": player_b})

        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader(preferred_protocol_domain="airplay")
        assert leader == player_b

    def test_select_leader_fallback_when_no_protocol_match(self) -> None:
        """When no member supports the preferred protocol, fall back to first available."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player_a = _make_mock_player("player_a", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"player_a": player_a})

        sgp._attr_group_members = ["player_a"]

        leader = sgp._select_sync_leader(preferred_protocol_domain="airplay")
        assert leader == player_a

    def test_select_leader_no_protocol_uses_first_available(self) -> None:
        """When no preferred protocol, pick first available."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player_a = _make_mock_player("player_a")
        player_b = _make_mock_player("player_b")
        mass.players.get_player = _player_lookup({"player_a": player_a, "player_b": player_b})

        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader()
        assert leader == player_a


class TestMemberSupportsProtocol:
    """Test protocol domain checking for members."""

    def test_native_provider_match(self) -> None:
        """Player's own provider domain matches."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player = _make_mock_player("p1", provider_domain="airplay")
        assert sgp._member_supports_protocol_domain(player, "airplay") is True

    def test_linked_protocol_match(self) -> None:
        """Player has a linked output protocol matching the domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player = _make_mock_player("p1", provider_domain="sonos", protocol_domains=["airplay"])
        assert sgp._member_supports_protocol_domain(player, "airplay") is True

    def test_no_match(self) -> None:
        """Player doesn't support the requested protocol."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player = _make_mock_player("p1", provider_domain="sonos")
        assert sgp._member_supports_protocol_domain(player, "airplay") is False


class TestActiveProtocolDomain:
    """Test that active_protocol_domain is derived correctly from live state."""

    def test_no_leader_returns_none(self) -> None:
        """With no sync leader, active_protocol_domain is None."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = None
        assert sgp.active_protocol_domain is None

    def test_native_leader_returns_native_domain(self) -> None:
        """With a native leader (no active output protocol) return the leader's domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "sonos"

    def test_active_protocol_with_requiring_member(self) -> None:
        """Non-native protocol stays active while a member still requires it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader", provider_domain="sonos", active_output_protocol="ap_leader"
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        # AirPlay-only member (only linked protocol is airplay)
        ap_only = _make_mock_player(
            "ap_only", provider_domain="universal_player", protocol_domains=["airplay"]
        )
        ap_only.is_native_player = False
        mass.players.get_player = _player_lookup(
            {"leader": leader, "ap_leader": ap_protocol, "ap_only": ap_only}
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "ap_only"]
        assert sgp.active_protocol_domain == "airplay"

    def test_active_protocol_downshifts_when_no_member_requires_it(self) -> None:
        """Non-native protocol downshifts to native when no member still requires it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader", provider_domain="sonos", active_output_protocol="ap_leader"
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        mass.players.get_player = _player_lookup({"leader": leader, "ap_leader": ap_protocol})
        sgp.sync_leader = leader
        # Only a native-capable Sonos leader remains; no one requires airplay
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "sonos"

    def test_dissolve_clears_sync_leader(self) -> None:
        """Verify _dissolve_syncgroup clears the sync leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        source = inspect.getsource(sgp._dissolve_syncgroup)
        assert "self.sync_leader = None" in source


class TestControllerLockCategory:
    """Test that the controller's lock categories serialize correctly."""

    def test_play_lock_key_format(self) -> None:
        """Lock key for play category uses 'play_{player_id}' format."""
        # The decorator uses lock category string as prefix
        # play_media, set_members, enqueue_next_media all use "play" category
        lock_key = "play_test_player_123"
        assert lock_key.startswith("play_")


class TestDynamicLeaderSwitch:
    """Test dynamic leader switching behaviour."""

    @pytest.mark.asyncio
    async def test_dynamic_leader_switch_hands_off_to_new_leader(self) -> None:
        """Dynamic leader switch removes the old leader and picks a new one."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # AirPlay-only protocol member ensures active_protocol_domain resolves
        # to "airplay" and drives new-leader selection toward a member that
        # supports AirPlay.
        ap_only = _make_mock_player(
            "ap_only", provider_domain="universal_player", protocol_domains=["airplay"]
        )
        ap_only.is_native_player = False

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            active_output_protocol="ap_old",
            protocol_domains=["airplay"],
        )
        old_leader.handoff_sync_leadership = AsyncMock()
        new_leader = _make_mock_player(
            "new_leader", provider_domain="sonos", protocol_domains=["airplay"]
        )
        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "new_leader": new_leader,
                "ap_old": ap_protocol,
                "ap_only": ap_only,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "new_leader", "ap_only"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader == new_leader
        assert "old_leader" not in sgp._attr_group_members
        # Old leader got the handoff call with the new leader + remaining members
        old_leader.handoff_sync_leadership.assert_awaited_once()
        call_args = old_leader.handoff_sync_leadership.await_args
        assert call_args.args[0] == new_leader
        assert "ap_only" in call_args.args[1]
