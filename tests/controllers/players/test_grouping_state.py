"""Tests for the grouping a player publishes, derived from what its protocols report."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.media_items import AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass

MAC_1 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"}
MAC_2 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"}
MAC_3 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"}


class _LockedProtocol(MockProtocol):
    """A protocol that keeps its group read-only (e.g. an externally-created mixed group)."""

    @property
    def grouping_locked(self) -> bool:
        """Suppress grouping in the exposed state."""
        return True


class _Server:
    """A players controller on a mocked server, with the providers it knows by instance id."""

    def __init__(self) -> None:
        """Create the server."""
        self.mass = mock_player_mass()
        self.mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
        self.providers: dict[str, MockProvider] = {}
        self.mass.get_provider = MagicMock(side_effect=self.providers.get)
        self.controller = PlayerController(self.mass)
        self.mass.players = self.controller

    def provider(self, domain: str, instance_id: str | None = None) -> MockProvider:
        """
        Return the provider of a domain, created on first use.

        :param domain: The domain of the provider.
        :param instance_id: Its instance id, the domain by default.
        """
        instance_id = instance_id or domain
        if instance_id not in self.providers:
            self.providers[instance_id] = MockProvider(
                domain, instance_id=instance_id, mass=self.mass
            )
        return self.providers[instance_id]

    def refresh(self, *players: Player) -> None:
        """Recalculate the state of the given players, all of them by default, in order."""
        for player in players or tuple(self.controller._players.values()):
            player.refresh_state(signal_event=False)


def _attach_audio(
    server: _Server,
    domain: str,
    protocol_id: str,
    player: Player,
    identifiers: dict[IdentifierType, str] | None = None,
    can_group_with: set[str] | None = None,
    set_members: bool = True,
) -> MockProtocol:
    """
    Attach an audio protocol to a player and return it.

    :param server: The server.
    :param domain: The domain of the protocol's provider.
    :param protocol_id: Id of the protocol.
    :param player: The player of its device.
    :param identifiers: The identifiers it reports.
    :param can_group_with: The protocols it can group with.
    :param set_members: Whether it can group members.
    """
    _, protocol = attach_mock(
        server.provider(domain),
        protocol_id,
        role=ProtocolRole.AUDIO,
        player=player,
        identifiers=identifiers,
    )
    if set_members:
        protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    protocol._attr_can_group_with = can_group_with or set()
    return protocol


class TestCanGroupWith:
    """Tests for can_group_with property with three scenarios."""

    @staticmethod
    def _living_room(server: _Server) -> tuple[Player, Player, MockProtocol]:
        """
        Return a Sonos player with an AirPlay protocol, another Sonos player, and that protocol.

        :param server: The server.
        """
        sonos = server.provider("sonos", "sonos_instance")
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_native._attr_can_group_with = {"sonos_456"}
        sonos_player_b, _ = attach_mock(sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        sonos_airplay = _attach_audio(
            server,
            "airplay",
            "airplay_sonos",
            sonos_player,
            identifiers=MAC_1,
            can_group_with={"airplay_other"},
        )
        return sonos_player, sonos_player_b, sonos_airplay

    def test_scenario_1_native_active_only_native_players(self) -> None:
        """Test Scenario 1: Native playback active -> all protocols shown (new behavior)."""
        server = _Server()
        sonos_player, sonos_player_b, _ = self._living_room(server)
        sonos_player.set_active_output_protocol("native")
        server.refresh()

        # Get can_group_with while native is active
        groupable = sonos_player.can_group_with

        # NEW BEHAVIOR: Should show both native AND protocol players
        # even when native protocol is active
        assert sonos_player_b.player_id in groupable  # Native Sonos player
        # Note: airplay_other is not attached, so it won't appear
        # But the logic should still allow showing AirPlay options if they were attached

    def test_scenario_2_protocol_active_hybrid_groups(self) -> None:
        """Test Scenario 2: Protocol active -> show all protocols (new behavior)."""
        server = _Server()
        sonos_player, sonos_player_b, _ = self._living_room(server)
        # Create another device with AirPlay
        wiim_player, _ = attach_mock(
            server.provider("sonos", "sonos_instance"), "wiim_789", "Bedroom", identifiers=MAC_3
        )
        _attach_audio(
            server,
            "airplay",
            "airplay_other",
            wiim_player,
            identifiers=MAC_3,
            can_group_with={"airplay_sonos"},
        )
        sonos_player.set_active_output_protocol("airplay_sonos")
        server.refresh()

        # Get can_group_with while AirPlay is active
        groupable = sonos_player.can_group_with

        # NEW BEHAVIOR: Should show ALL protocols + native players
        # regardless of which protocol is active
        assert sonos_player_b.player_id in groupable  # Native Sonos player
        assert wiim_player.player_id in groupable  # Via airplay_other protocol

    def test_scenario_3_no_active_output_all_protocols_shown(self) -> None:
        """Test Scenario 3: No active output -> show all compatible protocols + native."""
        server = _Server()
        sonos_player, sonos_player_b, _ = self._living_room(server)
        # DLNA does NOT support SET_MEMBERS
        _attach_audio(
            server,
            "dlna",
            "dlna_sonos",
            sonos_player,
            identifiers=MAC_1,
            can_group_with={"dlna_other"},
            set_members=False,
        )
        # Another device
        wiim_player, _ = attach_mock(
            server.provider("sonos", "sonos_instance"), "wiim_789", "Bedroom", identifiers=MAC_3
        )
        _attach_audio(
            server,
            "airplay",
            "airplay_other",
            wiim_player,
            identifiers=MAC_3,
            can_group_with={"airplay_sonos"},
        )
        # No active output protocol set
        server.refresh()

        # Get can_group_with with no active protocol
        groupable = sonos_player.can_group_with

        # Should show native players + AirPlay players (supports SET_MEMBERS)
        # but NOT DLNA players (no SET_MEMBERS support)
        assert sonos_player_b.player_id in groupable
        assert wiim_player.player_id in groupable  # Via AirPlay protocol
        # DLNA players should not be shown since DLNA doesn't support SET_MEMBERS


class TestNativePlayerProtocolGrouping:
    """Tests for grouping between a player whose own protocol is AirPlay and AirPlay protocols."""

    @staticmethod
    def _homepod_and_sonos(server: _Server) -> tuple[Player, Player, MockProtocol]:
        """
        Return a HomePod (own protocol AirPlay), a Sonos player with AirPlay, and the Sonos native.

        Both AirPlay protocols can group with every AirPlay protocol of their provider.

        :param server: The server.
        """
        airplay = server.provider("airplay")
        homepod, homepod_native = attach_mock(airplay, "homepod_1", "Office")
        homepod_native._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        homepod_native._attr_can_group_with = {"airplay"}  # Provider instance ID
        sonos_player, sonos_native = attach_mock(server.provider("sonos"), "sonos_1", "Kitchen")
        sonos_native._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        _attach_audio(
            server, "airplay", "airplay_sonos_1", sonos_player, can_group_with={"airplay"}
        )
        return homepod, sonos_player, sonos_native

    def test_native_airplay_player_sees_protocol_players_as_visible_parents(self) -> None:
        """Test that a native AirPlay player translates AirPlay protocols to their players."""
        server = _Server()
        homepod, sonos_player, _ = self._homepod_and_sonos(server)
        server.refresh(sonos_player, homepod)

        groupable = homepod.can_group_with

        # HomePod should see the Sonos PLAYER, not its AirPlay protocol
        assert sonos_player.player_id in groupable
        assert "airplay_sonos_1" not in groupable  # A protocol id must NOT appear

    def test_protocol_linked_player_sees_native_airplay_player(self) -> None:
        """Test that a player with an AirPlay protocol sees native AirPlay players."""
        server = _Server()
        homepod, sonos_player, sonos_native = self._homepod_and_sonos(server)
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_native._attr_can_group_with = set()  # No native Sonos grouping peers
        server.refresh(homepod, sonos_player)

        groupable = sonos_player.can_group_with

        # Sonos should see HomePod via its AirPlay protocol's can_group_with
        assert homepod.player_id in groupable


class TestFinalGroupMembersTranslation:
    """Tests for the translation of protocol ids in the published group members."""

    def test_final_group_members_translates_protocol_ids(self) -> None:
        """
        Test that the published group members translate protocol ids to player ids.

        When a native AirPlay player (Apple TV) has AirPlay protocols of other players in
        its group_members, the final state should show the ids of those players instead.
        """
        server = _Server()
        apple_tv, apple_tv_native = attach_mock(
            server.provider("airplay"), "apple_tv_1", "Apple TV"
        )
        apple_tv_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        apple_tv_native._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        sonos_player, sonos_native = attach_mock(server.provider("sonos"), "sonos_1", "Sonos")
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _attach_audio(server, "airplay", "airplay_sonos", sonos_player, set_members=False)
        server.refresh(sonos_player, apple_tv)

        # Final group_members should show player ids
        final_members = apple_tv.group_members
        assert apple_tv.player_id in final_members
        assert sonos_player.player_id in final_members
        # Protocol ids should NOT appear in final state
        assert "apple_tv_1" not in final_members
        assert "airplay_sonos" not in final_members


class TestFinalActiveGroupTranslation:
    """Tests for the published active group of a member a group lists."""

    @pytest.mark.parametrize("listed_as", ["player", "protocol"])
    def test_member_shows_the_group_that_lists_it_as_its_active_group(self, listed_as: str) -> None:
        """A member listed by a playing group, by any id that names it, shows that group."""
        server = _Server()
        group_player, group_protocol = attach_mock(
            server.provider("universal_group"),
            "group_1",
            "House Group",
            role=ProtocolRole.VIRTUAL,
            player_type=PlayerType.GROUP,
        )
        group_protocol._attr_playback_state = PlaybackState.PLAYING
        sonos_player, sonos_native = attach_mock(server.provider("sonos"), "sonos_1", "Sonos")
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _attach_audio(server, "airplay", "airplay_sonos", sonos_player, set_members=False)
        # a group names its members as players; the id of a protocol stands for its player
        group_protocol._attr_group_members = [
            sonos_player.player_id if listed_as == "player" else "airplay_sonos"
        ]
        server.refresh(group_player, sonos_player)

        assert group_player.group_members == [sonos_player.player_id]
        assert sonos_player.active_group == group_player.player_id


class TestFinalSyncedToWithNativeProtocolParent:
    """Tests for the published synced_to when the leader's own protocol is AirPlay."""

    def test_synced_to_native_airplay_player(self) -> None:
        """
        Test that synced_to correctly shows native AirPlay player as parent.

        When the AirPlay protocol of a Sonos player is synced to a native AirPlay player
        (Apple TV), the Sonos's final synced_to should show the Apple TV.
        """
        server = _Server()
        # Apple TV: native AirPlay (the group leader)
        apple_tv, apple_tv_native = attach_mock(
            server.provider("airplay"), "apple_tv_1", "Apple TV"
        )
        apple_tv_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        apple_tv_native._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        sonos_player, sonos_native = attach_mock(server.provider("sonos"), "sonos_1", "Sonos")
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        # the AirPlay protocol of the Sonos, synced to the Apple TV
        sonos_airplay = _attach_audio(
            server, "airplay", "airplay_sonos", sonos_player, set_members=False
        )
        # Set group_members with Apple TV first to indicate synced_to Apple TV
        sonos_airplay._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        server.refresh(apple_tv, sonos_player)

        # Sonos's final synced_to should be the Apple TV player
        assert sonos_player.synced_to == apple_tv.player_id


class TestGroupingLockedPolicy:
    """A grouping-locked player withholds SET_MEMBERS and group targets in its final state."""

    def test_locked_player_withholds_set_members(self) -> None:
        """A locked player drops SET_MEMBERS from its exposed features."""
        server = _Server()
        player, protocol = attach_mock(
            server.provider("wiim", "wiim_instance"),
            "shell_1",
            "Shell",
            protocol_class=_LockedProtocol,
        )
        protocol._attr_supported_features = {PlayerFeature.SET_MEMBERS, PlayerFeature.VOLUME_SET}
        server.refresh()

        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        assert PlayerFeature.VOLUME_SET in player.supported_features

    def test_locked_player_withholds_protocol_derived_set_members(self) -> None:
        """Even the SET_MEMBERS of an audio protocol of the player is suppressed while locked."""
        server = _Server()
        player, protocol = attach_mock(
            server.provider("wiim", "wiim_instance"),
            "shell_1",
            "Shell",
            protocol_class=_LockedProtocol,
        )
        protocol._attr_supported_features = set()  # no native grouping of its own
        _attach_audio(server, "airplay", "airplay_p", player)
        server.refresh()

        assert PlayerFeature.SET_MEMBERS not in player.supported_features

    def test_locked_player_offers_no_group_targets(self) -> None:
        """A locked player exposes no group targets, even an otherwise groupable peer."""
        server = _Server()
        provider = server.provider("wiim", "wiim_instance")
        player, protocol = attach_mock(provider, "shell_1", "Shell", protocol_class=_LockedProtocol)
        protocol._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        protocol._attr_can_group_with = {"peer_1"}
        peer, _ = attach_mock(provider, "peer_1", "Peer")
        server.refresh(peer, player)

        assert player.can_group_with == set()

    def test_unlocked_player_keeps_grouping(self) -> None:
        """A normal (unlocked) player keeps SET_MEMBERS and its group targets."""
        server = _Server()
        provider = server.provider("wiim", "wiim_instance")
        player, protocol = attach_mock(provider, "shell_2", "Shell2")
        protocol._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        protocol._attr_can_group_with = {"peer_2"}
        peer, _ = attach_mock(provider, "peer_2", "Peer2")
        server.refresh(peer, player)

        assert PlayerFeature.SET_MEMBERS in player.supported_features
        assert peer.player_id in player.can_group_with

    def test_locked_peer_excluded_from_group_targets(self) -> None:
        """An unlocked leader drops a locked peer but keeps an unlocked one as a target."""
        server = _Server()
        provider = server.provider("wiim", "wiim_instance")
        leader, leader_native = attach_mock(provider, "shell_1", "Shell")
        leader_native._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader_native._attr_can_group_with = {"locked_peer", "open_peer"}
        locked_peer, _ = attach_mock(
            provider, "locked_peer", "Locked Peer", protocol_class=_LockedProtocol
        )
        open_peer, _ = attach_mock(provider, "open_peer", "Open Peer")
        server.refresh(locked_peer, open_peer, leader)

        # the locked peer keeps its own group read-only, so it is not offered as a target,
        # while the ordinary peer still is
        assert locked_peer.player_id not in leader.can_group_with
        assert open_peer.player_id in leader.can_group_with

    def test_linked_protocol_cannot_reintroduce_locked_member(self) -> None:
        """A locked member offered by an audio protocol is still kept out of the targets."""
        server = _Server()
        provider = server.provider("wiim", "wiim_instance")
        leader, leader_native = attach_mock(provider, "shell_1", "Shell")
        leader_native._attr_supported_features = set()  # no native grouping of its own
        _attach_audio(
            server, "airplay", "airplay_p", leader, can_group_with={"locked_peer", "open_peer"}
        )
        locked_peer, _ = attach_mock(
            provider, "locked_peer", "Locked Peer", protocol_class=_LockedProtocol
        )
        open_peer, _ = attach_mock(provider, "open_peer", "Open Peer")
        server.refresh(locked_peer, open_peer, leader)

        # the audio protocol lists the locked peer, but the final filter still excludes it
        assert locked_peer.player_id not in leader.can_group_with
        assert open_peer.player_id in leader.can_group_with


def _live_source(item_id: str, provider: str = "spotify_connect") -> AudioSource:
    """Build the AudioSource a live session publishes on a player."""
    return AudioSource(
        item_id=item_id,
        provider=provider,
        name=f"Live source ({item_id})",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
    )


class TestCanGroupWithExternalSource:
    """Grouping candidates while something other than the player's queue is playing."""

    @staticmethod
    def _build_rig(server: _Server) -> dict[str, Any]:
        """
        Wire a player with one native peer and one reachable only over AirPlay.

        Returns the players by name, with the native protocol of the player whose grouping
        candidates are asserted on.
        """
        sonos = server.provider("sonos", "sonos_instance")
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_native._attr_can_group_with = {"sonos_456"}
        sonos_player_b, _ = attach_mock(sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        _attach_audio(
            server,
            "airplay",
            "airplay_sonos",
            sonos_player,
            identifiers=MAC_1,
            can_group_with={"airplay_other"},
        )
        wiim_player, _ = attach_mock(sonos, "wiim_789", "Bedroom", identifiers=MAC_3)
        _attach_audio(
            server,
            "airplay",
            "airplay_other",
            wiim_player,
            identifiers=MAC_3,
            can_group_with={"airplay_sonos"},
        )
        server.refresh()
        return {
            "player": sonos_player,
            "native": sonos_native,
            "native_peer": sonos_player_b,
            "airplay_peer": wiim_player,
        }

    def test_live_audio_source_keeps_protocol_candidates(self) -> None:
        """A source MA streams itself (e.g. Spotify Connect) can still be grouped."""
        server = _Server()
        rig = self._build_rig(server)
        player_id = rig["player"].player_id
        server.controller._start_audio_source_session(
            player_id, _live_source(player_id), "spotify_connect"
        )
        server.refresh(rig["player"])

        assert rig["player"].state.active_source == f"spotify_connect://audio_source/{player_id}"
        # MA produces the audio, so both the native peer and the one reachable
        # only over AirPlay can be added to it
        assert rig["player"].state.can_group_with == {
            rig["native_peer"].player_id,
            rig["airplay_peer"].player_id,
        }

    def test_device_own_external_source_hides_protocol_candidates(self) -> None:
        """The device can still group its own line-in natively, just not over a protocol."""
        server = _Server()
        rig = self._build_rig(server)
        # a live source elsewhere says nothing about what this player is playing
        airplay_peer_id = rig["airplay_peer"].player_id
        server.controller._start_audio_source_session(
            airplay_peer_id, _live_source(airplay_peer_id), "vban"
        )
        rig["native"]._attr_active_source = "tv"
        rig["native"]._attr_playback_state = PlaybackState.PLAYING
        server.refresh(rig["player"])

        assert rig["player"].state.active_source == "tv"
        # the device distributes its own input to its own kind, but MA cannot put
        # audio it never receives onto an AirPlay/Sendspin protocol
        assert rig["player"].state.can_group_with == {rig["native_peer"].player_id}


class TestExternalSourceTakeover:
    """Whether a source change while a protocol renders the audio counts as a takeover."""

    @staticmethod
    def _playing_over_airplay(server: _Server) -> dict[str, Any]:
        """Wire a player rendering through its AirPlay protocol."""
        rig = TestCanGroupWithExternalSource._build_rig(server)
        rig["native"]._attr_playback_state = PlaybackState.PLAYING
        rig["player"].set_active_output_protocol("airplay_sonos")
        server.refresh(rig["player"])
        return rig

    def test_live_audio_source_is_not_a_takeover(self) -> None:
        """MA putting a live source on the player must not tear down its protocol group."""
        server = _Server()
        rig = self._playing_over_airplay(server)
        player_id = rig["player"].player_id
        server.controller._start_audio_source_session(
            player_id, _live_source(player_id), "spotify_connect"
        )
        server.refresh(rig["player"])

        server.controller._check_external_source_takeover(rig["player"])

        assert rig["player"].active_output_protocol == "airplay_sonos"

    def test_device_own_external_source_stays_external(self) -> None:
        """A source MA does not produce is still classified as external."""
        # checked on the classifier directly: while an output protocol renders the audio,
        # the published active source overrules what the device reports, so a device-local
        # source cannot be driven through _check_external_source_takeover at all
        server = _Server()
        rig = TestCanGroupWithExternalSource._build_rig(server)

        assert not server.controller._is_ma_managed_source(rig["player"], "tv")
