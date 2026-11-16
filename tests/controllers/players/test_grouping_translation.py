"""Tests for how a group of players is translated to the protocols that carry it."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, ProtocolRole
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.grouping import grouping_snapshot
from music_assistant.controllers.players.grouping_plan import (
    ChangeKind,
    GroupChange,
    GroupRules,
    GroupStep,
    NativeLeg,
    Note,
    ProtocolLeg,
    plan_group_change,
)
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass

MAC_1 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"}
MAC_2 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"}
MAC_3 = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"}


class _SessionBoundProtocol(MockProtocol):
    """A protocol whose native grouping attaches members to its own stream (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members ride this protocol's own stream session."""
        return True


class _NativeGroupingProtocol(MockProtocol):
    """A protocol of a device that runs its own multiroom and prefers native grouping."""

    @property
    def prefer_native_grouping(self) -> bool:
        """Prefer native grouping over any audio protocol."""
        return True


class _RejectsNativeGroupingProtocol(MockProtocol):
    """A leader that never accepts another device as a native group peer."""

    def is_native_group_compatible(self, other: PlayerProtocol) -> bool:
        """Reject every peer as native-incompatible (e.g. a cross-backend device)."""
        return False


def _controller() -> tuple[PlayerController, MagicMock, dict[str, str]]:
    """
    Return a players controller on a mocked server, that server, and the preferred outputs.

    The preferred output protocol of a player is read from the returned dict, by player id.
    """
    mass = mock_player_mass()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    preferred: dict[str, str] = {}

    def _get_player_value(player_id: str, key: str, default: Any = None) -> Any:
        if key == CONF_PREFERRED_OUTPUT_PROTOCOL:
            return preferred.get(player_id, default)
        return default

    mass.config.get_player_value = MagicMock(side_effect=_get_player_value)
    controller = PlayerController(mass)
    mass.players = controller
    return controller, mass, preferred


def _refresh(controller: PlayerController) -> None:
    """Recalculate the state of every player, as the controller does after a change."""
    for player in controller._players.values():
        player.refresh_state(signal_event=False)


def _attach_audio(
    provider: MockProvider,
    protocol_id: str,
    player: Player,
    name: str | None = None,
    identifiers: dict[IdentifierType, str] | None = None,
) -> MockProtocol:
    """
    Attach an audio protocol that can group to a player and return it.

    :param provider: The provider of the protocol.
    :param protocol_id: Id of the protocol.
    :param player: The player of its device.
    :param name: The name its device reports.
    :param identifiers: The identifiers it reports.
    """
    _, protocol = attach_mock(
        provider,
        protocol_id,
        name,
        role=ProtocolRole.AUDIO,
        player=player,
        identifiers=identifiers,
    )
    protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    return protocol


def _nested_admissions(controller: PlayerController) -> AsyncMock:
    """
    Run the next member change for real and catch the member changes nested in it.

    :param controller: The players controller.
    :return: The mock the nested member changes go to.
    """
    entry = controller._handle_set_members
    nested = AsyncMock()
    calls = 0

    async def _handle_set_members(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await entry(*args, **kwargs)
        return await nested(*args, **kwargs)

    controller._handle_set_members = _handle_set_members  # type: ignore[method-assign]
    return nested


def _planned(
    controller: PlayerController,
    parent: Player,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> tuple[ProtocolLeg | None, NativeLeg | None]:
    """
    Return the calls a member change on a parent decides on the state as it is.

    That is the join call of its protocol group and the call of the parent's own protocol. The
    admission is planned to its end first, on the same state.

    :param controller: The players controller.
    :param parent: The leader.
    :param add: The players to add.
    :param remove: The players to remove.
    """
    snapshot = grouping_snapshot(controller)
    change = GroupChange(
        kind=ChangeKind.MEMBERS,
        target_id=parent.player_id,
        add=tuple(add),
        remove=tuple(remove),
    )
    plan = plan_group_change(snapshot, change)
    steps: list[GroupStep] = list(plan.steps)
    while plan.progress is not None:
        plan = plan_group_change(snapshot, change, plan.progress)
        steps += plan.steps
    notes = [step for step in steps if isinstance(step, Note)]
    leg: ProtocolLeg | None = None
    native: NativeLeg | None = None
    if plan.join is not None:
        rules = GroupRules(snapshot, notes.append)
        leader = rules.held_player(parent.player_id)
        lists = rules.translate(leader, plan.join.add, plan.join.remove)
        decided = rules.protocol_leg(leader, lists)
        if decided is None or isinstance(decided, ProtocolLeg):
            leg = decided
            native = rules.native_leg(leader, lists.native_add, lists.native_remove)
    for note in notes:
        # the log lines of the change, as the executor writes them
        controller.logger.log(note.level, note.message, *note.args)
    return leg, native


def _grouping(
    controller: PlayerController,
    parent: Player,
    member_ids: Iterable[str],
    *,
    group_protocol: PlayerProtocol | None = None,
    remove: Iterable[str] = (),
) -> tuple[list[str], list[str], PlayerProtocol | None, str | None]:
    """
    Return how a member change groups members with a parent.

    That is the ids its protocol group adds (or removes), those its own protocol adds (or
    removes), the protocol of the group and its domain. The members pass the admission: the
    parent offers them. A group protocol given becomes the parent's active output first:
    that is how a group is on a protocol.

    :param controller: The players controller.
    :param parent: The leader.
    :param member_ids: The players to add.
    :param group_protocol: The protocol the group is on.
    :param remove: The players to remove.
    """
    if group_protocol is not None:
        parent.set_active_output_protocol(group_protocol.protocol_id)
        _refresh(controller)
    member_ids = list(member_ids)
    parent.state.can_group_with = set(parent.can_group_with) | set(member_ids)
    leg, native = _planned(controller, parent, member_ids, remove)
    protocol_members: list[str] = []
    protocol: PlayerProtocol | None = None
    if leg is not None:
        protocol_members = list(leg.add if member_ids else leg.remove)
        protocol = controller.get_protocol(leg.protocol_id)
    native_members: list[str] = []
    if native is not None:
        native_members = list(native.add if member_ids else native.remove)
    return (
        protocol_members,
        native_members,
        protocol,
        protocol.provider.domain if protocol else None,
    )


class TestPlayerGrouping:
    """Tests for player grouping scenarios."""

    def test_native_to_native_grouping(self) -> None:
        """Test that native players from same provider can group together."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", mass=mass)
        player_a, protocol_a = attach_mock(sonos, "sonos_123", "Living Room", identifiers=MAC_1)
        protocol_a._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        protocol_a._attr_can_group_with = {"sonos_456"}
        player_b, protocol_b = attach_mock(sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        protocol_b._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _refresh(controller)

        protocol_members, native_members, _, _ = _grouping(
            controller, player_a, [player_b.player_id], group_protocol=None
        )

        # Should use native grouping (same provider), naming the member as its provider does
        assert native_members == ["sonos_456"]
        assert len(protocol_members) == 0

    def test_protocol_to_protocol_grouping(self) -> None:
        """Test that players can group via a shared protocol."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", mass=mass)
        wiim = MockProvider("wiim", mass=mass)
        airplay = MockProvider("airplay", mass=mass)
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player, wiim_native = attach_mock(wiim, "wiim_456", "Bedroom", identifiers=MAC_2)
        wiim_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_airplay = _attach_audio(
            airplay, "airplay_sonos", sonos_player, "Living Room (AirPlay)", MAC_1
        )
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        wiim_airplay = _attach_audio(
            airplay, "airplay_wiim", wiim_player, "Bedroom (AirPlay)", MAC_2
        )
        wiim_airplay._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        _refresh(controller)

        # Translate members for protocol grouping (via AirPlay)
        protocol_members, native_members, protocol, _ = _grouping(
            controller, sonos_player, [wiim_player.player_id], group_protocol=sonos_airplay
        )

        # Should use protocol grouping (AirPlay)
        assert protocol_members == ["airplay_wiim"]
        assert len(native_members) == 0
        assert protocol is sonos_airplay

    def test_hybrid_grouping(self) -> None:
        """Test hybrid grouping: native + protocol players in same group."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos_instance", mass=mass)
        wiim = MockProvider("wiim", instance_id="wiim_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        sonos_a, sonos_a_native = attach_mock(sonos, "sonos_123", "Living Room", identifiers=MAC_1)
        sonos_a_native._attr_supported_features |= {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.PLAY_MEDIA,
        }
        sonos_a_native._attr_can_group_with = {"sonos_456"}
        sonos_b, sonos_b_native = attach_mock(sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        sonos_b_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player, wiim_native = attach_mock(wiim, "wiim_789", "Bedroom", identifiers=MAC_3)
        wiim_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_airplay = _attach_audio(
            airplay, "airplay_sonos", sonos_a, "Living Room (AirPlay)", MAC_1
        )
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        wiim_airplay = _attach_audio(
            airplay, "airplay_wiim", wiim_player, "Bedroom (AirPlay)", MAC_3
        )
        wiim_airplay._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        wiim_player.set_active_output_protocol("airplay_wiim")
        _refresh(controller)

        # Group Sonos B (native) + WiiM (via AirPlay) to Sonos A
        protocol_members, native_members, _protocol, _ = _grouping(
            controller,
            sonos_a,
            [sonos_b.player_id, wiim_player.player_id],
            group_protocol=sonos_airplay,
        )

        # Should have hybrid group: native Sonos B + protocol WiiM
        assert native_members == ["sonos_456"]
        assert protocol_members == ["airplay_wiim"]

    def test_protocol_selection_requires_set_members(self) -> None:
        """Test that only protocols with SET_MEMBERS support are selected for grouping."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", mass=mass)
        wiim = MockProvider("wiim", mass=mass)
        dlna = MockProvider("dlna", mass=mass)
        airplay = MockProvider("airplay", mass=mass)
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player, wiim_native = attach_mock(wiim, "wiim_456", "Bedroom", identifiers=MAC_2)
        wiim_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        # DLNA does NOT support SET_MEMBERS
        sonos_dlna = _attach_audio(dlna, "dlna_sonos", sonos_player, "Living Room (DLNA)", MAC_1)
        sonos_dlna._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        wiim_dlna = _attach_audio(dlna, "dlna_wiim", wiim_player, "Bedroom (DLNA)", MAC_2)
        wiim_dlna._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        # AirPlay DOES support SET_MEMBERS
        sonos_airplay = _attach_audio(
            airplay, "airplay_sonos", sonos_player, "Living Room (AirPlay)", MAC_1
        )
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        wiim_airplay = _attach_audio(
            airplay, "airplay_wiim", wiim_player, "Bedroom (AirPlay)", MAC_2
        )
        wiim_airplay._attr_can_group_with = {"airplay_sonos"}
        _refresh(controller)

        # Translate members - should skip DLNA (no SET_MEMBERS) and select AirPlay
        protocol_members, _native_members, protocol, protocol_domain = _grouping(
            controller, sonos_player, [wiim_player.player_id], group_protocol=None
        )

        # Should select AirPlay (supports SET_MEMBERS) not DLNA
        assert protocol_members == ["airplay_wiim"]
        assert protocol_domain == "airplay"
        assert protocol is sonos_airplay

        # A group already on DLNA must be moved off it as well, because DLNA cannot
        # group members itself
        protocol_members, _native_members, protocol, protocol_domain = _grouping(
            controller, sonos_player, [wiim_player.player_id], group_protocol=sonos_dlna
        )

        assert protocol_members == ["airplay_wiim"]
        assert protocol_domain == "airplay"
        assert protocol is sonos_airplay


class TestJoinActiveNativeSession:
    """
    Grouping a child onto a parent that is already playing natively.

    A child's preferred output protocol must only steer protocol selection when the child
    initiates its own playback. When the child joins a parent that already holds an active
    native session, it should adopt native grouping if compatible, instead of forcing the
    whole group onto the child's preferred protocol.
    """

    @staticmethod
    def _mark_native_playing(player: Player) -> None:
        """Put the player into a live native playback session."""
        assert player.own_protocol is not None
        player.own_protocol._attr_playback_state = PlaybackState.PLAYING
        player.set_active_output_protocol("native")
        player.refresh_state(signal_event=False)

    @staticmethod
    def _build_topology(
        mass: MagicMock,
    ) -> tuple[dict[str, Player], dict[str, MockProtocol]]:
        """
        Build parent Sonos A + child Sonos B, both AirPlay-capable and natively groupable.

        The parent also has an AirPlay protocol that supports SET_MEMBERS, so the child's
        preferred AirPlay protocol *would* be selectable if it were given priority.

        :param mass: The mocked server.
        """
        sonos = MockProvider("sonos", instance_id="sonos_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        sonos_a, native_a = attach_mock(sonos, "sonos_a", "Office", identifiers=MAC_1)
        native_a._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        native_a._attr_can_group_with = {"sonos_b"}
        sonos_b, native_b = attach_mock(sonos, "sonos_b", "Bathroom", identifiers=MAC_2)
        native_b._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        native_b._attr_can_group_with = {"sonos_a"}
        airplay_a = _attach_audio(airplay, "airplay_a", sonos_a, "Office (AirPlay)", MAC_1)
        airplay_a._attr_can_group_with = {"airplay_b"}
        airplay_b = _attach_audio(airplay, "airplay_b", sonos_b, "Bathroom (AirPlay)", MAC_2)
        airplay_b._attr_can_group_with = {"airplay_a"}
        _refresh(mass.players)
        players = {"sonos_a": sonos_a, "sonos_b": sonos_b}
        protocols = {"airplay_a": airplay_a, "airplay_b": airplay_b}
        return players, protocols

    @staticmethod
    def _add_airplay_only_child(
        mass: MagicMock, players: dict[str, Player], protocols: dict[str, MockProtocol]
    ) -> None:
        """
        Add a WiiM child that can only group via AirPlay (no native grouping with Sonos).

        Different provider instance and absent from the parent's can_group_with, so
        it can not group natively with the parent.
        """
        wiim = MockProvider("wiim", instance_id="wiim_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        wiim_c, native_c = attach_mock(wiim, "wiim_c", "Bedroom", identifiers=MAC_3)
        native_c._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        airplay_c = _attach_audio(airplay, "airplay_c", wiim_c, "Bedroom (AirPlay)", MAC_3)
        airplay_c._attr_can_group_with = {"airplay_a"}
        # Allow the parent's AirPlay protocol to group with the new AirPlay child.
        protocols["airplay_a"]._attr_can_group_with = {"airplay_b", "airplay_c"}
        players["wiim_c"] = wiim_c
        _refresh(mass.players)

    def test_native_active_parent_ignores_child_preferred_protocol(self) -> None:
        """A child joining a natively-playing parent groups natively, not via preferred AirPlay."""
        controller, mass, preferred = _controller()
        players, _ = self._build_topology(mass)
        preferred[players["sonos_b"].player_id] = "airplay_b"
        # Parent A is already playing natively.
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, _ = _grouping(
            controller, players["sonos_a"], [players["sonos_b"].player_id], group_protocol=None
        )

        # Native grouping is used; the child's AirPlay preference is ignored on join.
        assert native_members == ["sonos_b"]
        assert protocol_members == []

    def test_fresh_group_still_honors_child_preferred_protocol(self) -> None:
        """When the parent is not playing, the child's preferred protocol still steers selection."""
        controller, mass, preferred = _controller()
        players, _ = self._build_topology(mass)
        preferred[players["sonos_b"].player_id] = "airplay_b"
        # Parent A has no active session (active_output_protocol stays None).
        assert players["sonos_a"].active_output_protocol is None

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller, players["sonos_a"], [players["sonos_b"].player_id], group_protocol=None
        )

        # Same topology as the active-native case, but here the preference applies.
        assert protocol_members == ["airplay_b"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_idle_parent_with_stale_native_protocol_honors_child_preferred(self) -> None:
        """A stopped parent (active protocol lingers briefly) does not force the native-join path."""
        controller, mass, preferred = _controller()
        players, _ = self._build_topology(mass)
        preferred[players["sonos_b"].player_id] = "airplay_b"
        # Parent A stopped: active protocol still reads "native" but playback is idle.
        players["sonos_a"].set_active_output_protocol("native")
        assert players["sonos_a"].playback_state == PlaybackState.IDLE

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller, players["sonos_a"], [players["sonos_b"].player_id], group_protocol=None
        )

        # No live session to join, so the child's preference applies.
        assert protocol_members == ["airplay_b"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_native_active_parent_airplay_only_child_uses_protocol(self) -> None:
        """An AirPlay-only child (cannot group natively) still joins via its preferred protocol."""
        controller, mass, preferred = _controller()
        players, protocols = self._build_topology(mass)
        self._add_airplay_only_child(mass, players, protocols)
        preferred[players["wiim_c"].player_id] = "airplay_c"
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller, players["sonos_a"], [players["wiim_c"].player_id], group_protocol=None
        )

        # No native path exists for the WiiM, so it falls back to AirPlay grouping.
        assert protocol_members == ["airplay_c"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_native_active_parent_protocol_only_child_switches_via_common_protocol(
        self,
    ) -> None:
        """
        A child with no native path (and no explicit preference) still forces the switch.

        Even without a configured preferred protocol, a child that can only reach the group via
        a shared protocol (here AirPlay, which the natively-playing parent also supports) is
        grouped via that protocol - moving the group onto it.
        """
        controller, mass, _ = _controller()
        players, protocols = self._build_topology(mass)
        self._add_airplay_only_child(mass, players, protocols)
        self._mark_native_playing(players["sonos_a"])

        # No preferred_output_protocol override - the WiiM resolves via the common-protocol path.
        protocol_members, native_members, _, protocol_domain = _grouping(
            controller, players["sonos_a"], [players["wiim_c"].player_id], group_protocol=None
        )

        assert protocol_members == ["airplay_c"]
        assert native_members == []
        assert protocol_domain == "airplay"

    @pytest.mark.parametrize(
        "member_order",
        [["wiim_c", "sonos_b"], ["sonos_b", "wiim_c"]],
    )
    def test_native_active_parent_keeps_mixed_batch_cohesive(self, member_order: list[str]) -> None:
        """
        A mixed batch stays cohesive on one protocol regardless of member order.

        When an AirPlay-only child forces the group onto AirPlay, the natively-groupable Sonos
        child adopts that same protocol rather than splitting off into a native sub-group -
        whether it is added before or after the AirPlay-only child.
        """
        controller, mass, _ = _controller()
        players, protocols = self._build_topology(mass)
        self._add_airplay_only_child(mass, players, protocols)
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller,
            players["sonos_a"],
            [players[member].player_id for member in member_order],
            group_protocol=None,
        )

        assert native_members == []
        assert set(protocol_members) == {"airplay_c", "airplay_b"}
        assert protocol_domain == "airplay"


class _SessionBoundGroup:
    """
    A leader with a natively groupable member and a player only reachable over a bridge.

    The leader and its member speak the same (speaker) provider, so they group natively.
    Both also have a bridge protocol, which is the only way to reach the third player:
    a player whose own protocol is of the bridge domain.
    """

    def __init__(
        self,
        mass: MagicMock,
        leader_class: type[MockProtocol],
        with_orphan: bool = False,
        with_airplay: bool = False,
    ) -> None:
        """
        Build the players.

        :param mass: The mocked server.
        :param leader_class: The protocol class of the leader's own protocol.
        :param with_orphan: Add a second natively groupable member without a bridge protocol.
        :param with_airplay: Also give the leader and its member an AirPlay protocol.
        """
        speaker = MockProvider("speaker", instance_id="speaker_instance", mass=mass)
        bridge = MockProvider("sendspin", instance_id="sendspin_instance", mass=mass)
        self.leader, self.leader_native = attach_mock(
            speaker, "speaker_leader", "Living Room", protocol_class=leader_class
        )
        self.leader_native._attr_supported_features |= {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.PLAY_MEDIA,
        }
        # the leader offers its members as its protocols report them
        self.leader_native._attr_can_group_with = {"speaker_member", "speaker_orphan"}
        self.member, self.member_native = attach_mock(speaker, "speaker_member", "Kitchen")
        self.member_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        self.bridge_leader = _attach_audio(
            bridge, "bridge_leader", self.leader, "Living Room (Bridge)"
        )
        self.bridge_leader._attr_can_group_with = {"bridge_member", "bridge_only"}
        self.bridge_member = _attach_audio(bridge, "bridge_member", self.member, "Kitchen (Bridge)")
        # a player whose own protocol is of the bridge domain: only reachable over the bridge
        self.bridge_only, self.bridge_only_native = attach_mock(
            bridge, "bridge_only", "Voice Assistant"
        )
        self.bridge_only_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        self.orphan: Player | None = None
        if with_orphan:
            self.orphan, orphan_native = attach_mock(speaker, "speaker_orphan", "Bedroom")
            orphan_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        self.airplay_leader: MockProtocol | None = None
        self.airplay_member: MockProtocol | None = None
        if with_airplay:
            airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
            self.airplay_leader = _attach_audio(
                airplay, "airplay_leader", self.leader, "Living Room (AirPlay)"
            )
            self.airplay_member = _attach_audio(
                airplay, "airplay_member", self.member, "Kitchen (AirPlay)"
            )
        _refresh(mass.players)


class TestSessionBoundNativeGrouping:
    """
    Grouping a parent whose native grouping only works while it renders its own stream.

    Such native members are attached to that very stream, so they cannot stay natively
    grouped once the group is moved onto one of the parent's output protocols. Parents
    with device-side native grouping are unaffected and keep their hybrid groups.
    """

    @pytest.mark.parametrize("bridge_only_first", [False, True])
    def test_native_members_follow_the_group_protocol(self, bridge_only_first: bool) -> None:
        """A natively groupable member moves onto the protocol the group ends up on."""
        controller, mass, _ = _controller()
        group = _SessionBoundGroup(mass, _SessionBoundProtocol)
        member_order = [group.member.player_id, group.bridge_only.player_id]
        if bridge_only_first:
            member_order.reverse()

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller, group.leader, member_order, group_protocol=None
        )

        assert native_members == []
        assert set(protocol_members) == {"bridge_only", "bridge_member"}
        assert protocol_domain == "sendspin"

    def test_member_without_the_group_protocol_is_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A native member that lacks the group's protocol is dropped, the others still move."""
        controller, mass, _ = _controller()
        group = _SessionBoundGroup(mass, _SessionBoundProtocol, with_orphan=True)
        assert group.orphan is not None

        with caplog.at_level(logging.WARNING):
            protocol_members, native_members, _, _ = _grouping(
                controller,
                group.leader,
                [
                    group.member.player_id,
                    group.orphan.player_id,
                    group.bridge_only.player_id,
                ],
                group_protocol=None,
            )

        assert native_members == []
        assert set(protocol_members) == {"bridge_only", "bridge_member"}
        assert (
            "Cannot group Bedroom with Living Room: the group plays through the sendspin "
            "protocol, which Bedroom does not support" in caplog.text
        )

    def test_device_side_parent_keeps_its_hybrid_group(self) -> None:
        """A parent with device-side native grouping keeps its members natively grouped."""
        controller, mass, _ = _controller()
        group = _SessionBoundGroup(mass, MockProtocol)

        protocol_members, native_members, _, protocol_domain = _grouping(
            controller,
            group.leader,
            [group.member.player_id, group.bridge_only.player_id],
            group_protocol=None,
        )

        assert native_members == ["speaker_member"]
        assert protocol_members == ["bridge_only"]
        assert protocol_domain == "sendspin"


class TestRegroupOffUnneededProtocol:
    """
    A group that was moved onto a bridge protocol for one member leaves it once that member left.

    The regrouping only happens on a fresh playback start, so running playback is never
    interrupted.
    """

    @staticmethod
    def _update_state_on_set_members(protocol: MockProtocol) -> None:
        """Recalculate the player's state after each member change, as a real provider does."""
        set_members = protocol.set_members

        async def _set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            await set_members(player_ids_to_add, player_ids_to_remove)
            protocol.update_state(signal_event=False)

        protocol.set_members = _set_members  # type: ignore[method-assign]

    async def _build_left_on_bridge(
        self, with_airplay: bool = False
    ) -> tuple[PlayerController, MagicMock, dict[str, str], _SessionBoundGroup]:
        """
        Build two session-bound speakers left grouped on the bridge after the bridge-only member left.

        The bridge-only member joins the native group (moving it onto the bridge) and leaves it
        again, exactly as the grouping commands do it.
        """
        controller, mass, preferred = _controller()
        group = _SessionBoundGroup(mass, _SessionBoundProtocol, with_airplay=with_airplay)
        for protocol in controller._protocols.values():
            assert isinstance(protocol, MockProtocol)
            self._update_state_on_set_members(protocol)
            protocol.play_media = AsyncMock()  # type: ignore[method-assign]
        await group.leader_native.set_members(player_ids_to_add=["speaker_member"])
        group.leader.set_active_output_protocol("native")

        await controller._handle_set_members(group.leader, [group.bridge_only.player_id])
        await controller._handle_set_members(group.leader, None, [group.bridge_only.player_id])

        assert group.bridge_leader.group_members == ["bridge_leader", "bridge_member"]
        assert "speaker_member" not in group.leader_native.group_members
        return controller, mass, preferred, group

    @pytest.mark.parametrize("active_protocol", ["bridge_leader", None])
    async def test_fresh_start_regroups_natively(self, active_protocol: str | None) -> None:
        """
        The remaining speakers are grouped natively again and the group plays natively.

        The leader's active protocol may still point at the bridge, or already be cleared
        once it went idle.
        """
        controller, _, _, group = await self._build_left_on_bridge()
        group.leader.set_active_output_protocol(active_protocol)
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(group.leader.player_id, media)

        assert group.bridge_leader.group_members == ["bridge_leader"]
        assert group.leader_native.group_members == ["speaker_leader", "speaker_member"]
        assert group.member.active_output_protocol is None
        assert group.leader.active_output_protocol == "native"
        group.leader_native.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        group.bridge_leader.play_media.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_group_that_still_needs_the_bridge_is_left_alone(self) -> None:
        """A member that can only be reached through the bridge keeps the group on it."""
        controller, _, _, group = await self._build_left_on_bridge()
        await controller._handle_set_members(group.leader, [group.bridge_only.player_id])
        bridge_members = list(group.bridge_leader.group_members)
        bridge_set_members = AsyncMock(wraps=group.bridge_leader.set_members)
        group.bridge_leader.set_members = bridge_set_members  # type: ignore[method-assign]
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(group.leader.player_id, media)

        bridge_set_members.assert_not_awaited()
        assert group.bridge_leader.group_members == bridge_members
        assert set(bridge_members) == {"bridge_leader", "bridge_member", "bridge_only"}
        assert "speaker_member" not in group.leader_native.group_members
        group.bridge_leader.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        group.leader_native.play_media.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_leader_that_prefers_the_bridge_keeps_its_group(self) -> None:
        """A leader that plays through its preferred bridge keeps its members on that bridge."""
        controller, _, preferred, group = await self._build_left_on_bridge()
        preferred[group.leader.player_id] = "bridge_leader"
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(group.leader.player_id, media)

        assert group.bridge_leader.group_members == ["bridge_leader", "bridge_member"]
        group.bridge_leader.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]

    async def test_group_that_prefers_another_protocol_regroups_onto_it(self) -> None:
        """
        Speakers that all prefer another protocol are regrouped onto that protocol.

        The leader plays through its preferred protocol, so its members can follow it there.
        """
        controller, _, preferred, group = await self._build_left_on_bridge(with_airplay=True)
        assert group.airplay_leader is not None
        preferred[group.leader.player_id] = "airplay_leader"
        preferred[group.member.player_id] = "airplay_member"
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(group.leader.player_id, media)

        assert group.bridge_leader.group_members == ["bridge_leader"]
        assert group.airplay_leader.group_members == ["airplay_leader", "airplay_member"]
        assert group.leader.active_output_protocol == "airplay_leader"
        group.airplay_leader.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        group.bridge_leader.play_media.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("playback_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
    async def test_running_playback_is_not_interrupted(self, playback_state: PlaybackState) -> None:
        """A group that is playing (or paused) keeps playing through the bridge."""
        controller, _, _, group = await self._build_left_on_bridge()
        # the bridge renders the group, so the leader reports its playback state
        group.leader.set_active_output_protocol("bridge_leader")
        group.bridge_leader._attr_playback_state = playback_state
        group.leader.refresh_state(signal_event=False)
        assert group.leader.playback_state == playback_state
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(group.leader.player_id, media)

        assert group.bridge_leader.group_members == ["bridge_leader", "bridge_member"]
        assert "speaker_member" not in group.leader_native.group_members
        group.bridge_leader.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        group.leader_native.play_media.assert_not_awaited()  # type: ignore[attr-defined]


class TestProtocolSwitchingDuringPlayback:
    """Tests for dynamic protocol switching when group members change during playback."""

    @staticmethod
    def _build_session_bound_group(
        mass: MagicMock, playback_state: PlaybackState
    ) -> _SessionBoundGroup:
        """
        Build a leader that renders its own stream, one native member and a bridge-only player.

        :param mass: The mocked server.
        :param playback_state: The state the leader is in when the switch happens.
        """
        group = _SessionBoundGroup(mass, _SessionBoundProtocol)
        group.leader_native._attr_group_members = ["speaker_leader", "speaker_member"]
        group.leader_native._attr_playback_state = playback_state
        group.leader.refresh_state(signal_event=False)
        group.leader.set_active_output_protocol("native")
        return group

    @staticmethod
    def _record_switch(group: _SessionBoundGroup) -> list[tuple[str, Any]]:
        """Record the provider level calls a switch of the leader performs, in order."""
        calls: list[tuple[str, Any]] = []
        leader_set_members = group.leader_native.set_members
        bridge_set_members = group.bridge_leader.set_members

        async def record_leader_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            calls.append(("leader_set_members", player_ids_to_remove))
            await leader_set_members(player_ids_to_add, player_ids_to_remove)

        async def record_bridge_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            calls.append(("bridge_set_members", player_ids_to_add))
            await bridge_set_members(player_ids_to_add, player_ids_to_remove)

        async def record_leader_stop() -> None:
            calls.append(("leader_stop", None))

        group.leader_native.set_members = record_leader_set_members  # type: ignore[method-assign]
        group.leader_native.stop = record_leader_stop  # type: ignore[method-assign]
        group.bridge_leader.set_members = record_bridge_set_members  # type: ignore[method-assign]
        return calls

    async def test_grouping_an_idle_player_over_a_protocol_starts_nothing(self) -> None:
        """Grouping a player that does not play marks the protocol it is on, and plays nothing."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_airplay = _attach_audio(
            airplay, "airplay_sonos", sonos_player, "Living Room (AirPlay)", MAC_1
        )
        sonos_airplay._attr_can_group_with = {"airplay_other"}
        # a speaker of another household, so the pair can only group over AirPlay
        other_sonos = MockProvider("sonos", instance_id="sonos_instance_2", mass=mass)
        other_player, _ = attach_mock(other_sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        _attach_audio(airplay, "airplay_other", other_player, "Kitchen (AirPlay)", MAC_2)
        _refresh(controller)
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]
        sonos_airplay.play_media = AsyncMock()  # type: ignore[method-assign]
        sonos_native.play_media = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(sonos_player, [other_player.player_id])

        assert sonos_airplay.group_members == ["airplay_sonos", "airplay_other"]
        # the group is on AirPlay now, so a later start plays through it
        assert sonos_player.active_output_protocol == "airplay_sonos"
        controller.cmd_resume.assert_not_awaited()
        sonos_airplay.play_media.assert_not_awaited()
        sonos_native.play_media.assert_not_awaited()

    async def test_protocol_selected_at_playback_time(self) -> None:
        """Test that protocol is selected when playback starts, not during grouping."""
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_airplay = _attach_audio(
            airplay, "airplay_sonos", sonos_player, "Living Room (AirPlay)", MAC_1
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        # Simulate that AirPlay protocol has group members (needs >1 for grouping check)
        sonos_airplay._attr_group_members = ["airplay_sonos", "airplay_other"]
        _refresh(controller)

        # Protocol should not be set yet
        assert sonos_player.active_output_protocol is None

        # Select protocol for playback
        selected, output_protocol = controller._select_best_output_protocol(sonos_player)

        # Should select AirPlay protocol because it has group members (Priority 1)
        assert selected is sonos_airplay
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "airplay_sonos"

    async def test_no_restart_from_handle_set_members(self) -> None:
        """
        Test that _handle_set_members does NOT restart playback.

        Protocol switching and playback restarts only happen when members join over a protocol,
        not when the last one leaves. This test verifies that _handle_set_members doesn't
        trigger any redundant playback restarts.
        """
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos_instance", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
        # currently playing via AirPlay
        sonos_player, sonos_native = attach_mock(
            sonos, "sonos_123", "Living Room", identifiers=MAC_1
        )
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_native._attr_playback_state = PlaybackState.PLAYING
        sonos_native._attr_group_members = ["sonos_123", "sonos_456"]
        # another Sonos in the group of sonos_123; its synced_to derives from group_members
        _, sonos_native_b = attach_mock(sonos, "sonos_456", "Kitchen", identifiers=MAC_2)
        sonos_native_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # the AirPlay protocol that was used for grouping
        _attach_audio(airplay, "airplay_sonos", sonos_player, "Living Room (AirPlay)", MAC_1)
        _refresh(controller)
        sonos_player.set_active_output_protocol("airplay_sonos")

        # Track if cmd_resume was called
        resume_called = False

        async def mock_cmd_resume(
            player_id: str,  # noqa: ARG001
            source: str | None = None,  # noqa: ARG001
            media: PlayerMedia | None = None,  # noqa: ARG001
        ) -> None:
            nonlocal resume_called
            resume_called = True

        controller.cmd_resume = mock_cmd_resume  # type: ignore[method-assign]

        # Remove member - now only the parent player is left
        # After removal, _select_best_output_protocol would return native
        sonos_native._attr_group_members = ["sonos_123"]
        member = controller.get_player_by_protocol("sonos_456")
        assert member is not None

        # Call _handle_set_members directly to trigger the protocol change check
        await controller._handle_set_members(
            sonos_player,
            player_ids_to_add=None,
            player_ids_to_remove=[member.player_id],
        )

        # Playback should NOT have been restarted because we're going back to native
        assert not resume_called, "cmd_resume should not be called when switching to native"

    @pytest.mark.parametrize("playback_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
    async def test_native_session_is_stopped_and_members_migrate(
        self, playback_state: PlaybackState
    ) -> None:
        """
        A parent that renders its own stream hands its native members over to the new protocol.

        The members join the protocol group in the same call as the new member, after which
        the native session releases them and stops. Playback resumes only for a player that
        was playing, so adding a member to a paused group does not start it.
        """
        controller, mass, _ = _controller()
        group = self._build_session_bound_group(mass, playback_state)
        calls = self._record_switch(group)
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]
        nested = _nested_admissions(controller)

        await controller._handle_set_members(group.leader, [group.bridge_only.player_id])

        assert calls == [
            # the native member joins the protocol group together with the new member
            ("bridge_set_members", ["bridge_only", "bridge_member"]),
            ("leader_set_members", ["speaker_member"]),
            ("leader_stop", None),
        ]
        assert group.leader.active_output_protocol == "bridge_leader"
        # Only a player that was playing resumes: adding a member to a paused group
        # hands the output over without starting playback.
        if playback_state == PlaybackState.PLAYING:
            controller.cmd_resume.assert_awaited_once_with(group.leader.player_id)
        else:
            controller.cmd_resume.assert_not_awaited()
        nested.assert_not_awaited()

    async def test_idle_native_members_migrate_without_stopping_the_parent(self) -> None:
        """
        An idle parent hands its native members over too, with no session to stop.

        A native group outlives the session it was formed for, so the members are still
        attached to the parent when it switches protocol. Leaving them behind would keep
        them listed as grouped while nothing feeds them on the next play.
        """
        controller, mass, _ = _controller()
        group = self._build_session_bound_group(mass, PlaybackState.IDLE)
        calls = self._record_switch(group)
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(group.leader, [group.bridge_only.player_id])

        # the native member joins the protocol group and is released from the parent,
        # but an idle parent has no session to stop and nothing to resume
        assert calls == [
            ("bridge_set_members", ["bridge_only", "bridge_member"]),
            ("leader_set_members", ["speaker_member"]),
        ]
        assert group.leader.active_output_protocol == "bridge_leader"
        controller.cmd_resume.assert_not_awaited()

    @staticmethod
    def _build_protocol_group(mass: MagicMock) -> dict[str, Any]:
        """
        Build a leader playing over AirPlay with one AirPlay member and an idle Sendspin player.

        The leader and the member each have an AirPlay output and a Sendspin bridge.

        :param mass: The mocked server.
        :return: The players and protocols by name.
        """
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        wiim = MockProvider("wiim", instance_id="wiim", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay", mass=mass)
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mass)
        built: dict[str, Any] = {}
        built["leader"], leader_native = attach_mock(sonos, "leader", "Living Room")
        leader_native._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        built["member"], _ = attach_mock(wiim, "member", "Kitchen")
        built["joiner"], joiner_native = attach_mock(sendspin, "joiner", "Bedroom")
        joiner_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        for name in ("leader", "member"):
            player = built[name]
            built[f"{name}_airplay"] = _attach_audio(airplay, f"{name}_airplay", player)
            built[f"{name}_bridge"] = _attach_audio(sendspin, f"{name}_bridge", player)
        built["leader_airplay"]._attr_group_members = ["leader_airplay", "member_airplay"]
        # the leader offers the Sendspin player, as its bridge reports it
        built["leader_bridge"]._attr_can_group_with = {"joiner"}
        built["leader_airplay"]._attr_playback_state = PlaybackState.PLAYING
        _refresh(mass.players)
        built["leader"].set_active_output_protocol("leader_airplay")
        built["member"].set_active_output_protocol("member_airplay")
        return built

    async def test_resume_after_switch_plays_through_the_new_protocol(self) -> None:
        """
        The resume after a protocol switch plays through the protocol that took over.

        The members of the stopped protocol group are re-added on the new protocol after the
        resume, so the stopped group must no longer count as grouped when the resume picks
        the output.
        """
        controller, mass, _ = _controller()
        built = self._build_protocol_group(mass)
        leader = built["leader"]

        selected_on_resume: list[str] = []

        async def record_resume(player_id: str) -> None:
            _refresh(controller)
            player = controller.get_player(player_id)
            assert player is not None
            selected_on_resume.append(
                controller._select_best_output_protocol(player)[0].protocol_id
            )

        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller.cmd_resume = AsyncMock(side_effect=record_resume)  # type: ignore[method-assign]
        nested = _nested_admissions(controller)

        await controller._handle_set_members(leader, [built["joiner"].player_id])

        assert selected_on_resume == ["leader_bridge"]
        # the members of the stopped group are added back after the resume
        nested.assert_awaited_once_with(leader, player_ids_to_add=[built["member"].player_id])

    async def test_switch_that_would_strand_a_member_is_refused(self) -> None:
        """
        A player whose protocol a grouped member cannot follow is not added to the group.

        The re-added member would move the group back onto a protocol the new player cannot
        join, which hands the output back and forth without end.
        """
        controller, mass, _ = _controller()
        built = self._build_protocol_group(mass)
        built["member_bridge"]._attr_available = False
        _refresh(controller)
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(PlayerCommandFailed) as err:
            await controller._handle_set_members(built["leader"], [built["joiner"].player_id])

        assert (
            err.value.translation_key,
            built["leader"].active_output_protocol,
            built["leader_bridge"].group_members,
            controller._handle_cmd_stop.await_count,
        ) == ("member_cannot_follow_protocol", "leader_airplay", [], 0)

    async def test_joining_member_protocol_is_active_before_its_stream_starts(self) -> None:
        """
        A joining member's player knows which protocol carries its audio before its stream starts.

        The member's stream is started from inside set_members, and providers resolve the
        member's volume control while starting it. A device that also exposes a higher
        priority sibling interface (e.g. a soundbar that is both Chromecast and AirPlay)
        would otherwise resolve volume to that sibling and apply its volume semantics to
        a stream it does not attenuate.
        """
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        airplay = MockProvider("airplay", instance_id="airplay", mass=mass)
        cast = MockProvider("chromecast", instance_id="chromecast", mass=mass)
        leader, _ = attach_mock(sonos, "leader", "Kitchen")
        leader_airplay = _attach_audio(airplay, "leader_airplay", leader, "Kitchen (AirPlay)")
        leader_airplay._attr_can_group_with = {"joiner_airplay"}
        # the joining device exposes both a Chromecast and an AirPlay interface, and has no
        # native volume of its own, so its volume control is one of the two protocols
        joiner, joiner_cast = attach_mock(
            cast, "joiner_cast", "Living Room (Cast)", role=ProtocolRole.AUDIO
        )
        _attach_audio(airplay, "joiner_airplay", joiner, "Living Room (AirPlay)")
        assert joiner_cast.supported_features == {PlayerFeature.VOLUME_SET}
        _refresh(controller)

        # While the device sits idle its Chromecast interface owns the volume: the winner is
        # decided by the hardcoded per-domain control priority, not by the output priorities.
        assert joiner._volume_control == "joiner_cast"

        observed_volume_controls: list[str] = []

        async def record_set_members(
            player_ids_to_add: list[str] | None = None,  # noqa: ARG001
            player_ids_to_remove: list[str] | None = None,  # noqa: ARG001
        ) -> None:
            observed_volume_controls.append(joiner._volume_control)

        leader_airplay.set_members = record_set_members  # type: ignore[method-assign]

        await controller._handle_set_members(leader, [joiner.player_id])

        assert observed_volume_controls == ["joiner_airplay"]

    async def test_leaving_member_releases_its_protocol(self) -> None:
        """A member that leaves a protocol group reports playback started on the device again."""
        controller, mass, _ = _controller()
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        leader, _ = attach_mock(sendspin, "leader", "LedFx", role=ProtocolRole.AUDIO)
        leader_sendspin = leader.audio_protocols[0]
        leader_sendspin._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_sendspin._attr_can_group_with = {"member_bridge"}
        member, member_native = attach_mock(sonos, "member", "Living Room")
        attach_mock(
            sendspin,
            "member_bridge",
            "Living Room (Sendspin)",
            role=ProtocolRole.AUDIO,
            player=member,
        )
        _refresh(controller)

        await controller._handle_set_members(leader, [member.player_id])
        joined_protocol = member.active_output_protocol
        # the leader reports its group, as a real provider does
        _refresh(controller)

        await controller._handle_set_members(leader, None, [member.player_id])
        assert (joined_protocol, member.active_output_protocol) == ("member_bridge", None)

        member_native._attr_playback_state = PlaybackState.PLAYING
        member_native._attr_active_source = "Amazon Music"
        member.update_state(signal_event=False)

        assert member.playback_state == PlaybackState.PLAYING
        assert member.active_source == "Amazon Music"


class TestNativeProtocolPlayerGrouping:
    """Tests for grouping with a player whose own protocol is an audio domain (e.g. Apple TV)."""

    def test_native_airplay_groups_with_protocol_linked_player(self) -> None:
        """
        Test grouping a native AirPlay player (Apple TV) with a player that has AirPlay (Sonos).

        This tests the scenario where:
        - the Apple TV's own protocol is AirPlay
        - the Sonos has AirPlay as an audio protocol
        - Apple TV groups with Sonos via the common AirPlay protocol
        """
        controller, mass, _ = _controller()
        airplay = MockProvider("airplay", instance_id="airplay", mass=mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        # Apple TV: native AirPlay (supports grouping via AirPlay)
        apple_tv, apple_tv_native = attach_mock(airplay, "apple_tv_1", "Apple TV Bedroom")
        apple_tv_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        apple_tv_native._attr_can_group_with = {"airplay"}  # Provider instance ID
        sonos_player, sonos_native = attach_mock(sonos, "sonos_bathroom", "Bathroom")
        sonos_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        sonos_airplay = _attach_audio(airplay, "airplay_sonos", sonos_player, "Bathroom (AirPlay)")
        sonos_airplay._attr_can_group_with = {"airplay"}
        _refresh(controller)

        # Translate members for grouping Sonos to Apple TV
        protocol_members, _native_members, protocol, protocol_domain = _grouping(
            controller, apple_tv, [sonos_player.player_id], group_protocol=None
        )

        # Should find common AirPlay protocol
        assert protocol_members == ["airplay_sonos"]
        assert protocol_domain == "airplay"
        # For a native AirPlay player, the protocol is the Apple TV's own
        assert protocol is apple_tv_native

    def test_get_output_protocol_by_domain_finds_native(self) -> None:
        """Test that get_output_protocol_by_domain finds native protocol."""
        controller, mass, _ = _controller()
        airplay = MockProvider("airplay", instance_id="airplay", mass=mass)
        # Native AirPlay player
        apple_tv, apple_tv_native = attach_mock(airplay, "apple_tv_1", "Apple TV")
        apple_tv_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _refresh(controller)

        # Should find native AirPlay protocol
        output = apple_tv.get_output_protocol_by_domain("airplay")
        assert output is not None
        assert output.output_protocol_id == "native"
        assert output.protocol_domain == "airplay"
        assert output.is_native is True


class TestUngroupTranslation:
    """Tests for translation when ungrouping from a player whose own protocol is AirPlay."""

    def test_ungroup_translates_visible_to_protocol_id(self) -> None:
        """
        Test that ungrouping correctly translates the player id to a protocol id.

        When ungrouping Sonos from Apple TV, the Sonos player id should be translated to
        the id of its AirPlay protocol for the removal.
        """
        controller, mass, _ = _controller()
        airplay = MockProvider("airplay", instance_id="airplay", mass=mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        # Apple TV with Sonos's AirPlay protocol in group_members
        apple_tv, apple_tv_native = attach_mock(airplay, "apple_tv_1", "Apple TV")
        apple_tv_native._attr_supported_features |= {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
        }
        apple_tv_native._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        sonos_player, sonos_native = attach_mock(sonos, "sonos_1", "Sonos")
        sonos_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        attach_mock(
            airplay,
            "airplay_sonos",
            "Sonos (AirPlay)",
            role=ProtocolRole.AUDIO,
            player=sonos_player,
        )
        _refresh(controller)

        # Translate members for removal - the player id should become the protocol id
        _protocol_members, native_members = _grouping(
            controller, apple_tv, (), group_protocol=None, remove=[sonos_player.player_id]
        )[:2]

        # Should translate to the protocol id for native removal
        assert native_members == ["airplay_sonos"]


class TestNativeProtocolDomainPlayerGrouping:
    """
    Tests for grouping with players whose own protocol is of an audio protocol domain.

    This tests the scenario where a player's own protocol IS of the protocol domain (e.g. a
    sendspin web player whose own protocol is a native sendspin one), rather than having
    that protocol as an audio protocol.
    """

    @staticmethod
    def _office_and_web_player(
        mass: MagicMock,
    ) -> tuple[Player, MockProtocol, Player]:
        """
        Return an office player with a Sendspin protocol, its Sendspin protocol and a web player.

        :param mass: The mocked server.
        """
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mass)
        # Office: native Sonos player with Sendspin as audio protocol
        office, office_native = attach_mock(sonos, "sonos_office", "Office")
        office_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sendspin_office = _attach_audio(sendspin, "sendspin_office", office, "Office (Sendspin)")
        sendspin_office._attr_can_group_with = {"sendspin"}
        # Web player: its own protocol is a native sendspin one
        web_player, web_native = attach_mock(sendspin, "sendspin_web", "Web (Chrome on Mac)")
        web_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        return office, sendspin_office, web_player

    def test_native_protocol_player_groups_via_active_protocol(self) -> None:
        """
        Test grouping a native protocol-domain player via the parent's active protocol.

        Scenario: Office (has a sendspin audio protocol, active) groups with a Web player
        (native sendspin). This should use Priority 2 (parent's active protocol).
        """
        controller, mass, _ = _controller()
        office, sendspin_office, web_player = self._office_and_web_player(mass)
        _refresh(controller)

        # Group Web player with Office, with sendspin as active protocol
        protocol_members, native_members, protocol, _ = _grouping(
            controller, office, [web_player.player_id], group_protocol=sendspin_office
        )

        # Web player's own protocol id should be in protocol_members
        assert protocol_members == ["sendspin_web"]
        assert len(native_members) == 0
        assert protocol is sendspin_office

    def test_native_protocol_player_groups_via_common_protocol(self) -> None:
        """
        Test grouping a native protocol-domain player via common protocol search (Priority 4).

        Same scenario but without a pre-set active protocol — the common protocol
        search should find sendspin as the shared protocol.
        """
        controller, mass, _ = _controller()
        office, sendspin_office, web_player = self._office_and_web_player(mass)
        _refresh(controller)

        # Group Web player with Office, without pre-set active protocol
        protocol_members, native_members, protocol, protocol_domain = _grouping(
            controller, office, [web_player.player_id], group_protocol=None
        )

        # Should find common sendspin protocol via Priority 4
        assert protocol_members == ["sendspin_web"]
        assert len(native_members) == 0
        assert protocol_domain == "sendspin"
        assert protocol is sendspin_office

    def test_ungroup_native_protocol_player(self) -> None:
        """
        Test ungrouping a native protocol-domain player from a parent's protocol group.

        When ungrouping a native sendspin web player from Office's sendspin group, the id of
        the web player's own protocol should be used for removal.
        """
        controller, mass, _ = _controller()
        office, sendspin_office, web_player = self._office_and_web_player(mass)
        sendspin_office._attr_group_members = ["sendspin_office", "sendspin_web"]
        _refresh(controller)

        # Translate removal — the web player's own protocol id should be used
        protocol_members, native_members = _grouping(
            controller, office, (), group_protocol=sendspin_office, remove=[web_player.player_id]
        )[:2]

        # Web player's protocol id should be in the protocol removal list
        assert protocol_members == ["sendspin_web"]
        assert len(native_members) == 0

    def test_filter_protocol_members_accepts_native_protocol_player(self) -> None:
        """
        Test that a protocol group accepts the own protocol of a protocol-domain player.

        A native protocol with a matching provider domain should pass through the
        domain filter of the forward, not just audio protocols.
        """
        controller, mass, _ = _controller()
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mass)
        # the parent's sendspin audio protocol
        _, sendspin_parent = attach_mock(
            sendspin, "sendspin_parent", "Parent (Sendspin)", role=ProtocolRole.AUDIO
        )
        sendspin_parent._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # native sendspin web player
        _, web_native = attach_mock(sendspin, "sendspin_web", "Web (Chrome on Mac)")
        web_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        # another sendspin audio protocol
        attach_mock(sendspin, "sendspin_other", "Other (Sendspin)", role=ProtocolRole.AUDIO)
        _refresh(controller)

        parent = controller.get_player_by_protocol("sendspin_parent")
        web = controller.get_player_by_protocol("sendspin_web")
        other = controller.get_player_by_protocol("sendspin_other")
        assert parent is not None
        assert web is not None
        assert other is not None

        # Both native and audio protocols should pass through the filter
        filtered, _, _, _ = _grouping(
            controller, parent, [web.player_id, other.player_id], group_protocol=sendspin_parent
        )

        assert "sendspin_web" in filtered
        assert "sendspin_other" in filtered
        assert len(filtered) == 2

    def test_sendspin_visualizer_groups_via_common_protocol(self) -> None:
        """
        Test grouping a sendspin visualizer (e.g. Hue light) with a sendspin-bridged parent.

        Visualizer players have no PLAY_MEDIA so they are not native players, but the
        player advertises a self-referential sendspin output protocol on them
        (is_native=True) so the common-protocol search (Priority 4) can match them against
        any parent that has a sendspin bridge attached.
        """
        controller, mass, _ = _controller()
        sonos = MockProvider("sonos", instance_id="sonos", mass=mass)
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mass)
        # Mancave: native Sonos parent with a sendspin bridge attached
        mancave, mancave_native = attach_mock(sonos, "sonos_mancave", "Mancave")
        mancave_native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sendspin_bridge = _attach_audio(sendspin, "spb_mancave", mancave, "Mancave (AirPlay)")
        sendspin_bridge._attr_can_group_with = {"sendspin"}
        # Hue visualizer: sendspin provider, no PLAY_MEDIA, has SET_MEMBERS, type LIGHT
        hue_light, hue_native = attach_mock(sendspin, "hue-mancave", "Hue: Mancave")
        hue_native._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        _refresh(controller)

        protocol_members, native_members, protocol, protocol_domain = _grouping(
            controller, mancave, [hue_light.player_id], group_protocol=None
        )

        # Priority 4 should match sendspin↔sendspin and route via the bridge,
        # using the visualizer's own protocol id directly (is_native=True).
        assert protocol_members == ["hue-mancave"]
        assert native_members == []
        assert protocol_domain == "sendspin"
        assert protocol is sendspin_bridge


class TestPreferNativeGrouping:
    """A player that prefers native grouping uses it before its preferred output protocol."""

    @staticmethod
    def _parent_and_child(
        mass: MagicMock,
        parent_class: type[MockProtocol],
        child_class: type[MockProtocol],
        child_domain: str = "wiim",
        with_airplay: bool = False,
    ) -> tuple[Player, Player]:
        """
        Return a parent and a child, of one provider instance unless another domain is given.

        :param mass: The mocked server.
        :param parent_class: The protocol class of the parent's own protocol.
        :param child_class: The protocol class of the child's own protocol.
        :param child_domain: The provider domain of the child's own protocol.
        :param with_airplay: Let both also group over AirPlay.
        """
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mass)
        child_provider = (
            provider
            if child_domain == "wiim"
            else MockProvider(child_domain, instance_id=f"{child_domain}_instance", mass=mass)
        )
        parent, _ = attach_mock(provider, "shell_parent", "Parent", protocol_class=parent_class)
        child, _ = attach_mock(child_provider, "shell_child", "Child", protocol_class=child_class)
        if with_airplay:
            airplay = MockProvider("airplay", instance_id="airplay_instance", mass=mass)
            _attach_audio(airplay, "airplay_parent", parent)
            _attach_audio(airplay, "airplay_child", child)
        _refresh(mass.players)
        return parent, child

    def test_prefer_native_uses_native_before_child_preferred(self) -> None:
        """A prefer-native child groups natively even with an explicit protocol preference."""
        controller, mass, preferred = _controller()
        parent, child = self._parent_and_child(
            mass, MockProtocol, _NativeGroupingProtocol, with_airplay=True
        )
        # an explicit child output-protocol preference that would otherwise win
        preferred[child.player_id] = "airplay_child"
        # the parent groups natively: its own protocol says so, as the selector was told
        assert parent.own_protocol is not None
        parent.own_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_members, native_members, _, _ = _grouping(controller, parent, [child.player_id])

        assert native_members == ["shell_child"]
        assert protocol_members == []

    def test_prefer_native_falls_through_same_instance_not_native_peer(self) -> None:
        """A same-instance pair the parent rejects as a native peer is not forced native."""
        controller, mass, _ = _controller()
        # official + generic LinkPlay share one wiim provider instance, but a cross-backend
        # peer is not native-group compatible, so the seam must not force native grouping.
        parent, child = self._parent_and_child(
            mass, _RejectsNativeGroupingProtocol, _NativeGroupingProtocol
        )
        # the parent groups natively: its own protocol says so, as the selector was told
        assert parent.own_protocol is not None
        parent.own_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        _protocol_members, native_members, _, _ = _grouping(controller, parent, [child.player_id])

        assert native_members == []

    def test_prefer_native_falls_through_when_incompatible(self) -> None:
        """When native grouping is not possible (cross-backend), the seam does not force it."""
        controller, mass, _ = _controller()
        parent, child = self._parent_and_child(
            mass, MockProtocol, _NativeGroupingProtocol, child_domain="other"
        )
        assert parent.own_protocol is not None
        parent.own_protocol._attr_can_group_with = set()  # does not accept the cross-backend child
        # the parent groups natively: its own protocol says so, as the selector was told
        assert parent.own_protocol is not None
        parent.own_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        _protocol_members, native_members, _, _ = _grouping(controller, parent, [child.player_id])

        # the preferred-native seam did not force a cross-backend native group
        assert native_members == []

    def test_default_player_keeps_protocol_first_ordering(self) -> None:
        """An ordinary player (no native preference) is unaffected by the seam."""
        controller, mass, _ = _controller()
        parent, child = self._parent_and_child(mass, MockProtocol, MockProtocol)
        assert child.own_protocol is not None
        assert child.own_protocol.prefer_native_grouping is False
        # the parent groups natively: its own protocol says so, as the selector was told
        assert parent.own_protocol is not None
        parent.own_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        _protocol_members, native_members, _, _ = _grouping(controller, parent, [child.player_id])

        # with no preferred/active/common protocol, the default player still reaches
        # native grouping via the normal priority (Priority 3), not the preferred seam
        assert native_members == ["shell_child"]
