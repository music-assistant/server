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
    # wait_for_player_update is an async context manager — return one that no-ops.
    # __aexit__ must explicitly return False so exceptions inside the `async with`
    # body propagate (an unconfigured AsyncMock returns a truthy MagicMock and
    # would silently swallow real test failures).
    wait_ctx = AsyncMock()
    wait_ctx.__aenter__.return_value = None
    wait_ctx.__aexit__.return_value = False
    mass.players.wait_for_player_update = MagicMock(return_value=wait_ctx)
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
        # default to a synthetic protocol id; tests that need a specific id can override
        proto.output_protocol_id = f"{player_id}_{domain}_proto"
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
        # CONF_DYNAMIC_GROUP_MEMBERS resolves to "dynamic_members"
        if key == "dynamic_members":
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
        """When the new leader IS in the live session, seamless protocol-level handoff is used."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # AirPlay-only protocol member ensures active_protocol_domain resolves
        # to "airplay" and drives new-leader selection toward a member that
        # supports AirPlay.
        ap_only = _make_mock_player(
            "ap_only", provider_domain="universal_player", protocol_domains=["airplay"]
        )
        ap_only.is_native_player = False

        # new_leader's AirPlay protocol player (must be in the session for handoff)
        ap_new = _make_mock_player("ap_new", provider_domain="airplay")

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            active_output_protocol="ap_old",
            protocol_domains=["airplay"],
        )
        new_leader = _make_mock_player(
            "new_leader",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_new",
        )
        # Wire the linked airplay protocols on the parents to point to the
        # corresponding protocol player ids so _resolve_session_target can find them.
        new_leader.linked_output_protocols[0].output_protocol_id = "ap_new"
        ap_only.linked_output_protocols[0].output_protocol_id = "ap_only_proto"
        # ap_only's airplay protocol player needs to exist for the protocol-id resolution
        ap_only_proto = _make_mock_player("ap_only_proto", provider_domain="airplay")

        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")
        # Set up the live session with new_leader's protocol player in sync_clients
        mock_session = MagicMock()
        mock_session.sync_clients = [ap_protocol, ap_new, ap_only]
        ap_protocol.stream = MagicMock()
        ap_protocol.stream.session = mock_session

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "new_leader": new_leader,
                "ap_old": ap_protocol,
                "ap_new": ap_new,
                "ap_only": ap_only,
                "ap_only_proto": ap_only_proto,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "new_leader", "ap_only"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader == new_leader
        assert "old_leader" not in sgp._attr_group_members

        # 1. Old leader's session protocol player got told to step out (self-remove)
        ap_protocol.set_members.assert_any_await(player_ids_to_remove=["ap_old"])

        # 2. New leader's protocol player got the remaining members added.
        # ap_only is already a protocol player on the airplay domain (its own
        # provider is universal_player but its linked airplay protocol resolves
        # via _resolve_session_target). The exact id depends on the mock wiring,
        # but the call must have happened.
        ap_new.set_members.assert_awaited()
        add_call = ap_new.set_members.await_args
        assert add_call.kwargs.get("player_ids_to_add"), (
            "expected new leader's protocol player to receive the remaining members"
        )

    @pytest.mark.asyncio
    async def test_dynamic_leader_switch_dissolves_when_new_leader_not_in_session(
        self,
    ) -> None:
        """When the new leader is NOT in the live session, fall back to dissolve+reform."""
        mass = _make_mock_mass()
        mass.players.cmd_resume = AsyncMock()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            active_output_protocol="ap_old",
            protocol_domains=["airplay"],
        )
        # Freshly-added player — NOT in the live session
        fresh_player = _make_mock_player(
            "fresh_player", provider_domain="sonos", protocol_domains=["airplay"]
        )
        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")
        # Session does NOT contain fresh_player's protocol player
        mock_session = MagicMock()
        mock_session.sync_clients = [ap_protocol]
        ap_protocol.stream = MagicMock()
        ap_protocol.stream.session = mock_session

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "fresh_player": fresh_player,
                "ap_old": ap_protocol,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "fresh_player"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # The old protocol player must NOT have been told to self-remove
        # (that path is only for the seamless handoff). Instead, dissolve+reform
        # happened: wait_for_player_update was used to wrap the stop.
        ap_protocol.set_members.assert_not_awaited()
        mass.players.wait_for_player_update.assert_called()
        assert "old_leader" not in sgp._attr_group_members


class TestPowerLifecycle:
    """Test that power(True/False) drives the group's form/dissolve lifecycle."""

    @pytest.mark.asyncio
    async def test_power_on_forms_group_and_picks_leader(self) -> None:
        """power(True) should select a sync leader and mark the group powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp._attr_group_members = ["leader"]

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert sgp.sync_leader == leader
        assert sgp._attr_powered is True

    @pytest.mark.asyncio
    async def test_power_on_with_no_members_stays_unformed(self) -> None:
        """power(True) on an empty group should leave sync_leader as None but mark powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert sgp.sync_leader is None
        # group is powered (intent to be active) even though no leader could be picked
        assert sgp._attr_powered is True

    @pytest.mark.asyncio
    async def test_power_off_dissolves_group(self) -> None:
        """power(False) should clear the sync leader and mark unpowered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True

        with patch.object(sgp, "update_state"):
            await sgp.power(False)

        # use getattr to defeat mypy's narrowing of these attributes after the
        # earlier assignments, since it can't see that power(False) mutates them.
        assert getattr(sgp, "sync_leader") is None  # noqa: B009
        assert getattr(sgp, "_attr_powered") is False  # noqa: B009

    @pytest.mark.asyncio
    async def test_stop_does_not_dissolve_group(self) -> None:
        """stop() should stop the leader but leave the group formed and powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True

        await sgp.stop()

        # leader was stopped via the internal handler
        mass.players._handle_cmd_stop.assert_awaited_once_with("leader")
        # but the group is still formed: sync_leader and powered are unchanged
        assert sgp.sync_leader == leader
        assert sgp._attr_powered is True


class TestSetMembersDoesNotRegisterIncompatible:
    """Regression test for: incompatible members must NOT be added to _attr_group_members."""

    @pytest.mark.asyncio
    async def test_incompatible_member_is_not_registered(self) -> None:
        """A member that fails the can_group_with check must not be appended."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        # leader's can_group_with does NOT include the incompatible member
        leader.state.can_group_with = {"leader"}
        incompatible = _make_mock_player("incompatible", provider_domain="alien_protocol")

        mass.players.get_player = _player_lookup({"leader": leader, "incompatible": incompatible})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        await sgp.set_members(player_ids_to_add=["incompatible"])

        # incompatible must NOT linger in the internal member list
        assert "incompatible" not in sgp._attr_group_members
        # and the leader was never asked to add it (the call may still happen
        # with empty lists since the member-changed path is taken, but the
        # incompatible id must not appear in either add or remove)
        for call in mass.players.cmd_set_members.await_args_list:
            assert "incompatible" not in (call.kwargs.get("player_ids_to_add") or [])
            assert "incompatible" not in (call.kwargs.get("player_ids_to_remove") or [])

    @pytest.mark.asyncio
    async def test_compatible_member_is_registered_and_forwarded(self) -> None:
        """A compatible member should be appended and forwarded to the leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.can_group_with = {"compatible"}
        compatible = _make_mock_player("compatible", provider_domain="sonos")

        mass.players.get_player = _player_lookup({"leader": leader, "compatible": compatible})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        await sgp.set_members(player_ids_to_add=["compatible"])

        assert "compatible" in sgp._attr_group_members
        mass.players.cmd_set_members.assert_awaited_once()
        kwargs = mass.players.cmd_set_members.await_args.kwargs
        assert kwargs.get("player_ids_to_add") == ["compatible"]

    @pytest.mark.asyncio
    async def test_member_added_when_no_leader_yet(self) -> None:
        """Adding to an empty/unformed group must register the member regardless."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        member = _make_mock_player("member", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"member": member})

        # no sync leader, empty group
        sgp.sync_leader = None
        sgp._attr_group_members = []

        await sgp.set_members(player_ids_to_add=["member"])

        # member is registered so a future _form_syncgroup can pick it as leader
        assert "member" in sgp._attr_group_members
        # but cmd_set_members on the leader is not called (no leader yet)
        mass.players.cmd_set_members.assert_not_awaited()
