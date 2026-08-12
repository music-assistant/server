"""Tests for Sync Group Player protocol awareness and locking."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import CONF_GROUP_MEMBERS, CONF_PLAYERS, PROTOCOL_PRIORITY
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
    mass.players._handle_set_members = AsyncMock()
    # get_player_lock is an async context manager (used by syncgroup to lock the sync leader)
    lock_ctx = AsyncMock()
    lock_ctx.__aenter__.return_value = None
    lock_ctx.__aexit__.return_value = False
    mass.players.get_player_lock = MagicMock(return_value=lock_ctx)
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
    offline_protocol_domains: list[str] | None = None,
    active_output_protocol: str | None = None,
    playback_state: PlaybackState = PlaybackState.IDLE,
    player_type: PlayerType = PlayerType.PLAYER,
) -> MagicMock:
    """
    Create a mock player with configurable protocol support.

    :param provider_domain: Domain of the provider owning the player.
    :param protocol_domains: Domains of the linked output protocols to attach.
    :param offline_protocol_domains: Domains from ``protocol_domains`` whose protocol
        player is currently unreachable.
    :param player_type: Use ``PlayerType.PROTOCOL`` for a protocol endpoint
        without independent device control (e.g. a generic AirPlay speaker).
    """
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.available = available
    player.active_output_protocol = active_output_protocol
    player.playback_state = playback_state
    player.protocol_parent_id = None
    player.provider = MagicMock()
    player.provider.domain = provider_domain
    supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.SET_MEMBERS}
    player.supported_features = supported_features

    # mirrors Player.is_native_player: protocol endpoints and wrappers have no
    # native playback of their own
    is_native = (
        player_type != PlayerType.PROTOCOL
        and provider_domain != "universal_player"
        and PlayerFeature.PLAY_MEDIA in supported_features
    )
    player.is_native_player = is_native

    # Build linked_output_protocols. Their `available` flag is the one production
    # sets at link time and never refreshes, so it stays True even for a protocol
    # player that has since gone offline.
    offline = set(offline_protocol_domains or [])
    protocols = []
    for domain in protocol_domains or []:
        proto = MagicMock(spec=OutputProtocol)
        proto.protocol_domain = domain
        proto.available = True
        # default to a synthetic protocol id; tests that need a specific id can override
        proto.output_protocol_id = f"{player_id}_{domain}_proto"
        protocols.append(proto)
    player.linked_output_protocols = protocols

    # mirrors Player.output_protocols: own output first (native playback, or the
    # protocol this player is itself an endpoint of), then the linked protocols
    # with their availability resolved from the live protocol player
    outputs = []
    if is_native or (
        provider_domain in PROTOCOL_PRIORITY and PlayerFeature.SET_MEMBERS in supported_features
    ):
        own = MagicMock(spec=OutputProtocol)
        own.protocol_domain = provider_domain
        own.available = available
        own.output_protocol_id = "native" if is_native else player_id
        outputs.append(own)
    for proto in protocols:
        live = MagicMock(spec=OutputProtocol)
        live.protocol_domain = proto.protocol_domain
        live.available = proto.protocol_domain not in offline
        live.output_protocol_id = proto.output_protocol_id
        outputs.append(live)
    player.output_protocols = outputs

    # State mock
    # real lists, so a test that needs members has to say so instead of silently
    # getting an empty auto-attribute
    player.group_members = []
    player.live_session_members = []

    player.state = MagicMock()
    player.state.available = available
    player.state.playback_state = playback_state
    player.state.can_group_with = set()
    player.state.group_members = []
    player.state.supported_features = supported_features
    # default to "not synced" so the syncgroup form path doesn't enter the
    # stale-state wait loop. Tests that want to assert on the stale-state
    # behavior can override this explicitly.
    player.state.synced_to = None
    player.synced_to = None
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

    def test_select_leader_prefers_live_session_member(self) -> None:
        """A member the live session already feeds outranks a plain protocol match."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # both speak AirPlay, but only player_b takes part in the live session
        player_a = _make_mock_player(
            "player_a", provider_domain="sonos", protocol_domains=["airplay"]
        )
        player_b = _make_mock_player(
            "player_b", provider_domain="sonos", protocol_domains=["airplay"]
        )
        mass.players.get_player = _player_lookup({"player_a": player_a, "player_b": player_b})

        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader(
            preferred_protocol_domain="airplay", preferred_member_ids=["player_b"]
        )
        assert leader == player_b

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

    def test_protocol_endpoint_matches_own_domain(self) -> None:
        """A protocol endpoint without device control still plays on its own domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player = _make_mock_player("p1", provider_domain="airplay", player_type=PlayerType.PROTOCOL)
        assert sgp._member_supports_protocol_domain(player, "airplay") is True

    def test_wrapper_domain_is_not_a_protocol(self) -> None:
        """A wrapper player never offers its own domain as a playback path."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        player = _make_mock_player(
            "p1", provider_domain="universal_player", protocol_domains=["airplay"]
        )
        assert sgp._member_supports_protocol_domain(player, "universal_player") is False

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

    def test_bridged_airplay_member_keeps_the_protocol(self) -> None:
        """An AirPlay device holds the group on AirPlay despite its Sendspin bridge."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader", provider_domain="sonos", active_output_protocol="ap_leader"
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        # an Apple TV plays AirPlay natively and carries a Sendspin bridge on top,
        # so its playback paths are airplay + sendspin - neither reaches sonos
        apple_tv = _make_mock_player(
            "apple_tv", provider_domain="airplay", protocol_domains=["sendspin"]
        )
        mass.players.get_player = _player_lookup(
            {"leader": leader, "ap_leader": ap_protocol, "apple_tv": apple_tv}
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "apple_tv"]
        assert sgp.active_protocol_domain == "airplay"

    def test_offline_protocol_does_not_count_as_a_playback_path(self) -> None:
        """A member's unreachable protocol no longer offers the group a way to it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # an Apple TV leads natively on airplay, with the session on its Sendspin bridge
        leader = _make_mock_player(
            "leader",
            provider_domain="airplay",
            protocol_domains=["sendspin"],
            active_output_protocol="sp_leader",
        )
        sp_protocol = _make_mock_player("sp_leader", provider_domain="sendspin")
        # the speaker still answers over its Sendspin bridge, but its AirPlay endpoint
        # dropped off - so airplay is no longer a way to reach it and the group has to
        # stay on sendspin rather than downshift to the leader's native airplay
        member = _make_mock_player(
            "member",
            provider_domain="universal_player",
            protocol_domains=["airplay", "sendspin"],
            offline_protocol_domains=["airplay"],
        )
        mass.players.get_player = _player_lookup(
            {"leader": leader, "sp_leader": sp_protocol, "member": member}
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]
        assert sgp.active_protocol_domain == "sendspin"

    def test_member_from_another_provider_keeps_the_protocol(self) -> None:
        """A member of a different provider keeps the shared protocol active."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader",
            provider_domain="sonos",
            protocol_domains=["sendspin"],
            active_output_protocol="sp_leader",
        )
        sp_protocol = _make_mock_player("sp_leader", provider_domain="sendspin")
        # a Chromecast can only meet the Sonos leader on the shared sendspin bridge
        chromecast = _make_mock_player(
            "chromecast", provider_domain="chromecast", protocol_domains=["sendspin"]
        )
        mass.players.get_player = _player_lookup(
            {"leader": leader, "sp_leader": sp_protocol, "chromecast": chromecast}
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "chromecast"]
        assert sgp.active_protocol_domain == "sendspin"

    def test_universal_leader_never_downshifts_to_its_wrapper_domain(self) -> None:
        """A wrapped leader has no native path, so the live protocol stays active."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # universal players play exclusively through their linked protocols
        leader = _make_mock_player(
            "leader",
            provider_domain="universal_player",
            protocol_domains=["airplay", "sendspin"],
            active_output_protocol="ap_leader",
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        mass.players.get_player = _player_lookup({"leader": leader, "ap_leader": ap_protocol})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "airplay"

    def test_unreachable_members_do_not_block_the_downshift(self) -> None:
        """Members that are offline or have no playback path are ignored."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader", provider_domain="sonos", active_output_protocol="ap_leader"
        )
        ap_protocol = _make_mock_player("ap_leader", provider_domain="airplay")
        offline = _make_mock_player("offline", provider_domain="airplay", available=False)
        # a wrapper that lost every linked protocol has nothing left to play on
        no_paths = _make_mock_player("no_paths", provider_domain="universal_player")
        mass.players.get_player = _player_lookup(
            {
                "leader": leader,
                "ap_leader": ap_protocol,
                "offline": offline,
                "no_paths": no_paths,
            }
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "offline", "no_paths", "gone"]
        assert sgp.active_protocol_domain == "sonos"

    def test_dissolve_clears_sync_leader(self) -> None:
        """Verify _dissolve_syncgroup clears the sync leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        source = inspect.getsource(sgp._dissolve_syncgroup)
        assert "self.sync_leader = None" in source

    @pytest.mark.asyncio
    async def test_dissolve_schedules_protocol_clear_while_leader_still_playing(self) -> None:
        """
        Dissolve must schedule the delayed protocol clear while the leader plays.

        Real devices report new state with a delay after a stop, so a playback
        state check here would skip the clear and leave a stale active protocol
        on the leader.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player(
            "leader",
            provider_domain="wiim",
            active_output_protocol="ap_leader",
            playback_state=PlaybackState.PLAYING,
        )
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        # use getattr to defeat mypy's narrowing after the earlier assignment,
        # since it can't see that _dissolve_syncgroup mutates sync_leader.
        assert getattr(sgp, "sync_leader") is None  # noqa: B009
        mass.players.schedule_active_output_protocol_clear.assert_called_once_with(leader)

    @pytest.mark.asyncio
    async def test_dissolve_ungroups_from_the_member_holding_the_group(self) -> None:
        """
        Dissolve must ungroup from the member that actually holds the group members.

        A provider can promote a different member to protocol leader than the one we
        track; ungrouping from our leader would then be a no-op and leave the members
        grouped and streaming with no way back.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        # our tracked leader no longer holds any members
        living_room = _make_mock_player("living_room", provider_domain="sendspin")
        bathroom = _make_mock_player("bathroom", provider_domain="sendspin")
        kitchen = _make_mock_player("kitchen", provider_domain="sendspin")
        bathroom.state.group_members = ["bathroom", "living_room", "kitchen"]

        mass.players.get_player = _player_lookup(
            {"living_room": living_room, "bathroom": bathroom, "kitchen": kitchen}
        )

        sgp.sync_leader = living_room
        sgp._attr_group_members = ["living_room", "bathroom", "kitchen"]

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_awaited_once_with(
            bathroom, player_ids_to_remove=["living_room", "kitchen"]
        )
        # the promoted owner must be stopped too, our callers only stop the tracked leader
        mass.players._handle_cmd_stop.assert_awaited_once_with("bathroom")
        assert getattr(sgp, "sync_leader") is None  # noqa: B009

    @pytest.mark.asyncio
    async def test_dissolve_keeps_using_the_sync_leader_when_it_holds_the_group(self) -> None:
        """A leader that still holds its members must stay the dissolve target."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sendspin")
        member = _make_mock_player("member", provider_domain="sendspin")
        leader.state.group_members = ["leader", "member"]

        mass.players.get_player = _player_lookup({"leader": leader, "member": member})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_remove=["member"]
        )
        mass.players._handle_cmd_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dissolve_ignores_a_member_leading_an_unrelated_group(self) -> None:
        """A member grouped outside of MA must never be adopted as the dissolve target."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        living_room = _make_mock_player("living_room", provider_domain="sendspin")
        member = _make_mock_player("member", provider_domain="sendspin")
        # member leads a group that shares no player with us
        member.state.group_members = ["member", "outsider"]

        mass.players.get_player = _player_lookup({"living_room": living_room, "member": member})
        sgp.sync_leader = living_room
        sgp._attr_group_members = ["living_room", "member"]

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_not_awaited()
        mass.players._handle_cmd_stop.assert_not_awaited()


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
        ap_new.protocol_parent_id = "new_leader"
        ap_only_proto.protocol_parent_id = "ap_only"

        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")
        # the live session holds new_leader's protocol player, so it can take over
        ap_protocol.group_members = ["ap_old", "ap_new", "ap_only_proto"]
        ap_protocol.live_session_members = ["ap_old", "ap_new", "ap_only_proto"]

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
        # the old leader was streaming on its own, so its session holds nobody else
        ap_protocol = _make_mock_player("ap_old", provider_domain="airplay")

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

    @pytest.mark.asyncio
    async def test_dynamic_leader_switch_follows_live_session_order(self) -> None:
        """
        The new leader must be the member that inherits the live session.

        Providers promote their own first remaining member, so a leader picked from a
        drifted member order would leave the group tracking a different player than the
        one actually holding the session.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player("old_leader", provider_domain="sendspin")
        living_room = _make_mock_player("living_room", provider_domain="sendspin")
        bathroom = _make_mock_player("bathroom", provider_domain="sendspin")
        # the live session hands over to bathroom, our own order says living_room.
        # state.group_members is deliberately left unordered: only the raw attribute
        # carries the provider's member order.
        old_leader.group_members = ["old_leader", "bathroom", "living_room"]
        old_leader.live_session_members = ["old_leader", "bathroom", "living_room"]
        old_leader.state.group_members = ["old_leader", "living_room", "bathroom"]

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "living_room": living_room,
                "bathroom": bathroom,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "living_room", "bathroom"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader == bathroom
        assert sgp._attr_group_members == ["bathroom", "living_room"]
        old_leader.set_members.assert_any_await(player_ids_to_remove=["old_leader"])
        bathroom.set_members.assert_any_await(player_ids_to_add=["living_room"])

    @pytest.mark.asyncio
    async def test_handoff_stays_on_live_protocol_after_downshift(self) -> None:
        """
        A pending downshift must not redirect the handoff to the native players.

        Once no member requires the non-native protocol anymore,
        active_protocol_domain reports the native domain while the stream is
        still carried by the protocol players, so the handoff has to keep
        addressing the latter.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_old",
        )
        kitchen = _make_mock_player(
            "kitchen",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_kitchen",
        )
        kitchen.linked_output_protocols[0].output_protocol_id = "ap_kitchen"
        bathroom = _make_mock_player(
            "bathroom",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_bathroom",
        )
        bathroom.linked_output_protocols[0].output_protocol_id = "ap_bathroom"

        ap_old = _make_mock_player("ap_old", provider_domain="airplay")
        ap_kitchen = _make_mock_player("ap_kitchen", provider_domain="airplay")
        ap_bathroom = _make_mock_player("ap_bathroom", provider_domain="airplay")
        ap_kitchen.protocol_parent_id = "kitchen"
        ap_bathroom.protocol_parent_id = "bathroom"
        ap_old.group_members = ["ap_old", "ap_kitchen", "ap_bathroom"]
        ap_old.live_session_members = ["ap_old", "ap_kitchen", "ap_bathroom"]

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "kitchen": kitchen,
                "bathroom": bathroom,
                "ap_old": ap_old,
                "ap_kitchen": ap_kitchen,
                "ap_bathroom": ap_bathroom,
            }
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "kitchen", "bathroom"]
        # all remaining members can play natively, so the group is due to downshift
        assert sgp.active_protocol_domain == "sonos"

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader == kitchen
        ap_old.set_members.assert_any_await(player_ids_to_remove=["ap_old"])
        ap_kitchen.set_members.assert_any_await(player_ids_to_add=["ap_bathroom"])
        # a native grouping command would regroup speakers that stream over AirPlay
        kitchen.set_members.assert_not_awaited()
        bathroom.set_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_leader_selection_prefers_the_live_protocol_after_downshift(self) -> None:
        """The new leader comes from the live session, not from the downshifted domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_old",
        )
        # streams over AirPlay but can also play natively, so it does not hold
        # the group on AirPlay and the group is due to downshift
        kitchen = _make_mock_player(
            "kitchen",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_kitchen",
        )
        kitchen.linked_output_protocols[0].output_protocol_id = "ap_kitchen"
        # native-only member that never joined the live session
        spare = _make_mock_player("spare", provider_domain="sonos")

        ap_old = _make_mock_player("ap_old", provider_domain="airplay")
        ap_kitchen = _make_mock_player("ap_kitchen", provider_domain="airplay")
        ap_kitchen.protocol_parent_id = "kitchen"
        # the session reports who takes part but no order, so our tracked order stands
        ap_old.group_members = []
        ap_old.live_session_members = ["ap_old", "ap_kitchen"]

        mass.players.get_player = _player_lookup(
            {
                "old_leader": old_leader,
                "kitchen": kitchen,
                "spare": spare,
                "ap_old": ap_old,
                "ap_kitchen": ap_kitchen,
            }
        )

        sgp.sync_leader = old_leader
        # spare comes first, so selecting on the downshifted domain would pick it
        sgp._attr_group_members = ["old_leader", "spare", "kitchen"]
        assert sgp.active_protocol_domain == "sonos"

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # picking the native-only spare would have cost a dissolve + reform
        assert sgp.sync_leader == kitchen
        ap_old.set_members.assert_any_await(player_ids_to_remove=["ap_old"])

    @pytest.mark.asyncio
    async def test_reform_hint_keeps_the_protocol_a_member_depends_on(self) -> None:
        """The re-form hint names a protocol the remaining member can actually play on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player(
            "old_leader",
            provider_domain="sonos",
            protocol_domains=["airplay"],
            active_output_protocol="ap_old",
        )
        # reachable over AirPlay and its Sendspin bridge, but never over sonos
        apple_tv = _make_mock_player(
            "apple_tv", provider_domain="airplay", protocol_domains=["sendspin"]
        )
        ap_old = _make_mock_player("ap_old", provider_domain="airplay")
        ap_old.group_members = []
        # the remaining member never joined the session, so no seamless handoff
        ap_old.live_session_members = ["ap_old"]

        mass.players.get_player = _player_lookup(
            {"old_leader": old_leader, "apple_tv": apple_tv, "ap_old": ap_old}
        )
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "apple_tv"]

        with patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform:
            await sgp._dynamic_leader_switch("old_leader")

        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("preferred_protocol_domain") == "airplay"

    @pytest.mark.asyncio
    async def test_leader_selection_skips_a_member_the_session_dropped(self) -> None:
        """
        A member the session dropped must not cost the group its seamless handoff.

        AirPlay drops a member from its session on a write timeout without pruning it
        from the group, so it stays an available candidate while no longer having a
        stream to inherit.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player("ap_old", provider_domain="airplay")
        dropped = _make_mock_player("ap_dropped", provider_domain="airplay")
        still_playing = _make_mock_player("ap_live", provider_domain="airplay")
        # the session dropped ap_dropped, but the group bookkeeping still lists it
        # (and lists it first, so it would be picked on member order alone)
        old_leader.group_members = ["ap_old", "ap_dropped", "ap_live"]
        old_leader.live_session_members = ["ap_old", "ap_live"]

        mass.players.get_player = _player_lookup(
            {"ap_old": old_leader, "ap_dropped": dropped, "ap_live": still_playing}
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["ap_old", "ap_dropped", "ap_live"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("ap_old")

        assert sgp.sync_leader == still_playing
        # handed off at the protocol level, so playback was never stopped;
        # the dropped member rejoins as a member of the new leader
        old_leader.set_members.assert_any_await(player_ids_to_remove=["ap_old"])
        still_playing.set_members.assert_any_await(player_ids_to_add=["ap_dropped"])
        mass.players._handle_cmd_stop.assert_not_awaited()
        assert sgp._reform_task is None

    @pytest.mark.asyncio
    async def test_dissolves_when_no_remaining_member_is_in_the_session(self) -> None:
        """With every remaining member dropped from the session, the group must re-form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player("ap_old", provider_domain="airplay")
        dropped_a = _make_mock_player("ap_a", provider_domain="airplay")
        dropped_b = _make_mock_player("ap_b", provider_domain="airplay")
        old_leader.group_members = ["ap_old", "ap_a", "ap_b"]
        old_leader.live_session_members = ["ap_old"]

        mass.players.get_player = _player_lookup(
            {"ap_old": old_leader, "ap_a": dropped_a, "ap_b": dropped_b}
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["ap_old", "ap_a", "ap_b"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("ap_old")

        # no member has a stream to inherit: stop and re-form with the remaining two
        old_leader.set_members.assert_not_awaited()
        mass.players._handle_cmd_stop.assert_awaited_with("ap_old")
        assert sgp._attr_group_members == ["ap_a", "ap_b"]
        assert sgp._reform_task is not None
        assert sgp.sync_leader is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider_domain", "live_members"),
        [
            # a solo Sendspin leader still lists itself as its group's only client
            ("sendspin", ["old_leader"]),
            # Snapcast reports no members at all while it leads nobody
            ("snapcast", []),
        ],
    )
    async def test_dynamic_leader_switch_dissolves_when_member_never_joined_session(
        self, provider_domain: str, live_members: list[str]
    ) -> None:
        """A member that never joined the live session cannot inherit it, whatever the provider."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        old_leader = _make_mock_player("old_leader", provider_domain=provider_domain)
        old_leader.group_members = live_members
        old_leader.live_session_members = live_members
        # added to the group in the same call that removed the leader, so it was
        # never synced at the protocol level
        fresh_player = _make_mock_player("fresh_player", provider_domain=provider_domain)

        mass.players.get_player = _player_lookup(
            {"old_leader": old_leader, "fresh_player": fresh_player}
        )

        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "fresh_player"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # no protocol-level handoff was attempted; the group stopped and dissolved
        # with a re-form scheduled so playback resumes on the new member
        old_leader.set_members.assert_not_awaited()
        mass.players._handle_cmd_stop.assert_awaited_with("old_leader")
        assert sgp._attr_group_members == ["fresh_player"]
        assert sgp._reform_task is not None
        assert sgp.sync_leader is None

    def test_align_members_translates_protocol_ids_and_keeps_outsiders_last(self) -> None:
        """A protocol session player's order maps onto the parent members that we track."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        living_room = _make_mock_player("living_room")
        bathroom = _make_mock_player("bathroom")
        # tracked but not part of the live session
        offline = _make_mock_player("offline")
        ap_living_room = _make_mock_player("ap_living_room", provider_domain="airplay")
        ap_bathroom = _make_mock_player("ap_bathroom", provider_domain="airplay")
        ap_living_room.protocol_parent_id = "living_room"
        ap_bathroom.protocol_parent_id = "bathroom"

        session_player = _make_mock_player("ap_old", provider_domain="airplay")
        session_player.group_members = ["ap_old", "ap_bathroom", "ap_living_room"]

        mass.players.get_player = _player_lookup(
            {
                "living_room": living_room,
                "bathroom": bathroom,
                "offline": offline,
                "ap_living_room": ap_living_room,
                "ap_bathroom": ap_bathroom,
                "ap_old": session_player,
            }
        )
        sgp._attr_group_members = ["living_room", "bathroom", "offline"]

        sgp._align_members_with_session(session_player)

        assert sgp._attr_group_members == ["bathroom", "living_room", "offline"]

    def test_align_members_leaves_order_untouched_without_a_live_session(self) -> None:
        """Without a session (or with one holding none of our members) the order stands."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        stranger = _make_mock_player("stranger", provider_domain="airplay")
        stranger.group_members = ["stranger"]
        mass.players.get_player = _player_lookup({"stranger": stranger})
        sgp._attr_group_members = ["living_room", "bathroom"]

        sgp._align_members_with_session(None)
        assert sgp._attr_group_members == ["living_room", "bathroom"]

        sgp._align_members_with_session(stranger)
        assert sgp._attr_group_members == ["living_room", "bathroom"]


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
        for call in mass.players._handle_set_members.await_args_list:
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
        mass.players._handle_set_members.assert_awaited_once()
        kwargs = mass.players._handle_set_members.await_args.kwargs
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
        # but _handle_set_members on the leader is not called (no leader yet)
        mass.players._handle_set_members.assert_not_awaited()


class TestSetMembersRemovesExternalJoiner:
    """Regression: a member joined to the leader outside MA must be removable."""

    @pytest.mark.asyncio
    async def test_external_joiner_removal_forwards_to_leader(self) -> None:
        """
        Removing an external joiner must forward straight to the sync leader.

        A player grouped directly to the sync leader (e.g. via the Sonos app)
        appears in group_members via the leader's live state but is not in the
        group's tracked member list, so it must not be silently skipped.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        # the leader's live native group includes an external joiner the group
        # itself never registered
        leader.state.group_members = ["leader", "external_x"]
        external = _make_mock_player("external_x", provider_domain="sonos")

        mass.players.get_player = _player_lookup({"leader": leader, "external_x": external})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        await sgp.set_members(player_ids_to_remove=["external_x"])

        # the removal must be forwarded to the sync leader
        mass.players._handle_set_members.assert_awaited_once()
        call = mass.players._handle_set_members.await_args
        assert call.args[0] is leader
        assert call.kwargs.get("player_ids_to_remove") == ["external_x"]
        # the group's tracked member list is untouched (external was never in it)
        assert sgp._attr_group_members == ["leader"]

    @pytest.mark.asyncio
    async def test_tracked_member_removal_uses_managed_path(self) -> None:
        """A member the group actually manages is removed via the managed path."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.group_members = ["leader", "member_b"]
        member_b = _make_mock_player("member_b", provider_domain="sonos")

        mass.players.get_player = _player_lookup({"leader": leader, "member_b": member_b})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member_b"]

        await sgp.set_members(player_ids_to_remove=["member_b"])

        # tracked member is dropped from the internal list and forwarded
        assert "member_b" not in sgp._attr_group_members
        mass.players._handle_set_members.assert_awaited_once()
        call = mass.players._handle_set_members.await_args
        assert call.kwargs.get("player_ids_to_remove") == ["member_b"]


def _make_sync_group_with_filters(
    mass: MagicMock,
    allowed_members: list[str] | None = None,
) -> SyncGroupPlayer:
    """Create a SyncGroupPlayer where the allowed_members filter can be configured."""
    provider = MagicMock()
    provider.domain = "sync_group"
    provider.instance_id = "sync_group_test"
    provider.name = "Sync Group"
    provider.mass = mass

    def _config_get_value(key: str, default: object = None) -> object:
        if key == "dynamic_members":
            return True
        if key == "allowed_members":
            return allowed_members or []
        return default

    mass.config.get_base_player_config.return_value = MagicMock(
        name=None, default_name="Test Group", get_value=_config_get_value
    )

    sgp = SyncGroupPlayer(provider, "syncgroup_test")
    sgp._cache.clear()
    return sgp


class TestPlayerFilters:
    """Test the allowed_members filter semantics."""

    def test_no_filter_allows_anything(self) -> None:
        """Empty filter: any player is allowed."""
        mass = _make_mock_mass()
        sgp = _make_sync_group_with_filters(mass)
        assert sgp._is_member_allowed("p1") is True

    def test_allowed_members_restricts_to_listed(self) -> None:
        """allowed_members populated: only listed IDs pass."""
        mass = _make_mock_mass()
        sgp = _make_sync_group_with_filters(mass, allowed_members=["p1"])
        assert sgp._is_member_allowed("p1") is True
        assert sgp._is_member_allowed("p2") is False

    @pytest.mark.asyncio
    async def test_set_members_rejects_filtered_player(self) -> None:
        """set_members must skip a candidate that fails the filter."""
        mass = _make_mock_mass()
        sgp = _make_sync_group_with_filters(mass, allowed_members=["allowed"])

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.can_group_with = {"allowed", "blocked"}
        allowed = _make_mock_player("allowed", provider_domain="sonos")
        blocked = _make_mock_player("blocked", provider_domain="sonos")

        mass.players.get_player = _player_lookup(
            {"leader": leader, "allowed": allowed, "blocked": blocked}
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        await sgp.set_members(player_ids_to_add=["allowed", "blocked"])

        assert "allowed" in sgp._attr_group_members
        assert "blocked" not in sgp._attr_group_members

    def test_can_group_with_current_members_exempt_from_filter(self) -> None:
        """
        A current member not listed in allowed_members must still seed candidates.

        Regression: filter must constrain JOINERS only, not gate the seed of
        current members. Otherwise enabling a filter that doesn't list a current
        member makes the candidate set empty (no joinable players show up).
        """
        mass = _make_mock_mass()
        # current member is "leader", but the allow-list only lists "candidate".
        sgp = _make_sync_group_with_filters(mass, allowed_members=["candidate"])

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.can_group_with = {"candidate", "outsider"}

        candidate = _make_mock_player("candidate", provider_domain="sonos")
        outsider = _make_mock_player("outsider", provider_domain="sonos")

        mass.players.get_player = _player_lookup(
            {"leader": leader, "candidate": candidate, "outsider": outsider}
        )
        sgp._attr_group_members = ["leader"]

        result = sgp.can_group_with
        # The current member (leader) is exempt and stays in the set.
        assert "leader" in result
        # An allowed candidate passes the filter.
        assert "candidate" in result
        # A player not in the allow-list is rejected.
        assert "outsider" not in result


class TestPresetMembersInDynamicGroup:
    """In a dynamic group, configured members act as a preset, not as a lock."""

    def _make_dynamic_group_with_preset(
        self, mass: MagicMock, preset_members: list[str]
    ) -> SyncGroupPlayer:
        """Create a dynamic sync group whose CONF_GROUP_MEMBERS is the given preset."""
        provider = MagicMock()
        provider.domain = "sync_group"
        provider.instance_id = "sync_group_test"
        provider.name = "Sync Group"
        provider.mass = mass

        def _config_get_value(key: str, default: object = None) -> object:
            if key == "dynamic_members":
                return True
            if key == "group_members":
                return list(preset_members)
            return default

        mass.config.get_base_player_config.return_value = MagicMock(
            name=None, default_name="Test Group", get_value=_config_get_value
        )
        sgp = SyncGroupPlayer(provider, "syncgroup_test")
        sgp._cache.clear()
        return sgp

    @pytest.mark.asyncio
    async def test_preset_member_can_be_unjoined(self) -> None:
        """A preset member in a dynamic group can be unjoined for the session."""
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["leader", "preset_b"])
        # Re-trigger on_config_updated so the new config is applied.
        await sgp.on_config_updated()

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.can_group_with = {"preset_b"}
        preset_b = _make_mock_player("preset_b", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader, "preset_b": preset_b})
        sgp.sync_leader = leader

        await sgp.set_members(player_ids_to_remove=["preset_b"])

        assert "preset_b" not in sgp._attr_group_members

    @pytest.mark.asyncio
    async def test_static_group_members_empty_in_dynamic_mode(self) -> None:
        """In dynamic mode static_group_members is empty (frontend gating)."""
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["a", "b", "c"])
        await sgp.on_config_updated()
        assert sgp._attr_static_group_members == []
        # but the configured members are seeded into group_members
        assert set(sgp._attr_group_members) == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_power_on_re_applies_preset_members(self) -> None:
        """Powering on re-applies the configured preset, restoring any previously unjoined member."""
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["leader", "preset_b"])
        await sgp.on_config_updated()

        leader = _make_mock_player("leader", provider_domain="sonos")
        preset_b = _make_mock_player("preset_b", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader, "preset_b": preset_b})
        sgp._attr_group_members = ["leader"]  # preset_b was unjoined in a previous session

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert "preset_b" in sgp._attr_group_members

    @pytest.mark.asyncio
    async def test_form_syncgroup_does_not_re_add_unjoined_preset(self) -> None:
        """Mid-session unjoins must stick: _form_syncgroup must NOT re-add preset members."""
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["leader", "preset_b"])
        await sgp.on_config_updated()

        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.group_members = ["leader"]
        preset_b = _make_mock_player("preset_b", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader, "preset_b": preset_b})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]  # preset_b was unjoined during this session

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_translate_to_parent_ids", side_effect=lambda x: list(x)),
        ):
            await sgp._form_syncgroup()

        assert "preset_b" not in sgp._attr_group_members

    def test_preset_member_bypasses_allow_list_filter(self) -> None:
        """
        A preset member that was unjoined must still pass the allow-list filter.

        Regression: after unjoining a preset member, the user must be able to
        re-add it via the OSD even when allowed_members doesn't list it.
        """
        mass = _make_mock_mass()
        provider = MagicMock()
        provider.domain = "sync_group"
        provider.instance_id = "sync_group_test"
        provider.name = "Sync Group"
        provider.mass = mass

        def _config_get_value(key: str, default: object = None) -> object:
            if key == "dynamic_members":
                return True
            if key == "group_members":
                return ["preset_a", "preset_b"]
            if key == "allowed_members":
                return ["other"]
            return default

        mass.config.get_base_player_config.return_value = MagicMock(
            name=None, default_name="Test Group", get_value=_config_get_value
        )
        sgp = SyncGroupPlayer(provider, "syncgroup_test")
        sgp._cache.clear()

        # preset members bypass the allow-list filter
        assert sgp._is_member_allowed("preset_a") is True
        assert sgp._is_member_allowed("preset_b") is True
        # the explicit allow-list entry still passes
        assert sgp._is_member_allowed("other") is True
        # an unrelated player is rejected
        assert sgp._is_member_allowed("outsider") is False

    @pytest.mark.asyncio
    async def test_can_group_with_falls_back_when_all_members_offline(self) -> None:
        """
        An offline preset member must not block adding other players.

        Regression: with only offline members in the list, can_group_with
        returned an empty set instead of offering compatible players, so
        nothing could be added to the group until the member came back.
        """
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["offline_member"])
        await sgp.on_config_updated()

        offline_member = _make_mock_player("offline_member", available=False)
        candidate = _make_mock_player("candidate")
        candidate.type = PlayerType.PLAYER
        candidate.state.can_group_with = {"other"}
        candidate.state.active_group = None
        mass.players.get_player = _player_lookup(
            {"offline_member": offline_member, "candidate": candidate}
        )
        mass.players.iter_players = MagicMock(return_value=[candidate])

        assert "candidate" in sgp.can_group_with

    @pytest.mark.asyncio
    async def test_can_group_with_aggregates_from_available_members(self) -> None:
        """With at least one available member, candidates come from the members only."""
        mass = _make_mock_mass()
        sgp = self._make_dynamic_group_with_preset(mass, ["offline_member", "online_member"])
        await sgp.on_config_updated()

        offline_member = _make_mock_player("offline_member", available=False)
        online_member = _make_mock_player("online_member")
        online_member.state.can_group_with = {"online_member", "friend"}
        mass.players.get_player = _player_lookup(
            {"offline_member": offline_member, "online_member": online_member}
        )

        result = sgp.can_group_with

        assert {"online_member", "friend"} <= result
        mass.players.iter_players.assert_not_called()


class TestGetConfigEntriesMemberPicker:
    """Test the member options offered in the group settings dropdown."""

    @pytest.mark.asyncio
    async def test_slaved_follower_is_still_offered(self) -> None:
        """
        A synced follower must stay selectable in the member dropdown.

        Regression: a member removed from the config during active playback
        remains slaved at the protocol level. Its can_group_with is then empty,
        and since it is no longer in the saved ids it vanished from the
        dropdown, making it impossible to re-add without stopping playback.
        """
        mass = _make_mock_mass()
        leader = _make_mock_player("leader")
        leader.type = PlayerType.PLAYER
        leader.state.can_group_with = {"follower"}
        # slaved follower: empty can_group_with while synced_to is set
        follower = _make_mock_player("follower")
        follower.type = PlayerType.PLAYER
        follower.state.synced_to = "leader"
        # idle player that cannot group with anything must NOT be offered
        solo = _make_mock_player("solo")
        solo.type = PlayerType.PLAYER
        mass.players.all_players = MagicMock(return_value=[leader, follower, solo])
        sgp = _make_sync_group(mass)

        entries = await sgp.get_config_entries()
        members_entry = next(x for x in entries if x.key == CONF_GROUP_MEMBERS)
        option_ids = {option.value for option in members_entry.options}

        assert option_ids == {"leader", "follower"}


class TestMembersFilterMigration:
    """Test the members_filter (exclusion) -> allowed_members (inclusion) migration."""

    def _run_migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run the migration block against a copy of the provided data dict."""
        all_player_configs = data.get(CONF_PLAYERS, {})
        if isinstance(all_player_configs, dict):
            group_provider_domains = {"sync_group", "universal_group"}
            universe = {
                pid
                for pid, cfg in all_player_configs.items()
                if isinstance(cfg, dict) and cfg.get("provider") not in group_provider_domains
            }
            for player_cfg in all_player_configs.values():
                if not isinstance(player_cfg, dict):
                    continue
                if player_cfg.get("provider") != "sync_group":
                    continue
                values = player_cfg.setdefault("values", {})
                old_exclude = values.get("members_filter") or []
                if not old_exclude or values.get("allowed_members") is not None:
                    continue
                values["allowed_members"] = sorted(universe - set(old_exclude))
                values["members_filter"] = []
        return data

    def test_inverts_exclusion_to_inclusion(self) -> None:
        """An exclusion list of [X] becomes an inclusion list of universe - {X}."""
        data = {
            CONF_PLAYERS: {
                "a": {"provider": "sonos", "player_id": "a"},
                "b": {"provider": "airplay", "player_id": "b"},
                "c": {"provider": "chromecast", "player_id": "c"},
                "syncgroup_1": {
                    "provider": "sync_group",
                    "player_id": "syncgroup_1",
                    "values": {"members_filter": ["b"]},
                },
            }
        }
        out = self._run_migrate(data)
        sg = out[CONF_PLAYERS]["syncgroup_1"]["values"]
        assert sg["allowed_members"] == ["a", "c"]
        assert sg["members_filter"] == []

    def test_idempotent_when_already_migrated(self) -> None:
        """Re-running migration on already-migrated config is a no-op."""
        data = {
            CONF_PLAYERS: {
                "a": {"provider": "sonos", "player_id": "a"},
                "syncgroup_1": {
                    "provider": "sync_group",
                    "player_id": "syncgroup_1",
                    "values": {"allowed_members": ["a"], "members_filter": []},
                },
            }
        }
        out = self._run_migrate(data)
        sg = out[CONF_PLAYERS]["syncgroup_1"]["values"]
        assert sg["allowed_members"] == ["a"]
        assert sg["members_filter"] == []

    def test_skips_when_no_exclusion_list(self) -> None:
        """A sync_group without a members_filter is left untouched."""
        data = {
            CONF_PLAYERS: {
                "syncgroup_1": {
                    "provider": "sync_group",
                    "player_id": "syncgroup_1",
                    "values": {},
                },
            }
        }
        out = self._run_migrate(data)
        sg = out[CONF_PLAYERS]["syncgroup_1"]["values"]
        assert "allowed_members" not in sg

    def test_excludes_group_providers_from_universe(self) -> None:
        """Group-providing players (sync_group/universal_group) are not in the universe."""
        data = {
            CONF_PLAYERS: {
                "a": {"provider": "sonos", "player_id": "a"},
                "ug": {"provider": "universal_group", "player_id": "ug"},
                "sg": {"provider": "sync_group", "player_id": "sg"},
                "syncgroup_1": {
                    "provider": "sync_group",
                    "player_id": "syncgroup_1",
                    "values": {"members_filter": ["a"]},
                },
            }
        }
        out = self._run_migrate(data)
        sg = out[CONF_PLAYERS]["syncgroup_1"]["values"]
        # Universe = {a} (sg/ug/syncgroup_1 are group providers excluded).
        # Inversion removes a, so allowed_members is empty.
        assert sg["allowed_members"] == []


class TestIsActiveSession:
    """The is_active_session property gates whether children report active_group."""

    def test_dormant_group_is_not_active(self) -> None:
        """A freshly-initialized group has no leader and no grace timer."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        assert sgp.is_active_session is False

    def test_group_with_sync_leader_is_active(self) -> None:
        """A group with a sync leader is considered active even without playback."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_mock_player("leader")
        assert sgp.is_active_session is True

    def test_group_in_grace_window_is_active(self) -> None:
        """While the idle-grace task is pending, the group still claims captured members."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # simulate a pending grace task without actually running asyncio
        sgp._idle_grace_task = MagicMock()
        assert sgp.is_active_session is True


class TestPowerlessLifecycle:
    """Form-on-play / deform-on-stop semantics when no power control is assigned."""

    @pytest.mark.asyncio
    async def test_play_media_forms_group_when_dormant(self) -> None:
        """play_media on a dormant group should select a leader and forward to it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp._attr_group_members = ["leader"]
        assert sgp.sync_leader is None

        with patch.object(sgp, "update_state"):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp.sync_leader == leader
        # mypy narrows sync_leader to None from the earlier assert; the
        # play_media call is what re-populates it, but mypy can't track that
        # mutation through the patch context — hence the ignore.
        mass.players._handle_play_media.assert_awaited_once()  # type: ignore[unreachable]

    @pytest.mark.asyncio
    async def test_stop_dissolves_group_when_powerless(self) -> None:
        """stop() on a group without power control should dissolve the session."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        # _attr_powered is None by default (no power control) so stop dissolves
        assert sgp._attr_powered is None

        with patch.object(sgp, "update_state"):
            await sgp.stop()

        # leader was stopped via the internal handler ...
        mass.players._handle_cmd_stop.assert_any_await("leader")
        # ... and the session was dissolved (sync_leader cleared)
        assert sgp.sync_leader is None

    @pytest.mark.asyncio
    async def test_stop_preserves_group_when_pinned_with_fake_power(self) -> None:
        """stop() should NOT dissolve when the user has pinned the group on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True  # user explicitly pinned via Fake control

        await sgp.stop()

        mass.players._handle_cmd_stop.assert_any_await("leader")
        # session stays intact
        assert sgp.sync_leader == leader


class TestIdleGraceTimer:
    """The idle-grace timer absorbs natural PLAYING→IDLE transitions."""

    def test_grace_scheduled_on_playing_to_idle(self) -> None:
        """_update_attributes schedules a grace task when the leader transitions to IDLE."""
        mass = _make_mock_mass()
        # create_task should return a sentinel we can inspect
        sentinel = MagicMock()
        mass.create_task = MagicMock(return_value=sentinel)
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", playback_state=PlaybackState.IDLE)
        sgp.sync_leader = leader
        # previous state was PLAYING
        sgp._attr_playback_state = PlaybackState.PLAYING
        sgp._attr_powered = None  # no pin → debounce applies

        sgp._update_attributes()

        assert sgp._idle_grace_task is sentinel
        mass.create_task.assert_called_once()

    def test_grace_not_scheduled_when_pinned(self) -> None:
        """No grace task when the user has pinned the group with Fake power."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", playback_state=PlaybackState.IDLE)
        sgp.sync_leader = leader
        sgp._attr_playback_state = PlaybackState.PLAYING
        sgp._attr_powered = True  # explicit pin

        sgp._update_attributes()

        assert sgp._idle_grace_task is None
        mass.create_task.assert_not_called()

    def test_grace_cancelled_on_resume(self) -> None:
        """A pending grace task is cancelled when the leader resumes playback."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", playback_state=PlaybackState.PLAYING)
        sgp.sync_leader = leader
        # pretend a grace task is pending
        prior_task = MagicMock()
        prior_task.done.return_value = False
        sgp._idle_grace_task = prior_task
        sgp._attr_playback_state = PlaybackState.IDLE

        sgp._update_attributes()

        prior_task.cancel.assert_called_once()
        assert sgp._idle_grace_task is None

    def test_grace_cancelled_on_explicit_stop(self) -> None:
        """An explicit stop cancels the pending grace task immediately."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        prior_task = MagicMock()
        prior_task.done.return_value = False
        sgp._idle_grace_task = prior_task

        async def _run() -> None:
            with patch.object(sgp, "update_state"):
                await sgp.stop()

        import asyncio  # noqa: PLC0415

        asyncio.run(_run())

        prior_task.cancel.assert_called_once()
        assert sgp._idle_grace_task is None


class TestFormWaitsForLeaderUnsynced:
    """The form path must wait for a stale leader to report synced_to=None."""

    @pytest.mark.asyncio
    async def test_form_waits_when_leader_still_synced(self) -> None:
        """If the new leader still reports synced_to, we wait before proceeding."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        # leader reports stale synced_to
        leader.state.synced_to = "old_leader"
        # but make _wait_member_unsynced succeed (clears state) so the form proceeds
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp._attr_group_members = ["leader"]

        async def fake_wait(
            _member_id: str,
            _timeout: float = 5.0,
        ) -> bool:
            leader.state.synced_to = None  # simulate provider catching up
            return True

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_wait_member_unsynced", side_effect=fake_wait) as wait_mock,
        ):
            await sgp._form_syncgroup()

        wait_mock.assert_awaited_once_with("leader")
        assert sgp.sync_leader == leader

    @pytest.mark.asyncio
    async def test_form_aborts_when_leader_stuck(self) -> None:
        """If the wait returns False (leader genuinely stuck), abort the form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.synced_to = "old_leader"
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp._attr_group_members = ["leader"]

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_wait_member_unsynced", return_value=False),
        ):
            await sgp._form_syncgroup()

        # form must NOT have left a sync_leader set, so the caller's play_media
        # won't be issued against a stuck player (the original Poolhouse race).
        assert sgp.sync_leader is None

    @pytest.mark.asyncio
    async def test_form_aborts_when_leader_cleared_during_wait(self) -> None:
        """If a concurrent dissolve clears sync_leader during the wait, abort the stale form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", provider_domain="sonos")
        leader.state.synced_to = "old_leader"
        member = _make_mock_player("m2", provider_domain="sonos")
        mass.players.get_player = _player_lookup({"leader": leader, "m2": member})
        sgp._attr_group_members = ["leader", "m2"]

        async def fake_wait(_member_id: str, _timeout: float = 5.0) -> bool:
            leader.state.synced_to = None
            sgp.sync_leader = None  # simulate a concurrent dissolve while waiting
            return True

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_wait_member_unsynced", side_effect=fake_wait),
        ):
            await sgp._form_syncgroup()

        # the stale form attempt must not (re)sync any members
        mass.players._handle_set_members.assert_not_awaited()
        assert sgp.sync_leader is None


class TestWaitMemberUnsynced:
    """The helper that waits for a member's synced_to to clear (with recovery)."""

    @pytest.mark.asyncio
    async def test_returns_true_when_already_unsynced(self) -> None:
        """Member already reports synced_to=None → return True without recovery."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_mock_player("m1")
        member.synced_to = None
        mass.players.get_player = _player_lookup({"m1": member})
        mass.players.cmd_ungroup = AsyncMock()

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        mass.players.cmd_ungroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attempts_recovery_when_stuck(self) -> None:
        """If the first wait doesn't clear it, kick the member from its stale parent directly."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_mock_player("m1")
        member.synced_to = "old_leader"  # still stale after first wait
        old_leader = _make_mock_player("old_leader")
        mass.players.get_player = _player_lookup({"m1": member, "old_leader": old_leader})

        # _handle_set_members on the stale parent is the expected recovery action.
        # We make it succeed by side-effect-clearing synced_to.
        async def _kick(_parent: Any, player_ids_to_remove: list[str]) -> None:
            assert player_ids_to_remove == ["m1"]
            member.synced_to = None

        mass.players._handle_set_members = AsyncMock(side_effect=_kick)
        mass.players.cmd_ungroup = AsyncMock()

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        mass.players._handle_set_members.assert_awaited_once_with(
            old_leader, player_ids_to_remove=["m1"]
        )
        mass.players.cmd_ungroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_genuinely_stuck(self) -> None:
        """If recovery doesn't help either, return False so callers can abort."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_mock_player("m1")
        member.synced_to = "old_leader"  # stays stuck even after recovery
        old_leader = _make_mock_player("old_leader")
        mass.players.get_player = _player_lookup({"m1": member, "old_leader": old_leader})
        mass.players._handle_set_members = AsyncMock()  # no-op: state stays stale

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is False
        mass.players._handle_set_members.assert_awaited_once_with(
            old_leader, player_ids_to_remove=["m1"]
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_stale_parent_gone(self) -> None:
        """If the stale parent no longer exists, skip the kick and report stuck."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_mock_player("m1")
        member.synced_to = "old_leader"  # parent is no longer registered
        mass.players.get_player = _player_lookup({"m1": member})

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is False
        mass.players._handle_set_members.assert_not_awaited()


class TestSupportedFeaturesPower:
    """POWER feature is only advertised when the user opts in via Fake control."""

    def test_power_not_in_base_features_by_default(self) -> None:
        """No power_control config → POWER feature not advertised."""
        mass = _make_mock_mass()
        # default config: no CONF_POWER_CONTROL
        mass.config.get_raw_player_config_value = MagicMock(return_value=None)
        sgp = _make_sync_group(mass)
        assert PlayerFeature.POWER not in sgp.supported_features

    def test_power_advertised_when_fake_control_assigned(self) -> None:
        """User assigns Fake power control → POWER feature shows up."""
        mass = _make_mock_mass()

        # CONF_POWER_CONTROL is the only key we care about; for everything
        # else return whatever the default config helper returns.
        def _get_raw(_player_id: str, key: str, default: object = None) -> object:
            if key == "power_control":
                return "fake"
            return default

        mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)
        sgp = _make_sync_group(mass)
        assert PlayerFeature.POWER in sgp.supported_features


def _recording_wait(order: list[str]) -> MagicMock:
    """
    Build a wait_for_player_update replacement that records call/enter/exit order.

    The returned mock records ``wait_for:<player_id>:<value>`` when invoked,
    ``subscribe`` on context enter and ``await`` on context exit, so a test can
    assert that the playback start was wrapped (subscribe before the command is
    issued) rather than awaited after the fact.
    """

    class _RecordingWait:
        async def __aenter__(self) -> None:
            order.append("subscribe")

        async def __aexit__(self, *_exc: object) -> bool:
            order.append("await")
            return False

    def _wait(player_id: str, **kwargs: Any) -> _RecordingWait:
        order.append(f"wait_for:{player_id}:{kwargs.get('attribute_value')}")
        return _RecordingWait()

    return MagicMock(side_effect=_wait)


class TestLeaderPlaybackAwaited:
    """A (re)form must not return until the leader confirms it has started playing."""

    @pytest.mark.asyncio
    async def test_play_waits_for_leader_before_returning(self) -> None:
        """play() wraps the resume in a wait so the group lock isn't released early."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", playback_state=PlaybackState.IDLE)
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        order: list[str] = []
        mass.players.wait_for_player_update = _recording_wait(order)
        mass.players.cmd_resume = AsyncMock(side_effect=lambda *_a, **_k: order.append("resume"))

        with patch.object(sgp, "_form_syncgroup", new=AsyncMock()):
            await sgp.play()

        # subscribe happens before the resume command, and the wait completes
        # after — i.e. the start is wrapped, not fire-and-forget.
        assert order == [
            f"wait_for:leader:{PlaybackState.PLAYING}",
            "subscribe",
            "resume",
            "await",
        ]

    @pytest.mark.asyncio
    async def test_play_media_waits_for_leader_before_returning(self) -> None:
        """play_media() wraps the leader start so a concurrent (un)group can't race it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader", playback_state=PlaybackState.IDLE)
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        order: list[str] = []
        mass.players.wait_for_player_update = _recording_wait(order)
        mass.players._handle_play_media = AsyncMock(
            side_effect=lambda *_a, **_k: order.append("play_media")
        )

        media = MagicMock()
        media.source_id = "syncgroup_test"
        with patch.object(sgp, "_form_syncgroup", new=AsyncMock()):
            await sgp.play_media(media)

        assert order == [
            f"wait_for:leader:{PlaybackState.PLAYING}",
            "subscribe",
            "play_media",
            "await",
        ]

    @pytest.mark.asyncio
    async def test_no_wait_when_group_has_no_leader(self) -> None:
        """With no leader to wait on, play() resumes without arming a playback wait."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = None

        mass.players.cmd_resume = AsyncMock()

        with patch.object(sgp, "_form_syncgroup", new=AsyncMock()):
            await sgp.play()

        mass.players.wait_for_player_update.assert_not_called()
        mass.players.cmd_resume.assert_awaited_once()


class TestPlaybackStartMarker:
    """A just-issued playback start counts as playing for group-command decisions."""

    def _setup_group(self, mass: MagicMock, group_state: PlaybackState) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        # a provider without dynamic leader switching, so leader removal takes the
        # dissolve+reform path
        leader = _make_mock_player("leader", provider_domain="wiim")
        other = _make_mock_player("other", provider_domain="wiim")
        mass.players.get_player = _player_lookup({"leader": leader, "other": other})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "other"]
        sgp._attr_playback_state = group_state
        # close scheduled background coroutines (idle-grace) instead of leaking them
        mass.create_task = MagicMock(side_effect=lambda coro, *_a, **_k: coro.close())
        return sgp

    @pytest.mark.asyncio
    async def test_leader_removal_during_startup_window_resumes(self) -> None:
        """
        Leader removal right after a start command must still resume.

        Devices report transient states (a false PLAYING, bounces through IDLE)
        while a group session starts, so the group state alone misreads the
        startup window as idle and would skip the post-reform resume.
        """
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.IDLE)
        sgp._playback_start_at = time.monotonic()

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()) as dyn_switch,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        dyn_switch.assert_not_awaited()
        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is True

    @pytest.mark.asyncio
    async def test_paused_group_never_resumes_despite_recent_start(self) -> None:
        """PAUSED is deliberate user intent and always wins over the startup marker."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.PAUSED)
        sgp._playback_start_at = time.monotonic()

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is False

    @pytest.mark.asyncio
    async def test_await_leader_playback_stamps_the_marker(self) -> None:
        """The playback wait stamps the start so decision logic can see the window."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_mock_player("leader")
        assert sgp._playback_recently_started is False

        async with sgp._await_leader_playback():
            pass

        assert sgp._playback_recently_started is True

    @pytest.mark.asyncio
    async def test_stop_voids_the_marker(self) -> None:
        """An explicit stop clears the marker so a later unjoin won't resume."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_mock_player("leader")
        mass.players.get_player = _player_lookup({"leader": leader})
        sgp.sync_leader = leader
        sgp._playback_start_at = time.monotonic()

        with patch.object(sgp, "update_state"):
            await sgp.stop()

        assert sgp._playback_recently_started is False


class TestDebouncedReform:
    """Leader removal re-forms debounced so cascaded unjoins coalesce."""

    def _setup_group(self, mass: MagicMock, members: list[str]) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        players = {
            member_id: _make_mock_player(member_id, provider_domain="wiim") for member_id in members
        }
        mass.players.get_player = _player_lookup(players)
        sgp.sync_leader = players[members[0]]
        sgp._attr_group_members = list(members)
        sgp._attr_playback_state = PlaybackState.PLAYING
        # run scheduled coroutines for real so the debounced re-form actually fires;
        # eager_start mirrors mass.create_task so the runner enters its sleep at
        # schedule time (a later cancel then always lands inside the body)
        mass.create_task = MagicMock(
            side_effect=lambda coro, *_a, **_k: asyncio.Task(
                coro, loop=asyncio.get_running_loop(), eager_start=True
            )
        )
        return sgp

    @pytest.mark.asyncio
    async def test_leader_removal_schedules_debounced_reform(self) -> None:
        """The stop/dissolve happens immediately; the re-form (with resume) is debounced."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

            # immediate half: leader stopped, group dissolved, re-form pending
            mass.players._handle_cmd_stop.assert_any_await("leader")
            assert sgp.sync_leader is None
            task = sgp._reform_task
            assert task is not None
            play.assert_not_awaited()
            # the group keeps claiming its members while the re-form is pending
            assert sgp.is_active_session is True

            await task

        play.assert_awaited_once()
        assert sgp._reform_task is None

    @pytest.mark.asyncio
    async def test_cascaded_unjoins_coalesce_into_single_reform(self) -> None:
        """A second unjoin within the window re-arms it: one re-form, final member list."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2", "m3"])

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            first_task = sgp._reform_task
            assert first_task is not None

            # second unjoin lands inside the debounce window
            await sgp.set_members(player_ids_to_remove=["m2"])
            second_task = sgp._reform_task
            assert second_task is not None
            assert second_task is not first_task

            await asyncio.gather(first_task, second_task)

        play.assert_awaited_once()
        assert sgp._attr_group_members == ["m3"]

    @pytest.mark.asyncio
    async def test_explicit_stop_cancels_pending_reform(self) -> None:
        """An explicit stop during the window means silence — no resume may follow."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None

            await sgp.stop()
            assert sgp._reform_task is None
            await task

        play.assert_not_awaited()
        assert sgp.is_active_session is False

    @pytest.mark.asyncio
    async def test_reform_aborts_when_all_members_left(self) -> None:
        """When the window drains the whole group, the re-form fires as a no-op."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            await sgp.set_members(player_ids_to_remove=["m2"])
            task = sgp._reform_task
            assert task is not None

            await task

        play.assert_not_awaited()
        assert sgp._reform_task is None
        assert sgp.is_active_session is False

    @pytest.mark.asyncio
    async def test_explicit_play_supersedes_pending_reform(self) -> None:
        """A user play during the window forms right away; the pending re-form is dropped."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        mass.players.cmd_resume = AsyncMock()

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None

            await sgp.play()
            assert sgp._reform_task is None
            await task

        mass.players.cmd_resume.assert_awaited_once()
        assert sgp.sync_leader is not None
        assert sgp.sync_leader.player_id == "m2"

    @pytest.mark.asyncio
    async def test_reform_survives_cancel_from_its_own_form(self) -> None:
        """
        The firing re-form must not cancel itself.

        The runner resumes via play() -> _form_syncgroup, which cancels any pending
        re-form timer — including, without the identity guard, the very task that
        is executing it.
        """
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        mass.players.cmd_resume = AsyncMock()

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None

            await task

        mass.players.cmd_resume.assert_awaited_once()
        assert sgp.sync_leader is not None
        assert sgp.sync_leader.player_id == "m2"
        assert sgp._reform_task is None


class TestReformResumeGate:
    """Removing the sync leader resumes playback if (and only if) the group was playing."""

    def _setup_group(self, mass: MagicMock, group_state: PlaybackState) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        # a provider without dynamic leader switching, so leader removal takes the
        # dissolve+reform path
        leader = _make_mock_player("leader", provider_domain="wiim")
        other = _make_mock_player("other", provider_domain="wiim")
        mass.players.get_player = _player_lookup({"leader": leader, "other": other})
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "other"]
        sgp._attr_playback_state = group_state
        # close scheduled background coroutines (idle-grace) instead of leaking them
        mass.create_task = MagicMock(side_effect=lambda coro, *_a, **_k: coro.close())
        return sgp

    @pytest.mark.asyncio
    async def test_leader_removal_resumes_when_playing(self) -> None:
        """Leader removal on a playing group must reform with a resume."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.PLAYING)

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()) as dyn_switch,
            patch.object(sgp, "_active_session_player", return_value=None),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        dyn_switch.assert_not_awaited()
        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is True

    @pytest.mark.asyncio
    async def test_leader_removal_does_not_resume_when_idle(self) -> None:
        """A genuinely idle group must not spuriously start playing on leader removal."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.IDLE)

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()),
            patch.object(sgp, "_active_session_player", return_value=None),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is False
