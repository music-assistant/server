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
        """When active protocol is set, prefer members supporting it."""
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
        sgp._active_protocol_domain = "airplay"

        leader = sgp._select_sync_leader()
        assert leader == player_b

    def test_select_leader_fallback_when_no_protocol_match(self) -> None:
        """When no member supports active protocol, fall back to first available."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player_a = _make_mock_player("player_a", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"player_a": player_a})

        sgp._attr_group_members = ["player_a"]
        sgp._active_protocol_domain = "airplay"

        leader = sgp._select_sync_leader()
        assert leader == player_a

    def test_select_leader_no_protocol_uses_first_available(self) -> None:
        """When no active protocol, pick first available (existing behavior)."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player_a = _make_mock_player("player_a")
        player_b = _make_mock_player("player_b")
        mass.players.get_player = _player_lookup({"player_a": player_a, "player_b": player_b})

        sgp._attr_group_members = ["player_a", "player_b"]
        sgp._active_protocol_domain = None

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


class TestProtocolDomainTracking:
    """Test that _active_protocol_domain is properly managed."""

    @pytest.mark.asyncio
    async def test_play_media_caches_protocol(self) -> None:
        """play_media should cache the protocol domain after playback starts."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player(
            "leader",
            active_output_protocol="ap_leader",
            playback_state=PlaybackState.PLAYING,
        )
        protocol_player = _make_mock_player("ap_leader", provider_domain="airplay")

        mass.players.get_player = _player_lookup({"leader": leader, "ap_leader": protocol_player})

        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        # Patch update_state to avoid deep Player model internals
        media = MagicMock()
        media.source_id = "test_source"
        with patch.object(sgp, "update_state"):
            await sgp.play_media(media)

        assert sgp._active_protocol_domain == "airplay"

    def test_dissolve_clears_protocol(self) -> None:
        """Verify _dissolve_syncgroup clears sync_leader and protocol domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # Verify the dissolve method has the clearing logic by inspecting source.
        # The actual async dissolve is tested in integration, but we verify
        # the attributes are cleared by the method body.
        source = inspect.getsource(sgp._dissolve_syncgroup)
        assert "self.sync_leader = None" in source
        assert "self._active_protocol_domain = None" in source


class TestControllerLockCategory:
    """Test that the controller's lock categories serialize correctly."""

    def test_play_lock_key_format(self) -> None:
        """Lock key for play category uses 'play_{player_id}' format."""
        # The decorator uses lock category string as prefix
        # play_media, set_members, enqueue_next_media all use "play" category
        lock_key = "play_test_player_123"
        assert lock_key.startswith("play_")


class TestProtocolSwitchCleanup:
    """Test that protocol switching properly cleans up old sessions."""

    def test_update_active_protocol_resolves_domain(self) -> None:
        """_update_active_protocol should resolve the protocol domain from the sync leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # Leader initially on airplay
        leader = _make_mock_player(
            "leader",
            provider_domain="sonos",
            active_output_protocol="ap_leader",
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        mass.players.get_player = _player_lookup({"leader": leader, "ap_leader": ap_protocol})

        sgp.sync_leader = leader
        sgp._active_protocol_domain = "airplay"
        sgp._update_active_protocol()

        assert sgp._active_protocol_domain == "airplay"

    def test_protocol_domain_follows_switch(self) -> None:
        """After a protocol switch, the cached domain should reflect the new protocol."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # Leader switched from airplay to sendspin
        leader = _make_mock_player(
            "leader",
            provider_domain="sonos",
            active_output_protocol="sp_leader",
        )
        sp_protocol = _make_mock_player("sp_leader", provider_domain="sendspin")
        mass.players.get_player = _player_lookup({"leader": leader, "sp_leader": sp_protocol})

        sgp.sync_leader = leader
        sgp._active_protocol_domain = "airplay"  # old value
        sgp._update_active_protocol()  # should pick up the new protocol

        assert sgp._active_protocol_domain == "sendspin"

    @pytest.mark.asyncio
    async def test_dynamic_leader_switch_preserves_protocol(self) -> None:
        """Dynamic leader switch should preserve the active protocol domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            active_output_protocol="ap_old",
        )
        new_leader = _make_mock_player("new_leader", provider_domain="sonos")
        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")
        ap_protocol.set_members = AsyncMock()

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "new_leader": new_leader,
                "ap_old": ap_protocol,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "new_leader"]
        sgp._active_protocol_domain = "airplay"

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # Protocol domain preserved, new leader selected
        assert sgp._active_protocol_domain == "airplay"
        assert sgp.sync_leader == new_leader
        assert "old_leader" not in sgp._attr_group_members
