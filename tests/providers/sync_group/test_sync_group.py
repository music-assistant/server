"""Tests for the Sync Group Player protocol: protocol awareness, member handling and locking."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    ResourceBusyError,
)
from music_assistant_models.player import PlayerMedia, PlayerSource
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_AUTO_PLAY,
    CONF_GROUP_MEMBERS,
    CONF_MUTE_CONTROL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import locks as players_locks
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.providers.sync_group.constants import REFORM_DEBOUNCE_SECONDS
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from tests.common import (
    MockProtocol,
    MockProvider,
    attach_mock,
    make_player_state,
    mock_player_mass,
    record_lock_waits,
)

# the lock budget of the tests that wait for a real player lock, in seconds
LOCK_BUDGET = 0.25

# what a member that can be (sync) grouped reports
MEMBER_FEATURES = frozenset({PlayerFeature.PLAY_MEDIA, PlayerFeature.SET_MEMBERS})


class _SessionProtocol(MockProtocol):
    """A mock protocol of a member, whose live session can differ from its group members."""

    # the protocol ids its live session feeds, None when that is its group members
    live_session: list[str] | None = None

    @property
    def live_session_members(self) -> list[str]:
        """Return the ids of the protocols that take part in the live session."""
        return self.group_members if self.live_session is None else self.live_session


def _noop_context() -> AsyncMock:
    """Return an async context manager that does nothing and lets exceptions through."""
    ctx = AsyncMock()
    ctx.__aenter__.return_value = None
    # an unconfigured AsyncMock returns a truthy MagicMock and would swallow exceptions
    ctx.__aexit__.return_value = False
    return ctx


def _busy_lock() -> MagicMock:
    """Build a get_player_lock mock whose lock stays taken for the whole wait."""
    ctx = AsyncMock()
    ctx.__aenter__.side_effect = ResourceBusyError("still busy")
    ctx.__aexit__.return_value = False
    return MagicMock(return_value=ctx)


def _make_mock_mass() -> MagicMock:
    """Create a mocked MusicAssistant whose players controller looks players up for real."""
    mass = mock_player_mass()
    players = mass.players
    players._handle_cmd_stop = AsyncMock()
    players._handle_cmd_resume = AsyncMock()
    players._handle_play_media = AsyncMock()
    players.cmd_set_members = AsyncMock()
    players._handle_set_members = AsyncMock()
    players.get_player_lock = MagicMock(return_value=_noop_context())
    # a member's lock is free unless a test says otherwise
    free = _noop_context()
    free.__aenter__.return_value = True
    players.get_player_lock_if_free = MagicMock(return_value=free)
    players.wait_for_player_update = MagicMock(return_value=_noop_context())
    players.trigger_player_update = MagicMock()
    # the real routing: through the sync leader, or from the member's own side
    players._detach_member = partial(PlayerController._detach_member, players)
    mass.call_later = MagicMock()
    mass.cancel_timer = MagicMock()
    mass.test_providers = {}
    return mass


def _provider(mass: MagicMock, domain: str) -> MockProvider:
    """Return the (mock) provider of a domain on the given mass."""
    if domain not in mass.test_providers:
        mass.test_providers[domain] = MockProvider(domain, instance_id=domain, mass=mass)
    provider: MockProvider = mass.test_providers[domain]
    return provider


def _make_member(
    mass: MagicMock,
    player_id: str,
    native: str | None = "sonos",
    *,
    audio: Iterable[str] = (),
    offline: Iterable[str] = (),
    active_output: str | None = None,
    playback_state: PlaybackState = PlaybackState.IDLE,
    player_type: PlayerType = PlayerType.PLAYER,
    available: bool = True,
) -> Player:
    """
    Create a player that can be a member of a sync group, with its protocols attached.

    Each protocol has the id ``<domain>_<player_id>``, so protocol ids and player ids differ.

    :param native: Domain of its native protocol, None for a device with only audio protocols.
    :param audio: Domains of its audio protocols.
    :param offline: Domains of the audio protocols that can not be reached right now.
    :param active_output: Domain of the output the player plays on.
    :param available: Whether its device can be reached at all.
    """
    player: Player | None = None
    if native:
        player, _ = attach_mock(
            _provider(mass, native),
            f"{native}_{player_id}",
            player_id,
            player_id=player_id,
            player_type=player_type,
            protocol_class=_SessionProtocol,
        )
    for domain in audio:
        player, protocol = attach_mock(
            _provider(mass, domain),
            f"{domain}_{player_id}",
            player_id,
            role=ProtocolRole.AUDIO,
            player=player,
            player_id=player_id,
            player_type=player_type,
            protocol_class=_SessionProtocol,
        )
        protocol._attr_available = domain not in offline
    assert player is not None
    for attached in player.attached_protocols.values():
        attached._attr_supported_features = set(MEMBER_FEATURES)
        if not available:
            attached._attr_available = False
    if active_output:
        player._active_output_protocol = (
            "native" if active_output == native else f"{active_output}_{player_id}"
        )
    player._cache.clear()
    player._state = make_player_state(
        player_id,
        type=player_type,
        available=available,
        playback_state=playback_state,
        supported_features=set(MEMBER_FEATURES),
    )
    return player


def _protocol(player: Player, domain: str) -> _SessionProtocol:
    """Return the protocol of a member of the given domain."""
    protocol = player.attached_protocols[f"{domain}_{player.player_id}"]
    assert isinstance(protocol, _SessionProtocol)
    return protocol


def _member_changes(protocol: PlayerProtocol) -> AsyncMock:
    """Return the mock that replaced the set_members of a protocol."""
    assert isinstance(protocol.set_members, AsyncMock)
    return protocol.set_members


def _set_reachable(player: Player, reachable: bool) -> None:
    """Let every protocol of a member report whether its device can be reached."""
    for protocol in player.attached_protocols.values():
        protocol._attr_available = reachable


def _forget(mass: MagicMock, player_id: str) -> None:
    """Take a player out of the registry, as if it was removed."""
    player = mass.players._players.pop(player_id)
    for protocol_id in player.attached_protocols:
        mass.players._protocols.pop(protocol_id, None)


def _make_sync_group(
    mass: MagicMock,
    protocol_id: str = "syncgroup_test",
    *,
    dynamic: bool = True,
    members: list[str] | None = None,
    allowed_members: list[str] | None = None,
) -> SyncGroupPlayer:
    """
    Create a sync group, attached to a player whose id differs from the protocol id.

    :param dynamic: Whether the group allows changing its members.
    :param members: The configured members.
    :param allowed_members: The configured allow-list of members.
    """
    provider = _provider(mass, "sync_group")
    values: dict[str, Any] = {
        "dynamic_members": dynamic,
        "group_members": list(members or []),
        "allowed_members": list(allowed_members or []),
    }
    mass.config.get_base_protocol_config.return_value = MagicMock(
        default_name="Test Group",
        get_value=lambda key, default=None: values.get(key, default),
    )
    sgp = SyncGroupPlayer(provider, protocol_id)  # type: ignore[arg-type]
    player_id = f"pl_{protocol_id}"
    player = Player(
        mass,
        player_id,
        PlayerConfig(
            values={},
            provider=provider.instance_id,
            player_id=player_id,
            default_name="Test Group",
            player_type=PlayerType.GROUP,
        ),
    )
    mass.players._players[player_id] = player
    mass.players._protocols[protocol_id] = sgp
    player.add_protocol(sgp, ProtocolRole.VIRTUAL)
    sgp.set_player(player)
    player.set_initialized()
    sgp._cache.clear()
    return sgp


def _group_id(sgp: SyncGroupPlayer) -> str:
    """Return the id of the player of a group."""
    assert sgp.player is not None
    return sgp.player.player_id


def _make_play_media_takeover_case(
    token: int,
) -> tuple[MagicMock, SyncGroupPlayer, MagicMock]:
    """Create the shared SyncGroup playback takeover setup."""
    mass = _make_mock_mass()
    sgp = _make_sync_group(mass)
    leader = _make_member(mass, "leader", "sendspin")
    sgp.sync_leader = leader
    sgp._attr_group_members = ["leader"]
    owner = MagicMock()
    owner.on_group_content_takeover_aborted = AsyncMock()
    owner.on_group_content_takeover_finished = AsyncMock()
    sgp._form_syncgroup = AsyncMock(return_value=(owner, token))  # type: ignore[method-assign]
    return mass, sgp, owner


def test_the_group_is_a_virtual_protocol() -> None:
    """The group attaches as the virtual protocol of its player."""
    mass = _make_mock_mass()
    sgp = _make_sync_group(mass)

    assert sgp.role == ProtocolRole.VIRTUAL
    assert sgp.type == PlayerType.GROUP
    assert sgp.player is not None
    assert sgp.player.own_protocol is sgp


class TestProtocolAwareLeaderSelection:
    """Test that leader selection prefers protocol continuity."""

    def test_select_leader_prefers_active_protocol(self) -> None:
        """When preferred protocol is given, prefer members supporting it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        _make_member(mass, "player_a")
        player_b = _make_member(mass, "player_b", audio=["airplay"])
        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader(preferred_protocol_domain="airplay")
        assert leader is player_b

    def test_select_leader_fallback_when_no_protocol_match(self) -> None:
        """When no member supports the preferred protocol, fall back to first available."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player_a = _make_member(mass, "player_a")
        sgp._attr_group_members = ["player_a"]

        leader = sgp._select_sync_leader(preferred_protocol_domain="airplay")
        assert leader is player_a

    def test_select_leader_prefers_live_session_member(self) -> None:
        """A member the live session already feeds outranks a plain protocol match."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # both speak AirPlay, but only player_b takes part in the live session
        _make_member(mass, "player_a", audio=["airplay"])
        player_b = _make_member(mass, "player_b", audio=["airplay"])
        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader(
            preferred_protocol_domain="airplay", preferred_member_ids=["player_b"]
        )
        assert leader is player_b

    def test_select_leader_no_protocol_uses_first_available(self) -> None:
        """When no preferred protocol, pick first available."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player_a = _make_member(mass, "player_a")
        _make_member(mass, "player_b")
        sgp._attr_group_members = ["player_a", "player_b"]

        leader = sgp._select_sync_leader()
        assert leader is player_a

    def test_select_leader_skips_non_playback_member(self) -> None:
        """A light member listed first is never picked as sync leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        _make_member(mass, "light", "sendspin", player_type=PlayerType.LIGHT)
        player_b = _make_member(mass, "player_b")
        sgp._attr_group_members = ["light", "player_b"]

        leader = sgp._select_sync_leader()
        assert leader is player_b

    def test_select_leader_skips_member_switched_off_at_its_power_control(self) -> None:
        """A member reported available while it is switched off is never picked as leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        switched_off = _make_member(mass, "switched_off")
        # clients still see it as available (and off), the device itself is offline
        _set_reachable(switched_off, False)
        assert switched_off.state.available
        player_b = _make_member(mass, "player_b")
        sgp._attr_group_members = ["switched_off", "player_b"]

        leader = sgp._select_sync_leader()
        assert leader is player_b


class TestMemberSupportsProtocol:
    """Test protocol domain checking for members."""

    def test_native_provider_match(self) -> None:
        """Player's native protocol domain matches."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player = _make_member(mass, "p1", "sonos")
        assert sgp._member_supports_protocol_domain(player, "sonos") is True

    def test_audio_protocol_match(self) -> None:
        """Player has an audio protocol of the domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player = _make_member(mass, "p1", audio=["airplay"])
        assert sgp._member_supports_protocol_domain(player, "airplay") is True

    def test_device_with_only_an_audio_protocol_matches_its_domain(self) -> None:
        """A device without a native protocol still plays on the domain of its audio protocol."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player = _make_member(mass, "p1", None, audio=["airplay"])
        assert sgp._member_supports_protocol_domain(player, "airplay") is True

    def test_offline_audio_protocol_does_not_match(self) -> None:
        """An audio protocol that can not be reached is no way to play on its domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player = _make_member(mass, "p1", audio=["airplay"], offline=["airplay"])
        assert sgp._member_supports_protocol_domain(player, "airplay") is False

    def test_no_match(self) -> None:
        """Player doesn't support the requested protocol."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        player = _make_member(mass, "p1", "sonos")
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
        sgp.sync_leader = _make_member(mass, "leader", "sonos")
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "sonos"

    def test_active_protocol_with_requiring_member(self) -> None:
        """Non-native protocol stays active while a member still requires it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sonos", audio=["airplay"], active_output="airplay")
        # AirPlay-only member
        _make_member(mass, "ap_only", None, audio=["airplay"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "ap_only"]
        assert sgp.active_protocol_domain == "airplay"

    def test_active_protocol_downshifts_when_no_member_requires_it(self) -> None:
        """Non-native protocol downshifts to native when no member still requires it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sonos", audio=["airplay"], active_output="airplay")
        sgp.sync_leader = leader
        # Only a native-capable Sonos leader remains; no one requires airplay
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "sonos"

    def test_bridged_airplay_member_keeps_the_protocol(self) -> None:
        """An AirPlay device holds the group on AirPlay despite its Sendspin bridge."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sonos", audio=["airplay"], active_output="airplay")
        # an Apple TV plays AirPlay and carries a Sendspin bridge on top,
        # so its playback paths are airplay + sendspin - neither reaches sonos
        _make_member(mass, "apple_tv", None, audio=["airplay", "sendspin"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "apple_tv"]
        assert sgp.active_protocol_domain == "airplay"

    def test_offline_protocol_does_not_count_as_a_playback_path(self) -> None:
        """A member's unreachable protocol no longer offers the group a way to it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # an Apple TV leads on airplay, with the session on its Sendspin bridge
        leader = _make_member(
            mass, "leader", None, audio=["airplay", "sendspin"], active_output="sendspin"
        )
        # the speaker still answers over its Sendspin bridge, but its AirPlay endpoint
        # dropped off - so airplay is no longer a way to reach it and the group has to
        # stay on sendspin rather than downshift to the leader's airplay
        _make_member(mass, "member", None, audio=["airplay", "sendspin"], offline=["airplay"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]
        assert sgp.active_protocol_domain == "sendspin"

    def test_member_from_another_provider_keeps_the_protocol(self) -> None:
        """A member of a different provider keeps the shared protocol active."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sonos", audio=["sendspin"], active_output="sendspin")
        # a Chromecast can only meet the Sonos leader on the shared sendspin bridge
        _make_member(mass, "chromecast", "chromecast", audio=["sendspin"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "chromecast"]
        assert sgp.active_protocol_domain == "sendspin"

    def test_leader_without_native_protocol_stays_on_its_preferred_protocol(self) -> None:
        """A leader with only audio protocols keeps the session on the one it plays on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(
            mass, "leader", None, audio=["airplay", "sendspin"], active_output="airplay"
        )
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        assert sgp.active_protocol_domain == "airplay"

    def test_unreachable_members_do_not_block_the_downshift(self) -> None:
        """Members that are offline or have no playback path are ignored."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sonos", audio=["airplay"], active_output="airplay")
        _make_member(mass, "offline", None, audio=["airplay"], available=False)
        # a device whose every protocol dropped off has nothing left to play on
        _make_member(mass, "no_paths", None, audio=["airplay"], offline=["airplay"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "offline", "no_paths", "gone"]
        assert sgp.active_protocol_domain == "sonos"

    def test_dissolve_clears_sync_leader(self) -> None:
        """Verify _dissolve_syncgroup clears the sync leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        source = inspect.getsource(sgp._dissolve_syncgroup)
        assert "self.sync_leader = None" in source

    async def test_dissolve_schedules_protocol_clear_while_leader_still_playing(self) -> None:
        """
        Dissolve must schedule the delayed protocol clear while the leader plays.

        Real devices report new state with a delay after a stop, so a playback
        state check here would skip the clear and leave a stale active protocol
        on the leader.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(
            mass,
            "leader",
            "wiim",
            audio=["airplay"],
            active_output="airplay",
            playback_state=PlaybackState.PLAYING,
        )
        sgp.sync_leader = leader

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        # use getattr to defeat mypy's narrowing after the earlier assignment,
        # since it can't see that _dissolve_syncgroup mutates sync_leader.
        assert getattr(sgp, "sync_leader") is None  # noqa: B009
        mass.players.schedule_active_output_protocol_clear.assert_called_once_with(leader)

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
        living_room = _make_member(mass, "living_room", "sendspin")
        bathroom = _make_member(mass, "bathroom", "sendspin")
        _make_member(mass, "kitchen", "sendspin")
        bathroom.state.group_members = UniqueList(["bathroom", "living_room", "kitchen"])
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

    async def test_dissolve_keeps_using_the_sync_leader_when_it_holds_the_group(self) -> None:
        """A leader that still holds its members must stay the dissolve target."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sendspin")
        _make_member(mass, "member", "sendspin")
        leader.state.group_members = UniqueList(["leader", "member"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_remove=["member"]
        )
        mass.players._handle_cmd_stop.assert_not_awaited()

    async def test_dissolve_ignores_a_member_leading_an_unrelated_group(self) -> None:
        """A member grouped outside of MA must never be adopted as the dissolve target."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        living_room = _make_member(mass, "living_room", "sendspin")
        member = _make_member(mass, "member", "sendspin")
        # member leads a group that shares no player with us
        member.state.group_members = UniqueList(["member", "outsider"])
        sgp.sync_leader = living_room
        sgp._attr_group_members = ["living_room", "member"]

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_not_awaited()
        mass.players._handle_cmd_stop.assert_not_awaited()

    async def test_dissolve_refreshes_members_after_the_session_ends(self) -> None:
        """
        Dissolve must trigger a state update for every member once the leader is cleared.

        Members derive active_group from is_active_session. When the idle grace dissolves
        the group after the queue ran out, the group's own state does not change, so
        without an explicit trigger the members keep reporting the group as active.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "squeezelite")
        _make_member(mass, "member", "squeezelite")
        leader.state.group_members = UniqueList(["leader", "member"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]
        leader_at_trigger: list[object] = []
        mass.players.trigger_player_update.side_effect = lambda _pid: leader_at_trigger.append(
            sgp.sync_leader
        )

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        triggered = [c.args[0] for c in mass.players.trigger_player_update.call_args_list]
        assert sorted(triggered) == ["leader", "member"]
        # the members must recalculate against the ended session, not the old leader
        assert leader_at_trigger == [None, None]

    async def test_dissolve_completes_when_leader_cannot_be_reached(self) -> None:
        """Failing ungroup and stop commands on the leader never abort the dissolve."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "wiim")
        _make_member(mass, "m2", "wiim")
        # the members ended up on a provider-promoted leader, which we stop as well
        promoted = _make_member(mass, "promoted", "wiim")
        promoted.state.group_members = UniqueList(["promoted", "m2"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2"]
        mass.players._handle_set_members.side_effect = RuntimeError("Not connected")
        mass.players._handle_cmd_stop.side_effect = RuntimeError("Not connected")

        with (
            patch.object(sgp, "update_state") as update_state,
            patch.object(sgp, "_protocol_group_leader", return_value=promoted),
        ):
            await sgp._dissolve_syncgroup()

        mass.players._handle_set_members.assert_awaited_once()
        mass.players._handle_cmd_stop.assert_awaited_once_with("promoted")
        assert getattr(sgp, "sync_leader") is None  # noqa: B009
        update_state.assert_called()
        mass.players.schedule_active_output_protocol_clear.assert_any_call(promoted)
        mass.players.schedule_active_output_protocol_clear.assert_any_call(leader)
        mass.players.trigger_player_update.assert_any_call("m2")

    async def test_a_dissolve_holds_the_leader_and_the_player_holding_its_members(
        self,
    ) -> None:
        """Both players whose group changes are locked before the members are released."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        living_room = _make_member(mass, "living_room", "sendspin")
        bathroom = _make_member(mass, "bathroom", "sendspin")
        _make_member(mass, "kitchen", "sendspin")
        bathroom.state.group_members = UniqueList(["bathroom", "living_room", "kitchen"])
        sgp.sync_leader = living_room
        sgp._attr_group_members = ["living_room", "bathroom", "kitchen"]
        events: list[str] = []
        mass.players.get_player_lock = _recording_lock(events)
        mass.players._handle_set_members.side_effect = lambda *_args, **_kwargs: events.append(
            "ungroup"
        )

        with patch.object(sgp, "update_state"):
            await sgp._dissolve_syncgroup()

        assert events == [
            "lock:living_room",
            "lock:bathroom",
            "ungroup",
            "unlock:bathroom",
            "unlock:living_room",
        ]

    async def test_a_busy_leader_leaves_the_group_and_its_session_as_they_were(self) -> None:
        """A dissolve that cannot get the leader's lock fails and changes nothing."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "sendspin")
        _make_member(mass, "member", "sendspin")
        leader.state.group_members = UniqueList(["leader", "member"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]
        mass.players.get_player_lock = _busy_lock()

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_cancel_idle_grace_timer") as cancel_idle_grace,
            patch.object(sgp, "_cancel_reform_timer") as cancel_reform,
            pytest.raises(ResourceBusyError),
        ):
            await sgp._dissolve_syncgroup()

        assert sgp.sync_leader is leader
        mass.players._handle_set_members.assert_not_awaited()
        cancel_idle_grace.assert_not_called()
        cancel_reform.assert_not_called()
        mass.players.schedule_active_output_protocol_clear.assert_not_called()


class TestDynamicLeaderSwitch:
    """Test dynamic leader switching behaviour."""

    async def test_dynamic_leader_switch_hands_off_to_new_leader(self) -> None:
        """When the new leader IS in the live session, seamless protocol-level handoff is used."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # an AirPlay-only member keeps the group on AirPlay, so the new leader must
        # be a member that supports AirPlay
        ap_only = _make_member(mass, "ap_only", None, audio=["airplay"])
        old_leader = _make_member(
            mass, "old_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        new_leader = _make_member(
            mass, "new_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        session = _protocol(old_leader, "airplay")
        # the live session holds the AirPlay protocols of the others, so they can take over
        session._attr_group_members = [
            "airplay_old_leader",
            "airplay_new_leader",
            "airplay_ap_only",
        ]
        for protocol in (session, _protocol(new_leader, "airplay"), _protocol(ap_only, "airplay")):
            protocol.set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "new_leader", "ap_only"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader is new_leader
        assert "old_leader" not in sgp._attr_group_members
        # 1. the session protocol of the old leader steps out (self-remove)
        _member_changes(session).assert_awaited_once_with(
            player_ids_to_remove=["airplay_old_leader"]
        )
        # 2. the protocol of the new leader takes over the remaining members, by protocol id
        _member_changes(_protocol(new_leader, "airplay")).assert_awaited_once_with(
            player_ids_to_add=["airplay_ap_only"]
        )

    async def test_dynamic_leader_switch_dissolves_when_new_leader_not_in_session(
        self,
    ) -> None:
        """When the new leader is NOT in the live session, fall back to dissolve+reform."""
        mass = _make_mock_mass()
        mass.players.cmd_resume = AsyncMock()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(
            mass, "old_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        # Freshly-added player — NOT in the live session
        _make_member(mass, "fresh_player", "sonos", audio=["airplay"])
        # the old leader was streaming on its own, so its session holds nobody else
        session = _protocol(old_leader, "airplay")
        session.set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "fresh_player"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # The old protocol must NOT have been told to self-remove (that path is only
        # for the seamless handoff). Instead, dissolve+reform happened:
        # wait_for_player_update was used to wrap the stop.
        _member_changes(session).assert_not_awaited()
        mass.players.wait_for_player_update.assert_called()
        assert "old_leader" not in sgp._attr_group_members

    async def test_dynamic_leader_switch_follows_live_session_order(self) -> None:
        """
        The new leader must be the member that inherits the live session.

        Providers promote their own first remaining member, so a leader picked from a
        drifted member order would leave the group tracking a different player than the
        one actually holding the session.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(mass, "old_leader", "sendspin")
        living_room = _make_member(mass, "living_room", "sendspin")
        bathroom = _make_member(mass, "bathroom", "sendspin")
        # the live session hands over to bathroom, our own order says living_room.
        # state.group_members is deliberately left unordered: only what the protocol
        # reports carries the provider's member order.
        session = _protocol(old_leader, "sendspin")
        session._attr_group_members = [
            "sendspin_old_leader",
            "sendspin_bathroom",
            "sendspin_living_room",
        ]
        old_leader.state.group_members = UniqueList(["old_leader", "living_room", "bathroom"])
        for player in (old_leader, living_room, bathroom):
            _protocol(player, "sendspin").set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "living_room", "bathroom"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader is bathroom
        assert sgp._attr_group_members == ["bathroom", "living_room"]
        _member_changes(session).assert_any_await(player_ids_to_remove=["sendspin_old_leader"])
        _member_changes(_protocol(bathroom, "sendspin")).assert_any_await(
            player_ids_to_add=["sendspin_living_room"]
        )

    async def test_handoff_stays_on_live_protocol_after_downshift(self) -> None:
        """
        A pending downshift must not redirect the handoff to the native protocols.

        Once no member requires the non-native protocol anymore,
        active_protocol_domain reports the native domain while the stream is
        still carried by the audio protocols, so the handoff has to keep
        addressing the latter.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(
            mass, "old_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        kitchen = _make_member(mass, "kitchen", "sonos", audio=["airplay"], active_output="airplay")
        bathroom = _make_member(
            mass, "bathroom", "sonos", audio=["airplay"], active_output="airplay"
        )
        session = _protocol(old_leader, "airplay")
        session._attr_group_members = ["airplay_old_leader", "airplay_kitchen", "airplay_bathroom"]
        for player in (old_leader, kitchen, bathroom):
            for protocol in player.attached_protocols.values():
                protocol.set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "kitchen", "bathroom"]
        # all remaining members can play natively, so the group is due to downshift
        assert sgp.active_protocol_domain == "sonos"

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        assert sgp.sync_leader is kitchen
        _member_changes(session).assert_any_await(player_ids_to_remove=["airplay_old_leader"])
        _member_changes(_protocol(kitchen, "airplay")).assert_any_await(
            player_ids_to_add=["airplay_bathroom"]
        )
        # a native grouping command would regroup speakers that stream over AirPlay
        _member_changes(_protocol(kitchen, "sonos")).assert_not_awaited()
        _member_changes(_protocol(bathroom, "sonos")).assert_not_awaited()

    async def test_leader_selection_prefers_the_live_protocol_after_downshift(self) -> None:
        """The new leader comes from the live session, not from the downshifted domain."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(
            mass, "old_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        # streams over AirPlay but can also play natively, so it does not hold
        # the group on AirPlay and the group is due to downshift
        kitchen = _make_member(mass, "kitchen", "sonos", audio=["airplay"], active_output="airplay")
        # native-only member that never joined the live session
        _make_member(mass, "spare", "sonos")
        session = _protocol(old_leader, "airplay")
        # the session reports who takes part but no order, so our tracked order stands
        session.live_session = ["airplay_old_leader", "airplay_kitchen"]
        session.set_members = AsyncMock()  # type: ignore[method-assign]
        _protocol(kitchen, "airplay").set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        # spare comes first, so selecting on the downshifted domain would pick it
        sgp._attr_group_members = ["old_leader", "spare", "kitchen"]
        assert sgp.active_protocol_domain == "sonos"

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # picking the native-only spare would have cost a dissolve + reform
        assert sgp.sync_leader is kitchen
        _member_changes(session).assert_any_await(player_ids_to_remove=["airplay_old_leader"])

    async def test_reform_hint_keeps_the_protocol_a_member_depends_on(self) -> None:
        """The re-form hint names a protocol the remaining member can actually play on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(
            mass, "old_leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        # reachable over AirPlay and its Sendspin bridge, but never over sonos
        _make_member(mass, "apple_tv", None, audio=["airplay", "sendspin"])
        # the remaining member never joined the session, so no seamless handoff
        _protocol(old_leader, "airplay").live_session = ["airplay_old_leader"]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "apple_tv"]

        with patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform:
            await sgp._dynamic_leader_switch("old_leader")

        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("preferred_protocol_domain") == "airplay"

    async def test_leader_selection_skips_a_member_the_session_dropped(self) -> None:
        """
        A member the session dropped must not cost the group its seamless handoff.

        AirPlay drops a member from its session on a write timeout without pruning it
        from the group, so it stays an available candidate while no longer having a
        stream to inherit.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(mass, "ap_old", None, audio=["airplay"])
        _make_member(mass, "ap_dropped", None, audio=["airplay"])
        still_playing = _make_member(mass, "ap_live", None, audio=["airplay"])
        # the session dropped ap_dropped, but the group bookkeeping still lists it
        # (and lists it first, so it would be picked on member order alone)
        session = _protocol(old_leader, "airplay")
        session._attr_group_members = ["airplay_ap_old", "airplay_ap_dropped", "airplay_ap_live"]
        session.live_session = ["airplay_ap_old", "airplay_ap_live"]
        session.set_members = AsyncMock()  # type: ignore[method-assign]
        _protocol(still_playing, "airplay").set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["ap_old", "ap_dropped", "ap_live"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("ap_old")

        assert sgp.sync_leader is still_playing
        # handed off at the protocol level, so playback was never stopped;
        # the dropped member rejoins as a member of the new leader
        _member_changes(session).assert_any_await(player_ids_to_remove=["airplay_ap_old"])
        _member_changes(_protocol(still_playing, "airplay")).assert_any_await(
            player_ids_to_add=["airplay_ap_dropped"]
        )
        mass.players._handle_cmd_stop.assert_not_awaited()
        assert sgp._reform_task is None

    async def test_dissolves_when_no_remaining_member_is_in_the_session(self) -> None:
        """With every remaining member dropped from the session, the group must re-form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        old_leader = _make_member(mass, "ap_old", None, audio=["airplay"])
        _make_member(mass, "ap_a", None, audio=["airplay"])
        _make_member(mass, "ap_b", None, audio=["airplay"])
        session = _protocol(old_leader, "airplay")
        session._attr_group_members = ["airplay_ap_old", "airplay_ap_a", "airplay_ap_b"]
        session.live_session = ["airplay_ap_old"]
        session.set_members = AsyncMock()  # type: ignore[method-assign]
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["ap_old", "ap_a", "ap_b"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("ap_old")

        # no member has a stream to inherit: stop and re-form with the remaining two
        _member_changes(session).assert_not_awaited()
        mass.players._handle_cmd_stop.assert_awaited_with("ap_old")
        assert sgp._attr_group_members == ["ap_a", "ap_b"]
        assert sgp._reform_task is not None
        assert sgp.sync_leader is None

    @pytest.mark.parametrize(
        ("provider_domain", "live_members"),
        [
            # a solo Sendspin leader still lists itself as its group's only client
            ("sendspin", ["sendspin_old_leader"]),
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
        old_leader = _make_member(mass, "old_leader", provider_domain)
        session = _protocol(old_leader, provider_domain)
        session._attr_group_members = list(live_members)
        session.set_members = AsyncMock()  # type: ignore[method-assign]
        # added to the group in the same call that removed the leader, so it was
        # never synced at the protocol level
        _make_member(mass, "fresh_player", provider_domain)
        sgp.sync_leader = old_leader
        sgp._attr_group_members = ["old_leader", "fresh_player"]

        with patch.object(sgp, "update_state"):
            await sgp._dynamic_leader_switch("old_leader")

        # no protocol-level handoff was attempted; the group stopped and dissolved
        # with a re-form scheduled so playback resumes on the new member
        _member_changes(session).assert_not_awaited()
        mass.players._handle_cmd_stop.assert_awaited_with("old_leader")
        assert sgp._attr_group_members == ["fresh_player"]
        assert sgp._reform_task is not None
        assert sgp.sync_leader is None

    def test_align_members_translates_protocol_ids_and_keeps_outsiders_last(self) -> None:
        """The order of a session protocol maps onto the players of the members we track."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        _make_member(mass, "living_room", audio=["airplay"])
        _make_member(mass, "bathroom", audio=["airplay"])
        # tracked but not part of the live session
        _make_member(mass, "offline")
        session_player = _make_member(mass, "ap_old", None, audio=["airplay"])
        session = _protocol(session_player, "airplay")
        session._attr_group_members = ["airplay_ap_old", "airplay_bathroom", "airplay_living_room"]
        sgp._attr_group_members = ["living_room", "bathroom", "offline"]

        sgp._align_members_with_session(session)

        assert sgp._attr_group_members == ["bathroom", "living_room", "offline"]

    def test_align_members_leaves_order_untouched_without_a_live_session(self) -> None:
        """Without a session (or with one holding none of our members) the order stands."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        stranger = _protocol(_make_member(mass, "stranger", None, audio=["airplay"]), "airplay")
        stranger._attr_group_members = ["airplay_stranger"]
        sgp._attr_group_members = ["living_room", "bathroom"]

        sgp._align_members_with_session(None)
        assert sgp._attr_group_members == ["living_room", "bathroom"]

        sgp._align_members_with_session(stranger)
        assert sgp._attr_group_members == ["living_room", "bathroom"]


class TestPowerLifecycle:
    """Test that power(True/False) drives the group's form/dissolve lifecycle."""

    async def test_power_on_forms_group_and_picks_leader(self) -> None:
        """power(True) should select a sync leader and mark the group powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert sgp.sync_leader is leader
        assert sgp._attr_powered is True

    async def test_power_on_does_not_claim_new_content(self) -> None:
        """Standalone power formation must not suppress a protocol's current media."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp._attr_group_members = ["leader"]

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_form_syncgroup", new=AsyncMock()) as form,
        ):
            await sgp.power(True)

        form.assert_awaited_once_with(new_content=False)
        assert sgp._attr_powered is True

    async def test_power_on_with_no_members_stays_unformed(self) -> None:
        """power(True) on an empty group should leave sync_leader as None but mark powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert sgp.sync_leader is None
        # group is powered (intent to be active) even though no leader could be picked
        assert sgp._attr_powered is True

    async def test_power_off_dissolves_group(self) -> None:
        """power(False) should clear the sync leader and mark unpowered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True

        with patch.object(sgp, "update_state"):
            await sgp.power(False)

        # use getattr to defeat mypy's narrowing of these attributes after the
        # earlier assignments, since it can't see that power(False) mutates them.
        assert getattr(sgp, "sync_leader") is None  # noqa: B009
        assert getattr(sgp, "_attr_powered") is False  # noqa: B009

    async def test_stop_does_not_dissolve_group(self) -> None:
        """stop() should stop the leader but leave the group formed and powered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True

        await sgp.stop()

        # leader was stopped via the internal handler
        mass.players._handle_cmd_stop.assert_awaited_once_with("leader")
        # but the group is still formed: sync_leader and powered are unchanged
        assert sgp.sync_leader is leader
        assert sgp._attr_powered is True


class TestSetMembersDoesNotRegisterIncompatible:
    """Regression test for: incompatible members must NOT be added to _attr_group_members."""

    async def test_incompatible_member_is_not_registered(self) -> None:
        """A member that fails the can_group_with check must not be appended."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        # leader's can_group_with does NOT include the incompatible member
        leader.state.can_group_with = {"leader"}
        _make_member(mass, "incompatible", "alien_protocol")
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

    async def test_compatible_member_is_registered_and_forwarded(self) -> None:
        """A compatible member should be appended and forwarded to the leader."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"compatible"}
        _make_member(mass, "compatible")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]

        await sgp.set_members(player_ids_to_add=["compatible"])

        assert "compatible" in sgp._attr_group_members
        mass.players._handle_set_members.assert_awaited_once()
        kwargs = mass.players._handle_set_members.await_args.kwargs
        assert kwargs.get("player_ids_to_add") == ["compatible"]
        mass.players.trigger_player_update.assert_called_with(_group_id(sgp))

    async def test_incompatible_member_is_refused_by_a_leader_that_reports_compatibility(
        self,
    ) -> None:
        """A prospective leader that does report a compatibility list still refuses joiners."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        # still slaved, but it already reports what it can group with
        registered = _make_member(mass, "registered")
        registered.state.can_group_with = {"registered", "compatible"}
        registered.state.synced_to = "old_leader"
        _make_member(mass, "incompatible", "alien_protocol")
        sgp.sync_leader = None
        sgp._attr_group_members = ["registered"]

        await sgp.set_members(player_ids_to_add=["incompatible"])

        assert sgp._attr_group_members == ["registered"]

    async def test_member_added_when_no_leader_yet(self) -> None:
        """Adding to an empty/unformed group must register the member regardless."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        _make_member(mass, "member")
        # no sync leader, empty group
        sgp.sync_leader = None
        sgp._attr_group_members = []

        await sgp.set_members(player_ids_to_add=["member"])

        # member is registered so a future _form_syncgroup can pick it as leader
        assert "member" in sgp._attr_group_members
        # but _handle_set_members on the leader is not called (no leader yet)
        mass.players._handle_set_members.assert_not_awaited()

    async def test_the_player_of_the_group_is_not_added_to_itself(self) -> None:
        """The group's own player id is no member, its protocol id is no player at all."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)

        await sgp.set_members(player_ids_to_add=[_group_id(sgp), sgp.protocol_id])

        assert sgp._attr_group_members == []


def _real_controller(monkeypatch: pytest.MonkeyPatch) -> PlayerController:
    """
    Return a real players controller on a mocked server.

    The lock budget is short and background tasks are left out.
    """
    monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
    monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", LOCK_BUDGET)
    mass = mock_player_mass()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.test_providers = {}

    def _discard_background(target: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
        if asyncio.iscoroutine(target):
            target.close()
        return MagicMock()

    mass.create_task = MagicMock(side_effect=_discard_background)
    players = PlayerController(mass)
    mass.players = players
    mass.loop = asyncio.get_running_loop()
    players.wait_for_player_update = MagicMock(return_value=_noop_context())  # type: ignore[method-assign]
    players.trigger_player_update = MagicMock()  # type: ignore[method-assign]
    players.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]
    players._release_audio_source = AsyncMock()  # type: ignore[method-assign]
    return players


def _controller_with_a_sync_group(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PlayerController, SyncGroupPlayer, Player, Player]:
    """
    Build a sync group of the speakers L and B, not formed, on a real players controller.

    The lock budget is short and background tasks are left out.

    :return: The controller, the group, speaker L and speaker B.
    """
    players = _real_controller(monkeypatch)
    mass = cast("MagicMock", players.mass)
    group = _make_sync_group(mass, members=["L", "B"])
    leader = _make_member(mass, "L")
    member = _make_member(mass, "B")
    leader.state.can_group_with = {"B"}
    member.state.can_group_with = {"L"}
    group._attr_group_members = ["L", "B"]
    leader.own_protocol._attr_can_group_with = {"sonos_B"}  # type: ignore[union-attr]
    member.own_protocol._attr_can_group_with = {"sonos_L"}  # type: ignore[union-attr]
    leader.own_protocol.play_media = AsyncMock()  # type: ignore[method-assign,union-attr]
    member.own_protocol.play_media = AsyncMock()  # type: ignore[method-assign,union-attr]
    for player in (group.player, leader, member):
        assert player is not None
        player.refresh_state(signal_event=False)
    group.update_state = MagicMock()  # type: ignore[method-assign,misc]
    return players, group, leader, member


class TestFormationAndACommandOnItsLeader:
    """A group that forms around a player and a command queued on that player both complete."""

    async def test_a_source_selection_queued_on_the_leader_and_a_play_on_the_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A source selection waiting for a standalone speaker and a play on its group both run.

        The play forms the group around the speaker while the selection still waits for the
        speaker's lock; neither may then hold one lock while it waits for the other's.
        """
        players, group, leader, member = _controller_with_a_sync_group(monkeypatch)
        players._handle_select_source = AsyncMock()  # type: ignore[method-assign]
        group_id = _group_id(group)

        async with players.get_player_lock("L"):
            selecting = asyncio.create_task(players.select_source("L", "line_in"))
            await asyncio.sleep(0)
            forming = asyncio.create_task(
                players.play_media(group_id, PlayerMedia(uri="x", source_id=group_id))
            )
            for _ in range(10):
                await asyncio.sleep(0)
            # a device report refreshes the members while both commands wait
            for player in (group.player, leader, member):
                assert player is not None
                player.refresh_state(signal_event=False)
        results = await asyncio.gather(selecting, forming, return_exceptions=True)

        assert list(results) == [None, None]
        players._handle_select_source.assert_awaited_once()
        assert not players._lock_holders


def _queue_resume_plays(players: PlayerController) -> None:
    """Let a queue resume play the queue's next item on its player, as a play action does."""

    async def _resume(queue_id: str, fade_in: bool | None = None) -> None:  # noqa: ARG001
        async with players.get_group_and_player_lock(queue_id):
            await players.play_media(queue_id, PlayerMedia(uri="x", source_id=queue_id))

    mass = cast("MagicMock", players.mass)
    mass.player_queues.get = MagicMock(side_effect=lambda queue_id: MagicMock(queue_id=queue_id))
    mass.player_queues.has_lost_paused_stream = MagicMock(return_value=False)
    mass.player_queues.resume = AsyncMock(side_effect=_resume)


class TestCommandOnAMemberOfASyncGroup:
    """A play addressed to a player that follows another one plays on whatever leads it."""

    @pytest.mark.parametrize("command", ["cmd_play", "cmd_resume"])
    async def test_a_member_of_a_formed_sync_group_plays_through_the_group(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """The group's leader plays, and the command returns at once."""
        players, group, leader, member = _controller_with_a_sync_group(monkeypatch)
        _queue_resume_plays(players)
        # formed: the leader holds the member natively
        group.sync_leader = leader
        leader.own_protocol._attr_group_members = ["sonos_L", "sonos_B"]  # type: ignore[union-attr]
        member.own_protocol._attr_synced_to = "sonos_L"  # type: ignore[union-attr]
        # the group follows its leader, the member follows both
        for player in (leader, group.player, member):
            assert player is not None
            player.refresh_state(signal_event=False)
        assert member.state.active_group == _group_id(group)
        waits = record_lock_waits(players)

        await getattr(players, command)("B")

        assert waits == []
        leader.own_protocol.play_media.assert_awaited_once()  # type: ignore[union-attr]

    @pytest.mark.parametrize("command", ["cmd_play", "cmd_resume"])
    async def test_a_native_sync_child_plays_through_its_leader(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """The child's own leader plays, and the command returns at once."""
        players, _group, leader, child = _controller_with_a_sync_group(monkeypatch)
        _queue_resume_plays(players)
        leader.own_protocol._attr_group_members = ["sonos_L", "sonos_B"]  # type: ignore[union-attr]
        child.own_protocol._attr_synced_to = "sonos_L"  # type: ignore[union-attr]
        for player in (leader, child):
            player.refresh_state(signal_event=False)
        assert child.state.synced_to == "L"
        waits = record_lock_waits(players)

        await getattr(players, command)("B")

        assert waits == []
        leader.own_protocol.play_media.assert_awaited_once()  # type: ignore[union-attr]
        child.own_protocol.play_media.assert_not_awaited()  # type: ignore[union-attr]


class _LoadingQueue:
    """Queue controller stand-in with one queue, whose play actions are the real decorated ones."""

    mass: MusicAssistant
    _queue_data: dict[str, PlayerQueueData]

    def __init__(self, mass: MagicMock, queue_id: str) -> None:
        """Initialize the stand-in with the queue of one player."""
        self.mass = mass
        queue = PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=0
        )
        self._queue_data = {queue_id: PlayerQueueData(queue=queue)}
        # set once the item to play is loaded
        self.loaded = asyncio.Event()

    @handle_play_action
    async def play_index(self, queue_id: str) -> None:
        """Load the queue's next item, then play it on the queue's player."""
        await self.loaded.wait()
        await self.mass.players.play_media(queue_id, PlayerMedia(uri="x", source_id=queue_id))

    @handle_play_action
    async def resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """Resume the queue by playing its item."""
        await self.mass.players.play_media(queue_id, PlayerMedia(uri="x", source_id=queue_id))

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Ignore the queue updates."""

    def on_player_update(self, player: Player, changed_values: dict[str, tuple[Any, Any]]) -> None:
        """Ignore the player updates."""


class TestRejoinLeavesABusyMemberAlone:
    """A static group re-adds a returned member only when no command is busy on it."""

    @staticmethod
    def _group_playing_without_a_member(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[PlayerController, SyncGroupPlayer, Player, Player, AsyncMock]:
        """
        Build a static group that plays on its leader L while its member B is back on its own.

        :return: The controller, the group, L, B and the leader's (native) set members, which
            reports the join of B the way the device does.
        """
        players, group, leader, member = _controller_with_a_sync_group(monkeypatch)
        group._attr_static_group_members = ["L", "B"]
        group.sync_leader = leader
        leader_protocol = cast("MockProtocol", leader.own_protocol)
        member_protocol = cast("MockProtocol", member.own_protocol)
        member_protocol._attr_supported_features.add(PlayerFeature.POWER)

        async def _join(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,  # noqa: ARG001
        ) -> None:
            leader_protocol._attr_group_members = ["sonos_L", *(player_ids_to_add or [])]
            member_protocol._attr_synced_to = "sonos_L"  # type: ignore[attr-defined]
            for player in (leader, group.player, member):
                assert player is not None
                player.refresh_state(signal_event=False)

        join = AsyncMock(side_effect=_join)
        leader_protocol.set_members = join  # type: ignore[method-assign]
        for player in (leader, group.player, member):
            assert player is not None
            player.refresh_state(signal_event=False)
        assert group._missing_members(leader) == ["B"]
        return players, group, leader, member, join

    async def test_a_play_on_the_member_queue_plays_while_the_member_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The play reaches the member; the member is re-added at the leader's next report."""
        players, group, _leader, member, join = self._group_playing_without_a_member(monkeypatch)
        queue = _LoadingQueue(cast("MagicMock", players.mass), "B")

        play = asyncio.create_task(queue.play_index("B"))
        await asyncio.sleep(0)
        await group._rejoin_members()
        queue.loaded.set()
        await play

        member.own_protocol.play_media.assert_awaited_once()  # type: ignore[union-attr]
        join.assert_not_awaited()
        await group._rejoin_members()
        join.assert_awaited_once()

    async def test_a_power_on_that_plays_plays_while_the_member_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A power on of the member with auto play plays on it, and the re-add waits its turn."""
        players, group, leader, member, join = self._group_playing_without_a_member(monkeypatch)
        queue = _LoadingQueue(cast("MagicMock", players.mass), "B")
        cast("MagicMock", players.mass).player_queues.resume = queue.resume
        member_protocol = cast("MockProtocol", member.own_protocol)
        member_protocol._attr_powered = False
        member.refresh_state(signal_event=False)
        assert group._missing_members(leader) == ["B"]
        powered_on = asyncio.Event()

        async def _power(powered: bool) -> None:
            await powered_on.wait()
            member_protocol._attr_powered = powered
            member.refresh_state(signal_event=False)

        member_protocol.power = _power  # type: ignore[method-assign]
        config_get_value = member.config.get_value
        member.config.get_value = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda key, *args, **kwargs: (
                True if key == CONF_AUTO_PLAY else config_get_value(key, *args, **kwargs)
            )
        )

        power_on = asyncio.create_task(players.cmd_power("B", True))
        await asyncio.sleep(0)
        # the leader reports while the member powers on; a re-add would power it on as well
        rejoin = asyncio.create_task(group._rejoin_members())
        await asyncio.sleep(0)
        powered_on.set()
        await asyncio.gather(power_on, rejoin)

        member_protocol.play_media.assert_awaited_once()  # type: ignore[attr-defined]
        join.assert_not_awaited()

    async def test_a_member_nobody_commands_is_re_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A returned member that is not busy joins the leader again at once."""
        _players, group, _leader, member, join = self._group_playing_without_a_member(monkeypatch)

        await group._rejoin_members()

        join.assert_awaited_once()
        assert member.state.active_group == _group_id(group)


def _group_on_sendspin_with_an_airplay_member_back(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PlayerController, SyncGroupPlayer, Player, Player, Player]:
    """
    Build a static group of A, B and C that plays over Sendspin on A and B, with C back.

    A and B also have AirPlay and C has nothing else to group over, so adding C moves the
    group to AirPlay. A resume of the group's queue plays it on its leader again.

    :return: The controller, the group, A, B and C.
    """
    players = _real_controller(monkeypatch)
    mass = cast("MagicMock", players.mass)
    mass.config.get_player_value = MagicMock(
        side_effect=lambda player_id, key, default=None: (
            "sendspin_A" if (player_id, key) == ("A", CONF_PREFERRED_OUTPUT_PROTOCOL) else default
        )
    )
    group = _make_sync_group(mass, dynamic=False, members=["A", "B", "C"])
    a = _make_member(mass, "A", "sonos", audio=["airplay", "sendspin"], active_output="sendspin")
    b = _make_member(mass, "B", "wiim", audio=["airplay", "sendspin"], active_output="sendspin")
    c = _make_member(mass, "C", "wiim", audio=["airplay"])
    assert group.player is not None
    devices = (a, group.player, b, c)

    def _refresh() -> None:
        # twice: the leader reads the group's state, which is derived from the leader's
        for player in (*devices, *devices):
            player.refresh_state(signal_event=False)

    def _reports(protocol: MockProtocol) -> None:
        """Let a protocol report its group, start and stop the way the device does."""
        set_members = protocol.set_members

        async def _set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            await set_members(player_ids_to_add, player_ids_to_remove)
            _refresh()

        async def _play_media(media: PlayerMedia) -> None:
            protocol._attr_current_media = media
            protocol._attr_playback_state = PlaybackState.PLAYING
            _refresh()

        async def _stop() -> None:
            protocol._attr_playback_state = PlaybackState.IDLE
            _refresh()

        protocol.set_members = _set_members  # type: ignore[method-assign]
        protocol.play_media = AsyncMock(side_effect=_play_media)  # type: ignore[method-assign]
        protocol.stop = AsyncMock(side_effect=_stop)  # type: ignore[method-assign]

    protocols = [
        protocol for player in (a, b, c) for protocol in player.attached_protocols.values()
    ]
    for protocol in protocols:
        assert isinstance(protocol, MockProtocol)
        _reports(protocol)
        protocol._attr_can_group_with = {
            other.protocol_id
            for other in protocols
            if other is not protocol and other.provider is protocol.provider
        }
    _protocol(a, "sendspin")._attr_group_members = ["sendspin_A", "sendspin_B"]
    for protocol in (_protocol(a, "sendspin"), _protocol(b, "sendspin")):
        protocol._attr_playback_state = PlaybackState.PLAYING
    group._attr_static_group_members = ["A", "B", "C"]
    group._attr_group_members = ["A", "B"]
    group.sync_leader = a
    group.update_state = MagicMock()  # type: ignore[method-assign,misc]
    _queue_resume_plays(players)
    _refresh()
    assert a.state.playback_state == PlaybackState.PLAYING
    assert a.state.active_group == _group_id(group)
    assert group._missing_members(a) == ["C"]
    return players, group, a, b, c


class TestRejoinThatMovesTheGroupToAnotherProtocol:
    """A re-add that moves the group to another protocol keeps it playing, or leaves it be."""

    @staticmethod
    def _assert_plays_on_airplay_with_every_member(
        group: SyncGroupPlayer, a: Player, c: Player
    ) -> None:
        """Assert the group plays on A over AirPlay again, with B and C following."""
        assert a.active_output_protocol == "airplay_A"
        assert a.state.playback_state == PlaybackState.PLAYING
        assert _protocol(a, "sendspin").playback_state == PlaybackState.IDLE
        assert set(_protocol(a, "airplay").group_members) == {"airplay_A", "airplay_B", "airplay_C"}
        assert c.active_output_protocol == "airplay_C"
        assert group._attr_group_members == ["A", "B", "C"]
        assert group._missing_members(a) == []

    @pytest.mark.parametrize("from_a_holder_of_the_group", [False, True])
    async def test_the_group_plays_on_with_every_member(
        self, monkeypatch: pytest.MonkeyPatch, from_a_holder_of_the_group: bool
    ) -> None:
        """
        The leader resumes on the new protocol and B follows it, as C joins.

        A re-add from a task that holds the group's lock already runs as well.
        """
        players, group, a, _b, c = _group_on_sendspin_with_an_airplay_member_back(monkeypatch)

        async with AsyncExitStack() as stack:
            if from_a_holder_of_the_group:
                await stack.enter_async_context(players.get_player_lock(_group_id(group)))
            await group._rejoin_members()

        self._assert_plays_on_airplay_with_every_member(group, a, c)
        assert not players._lock_holders

    async def test_a_busy_group_is_left_as_it_was_until_the_next_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A re-add while a command holds the group changes nothing and does not wait for it.

        The leader's next report re-adds C.
        """
        players, group, a, b, c = _group_on_sendspin_with_an_airplay_member_back(monkeypatch)

        async with players.get_player_lock(_group_id(group)):
            rejoin = asyncio.create_task(group._rejoin_members())
            for _ in range(10):
                await asyncio.sleep(0)
            assert rejoin.done()
            await rejoin

        assert a.active_output_protocol == "sendspin_A"
        assert b.active_output_protocol == "sendspin_B"
        assert c.active_output_protocol is None
        assert a.state.playback_state == PlaybackState.PLAYING
        assert _protocol(a, "sendspin").group_members == ["sendspin_A", "sendspin_B"]
        assert _protocol(a, "airplay").group_members == []
        for player in (a, b, c):
            for protocol in player.attached_protocols.values():
                cast("AsyncMock", protocol.stop).assert_not_awaited()
        assert group._attr_group_members == ["A", "B"]

        await group._rejoin_members()

        self._assert_plays_on_airplay_with_every_member(group, a, c)


# the ways a user adds C to the leader A by hand: the frontend's group panel, the MCP server
# and the API send a member change, Home Assistant's join a group of many
ADD_C_TO_A_BY_HAND = {
    "set_members": lambda players: players.cmd_set_members("A", player_ids_to_add=["C"]),
    "group_many": lambda players: players.cmd_group_many("A", ["C"]),
    "group": lambda players: players.cmd_group("C", "A"),
}


class TestMemberAddedToTheLeaderByHand:
    """A member added by hand to the leader of a static group keeps the group playing."""

    @staticmethod
    def _assert_plays_on_airplay_with_every_member(a: Player, c: Player) -> None:
        """Assert the group plays on A over AirPlay again, with B and C following."""
        assert a.active_output_protocol == "airplay_A"
        assert a.state.playback_state == PlaybackState.PLAYING
        assert _protocol(a, "sendspin").playback_state == PlaybackState.IDLE
        assert set(_protocol(a, "airplay").group_members) == {"airplay_A", "airplay_B", "airplay_C"}
        assert c.active_output_protocol == "airplay_C"

    @pytest.mark.parametrize("add", ADD_C_TO_A_BY_HAND.values(), ids=ADD_C_TO_A_BY_HAND.keys())
    async def test_the_group_plays_on_with_every_member(
        self,
        monkeypatch: pytest.MonkeyPatch,
        add: Callable[[PlayerController], Awaitable[None]],
    ) -> None:
        """The leader resumes on the new protocol and B follows it, as C joins."""
        players, _group, a, _b, c = _group_on_sendspin_with_an_airplay_member_back(monkeypatch)

        await add(players)

        self._assert_plays_on_airplay_with_every_member(a, c)
        assert not players._lock_holders

    async def test_the_add_waits_for_a_command_on_the_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An add while a command holds the group changes nothing until that one is done."""
        players, group, a, b, c = _group_on_sendspin_with_an_airplay_member_back(monkeypatch)

        async with players.get_player_lock(_group_id(group)):
            adding = asyncio.create_task(players.cmd_set_members("A", player_ids_to_add=["C"]))
            for _ in range(10):
                await asyncio.sleep(0)
            assert not adding.done()
            assert a.active_output_protocol == "sendspin_A"
            assert b.active_output_protocol == "sendspin_B"
            assert c.active_output_protocol is None
        await adding

        self._assert_plays_on_airplay_with_every_member(a, c)


class TestFormationTakesTheLeaderFirst:
    """A group that forms records its leader only once it holds the leader's lock."""

    async def test_a_busy_leader_is_not_recorded(self) -> None:
        """A formation that cannot get the leader's lock leaves the group and its timers be."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"member"}
        _make_member(mass, "member")
        sgp.sync_leader = None
        sgp._attr_group_members = ["leader", "member"]
        mass.players.get_player_lock = _busy_lock()

        with (
            patch.object(sgp, "_cancel_idle_grace_timer") as cancel_idle_grace,
            patch.object(sgp, "_cancel_reform_timer") as cancel_reform,
            pytest.raises(ResourceBusyError),
        ):
            await sgp._form_syncgroup()

        assert sgp.sync_leader is None
        mass.players.get_player_lock.assert_called_once_with("leader", PlayerLockPurpose.PLAYBACK)
        mass.players._handle_set_members.assert_not_awaited()
        cancel_idle_grace.assert_not_called()
        cancel_reform.assert_not_called()


class TestSetMembersWithABusyLeader:
    """A member change that cannot get the leader's lock leaves the recorded members as they were."""

    @pytest.mark.parametrize(
        ("add", "remove"),
        [(["new"], None), (None, ["member"]), (None, ["leader"]), (None, ["leader", "member"])],
    )
    async def test_the_member_list_stays_as_it_was(
        self, add: list[str] | None, remove: list[str] | None
    ) -> None:
        """Adding, removing, and removing the leader all fail before the list changes."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"member", "new"}
        _make_member(mass, "member")
        _make_member(mass, "new")
        leader.state.group_members = UniqueList(["leader", "member"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]
        mass.players.get_player_lock = _busy_lock()

        with patch.object(sgp, "update_state"), pytest.raises(ResourceBusyError):
            await sgp.set_members(player_ids_to_add=add, player_ids_to_remove=remove)

        assert sgp._attr_group_members == ["leader", "member"]
        assert sgp.sync_leader is leader
        mass.players._handle_set_members.assert_not_awaited()
        mass.players._handle_cmd_stop.assert_not_awaited()


class TestSetMembersRemovesExternalJoiner:
    """Regression: a member joined to the leader outside MA must be removable."""

    async def test_external_joiner_removal_forwards_to_leader(self) -> None:
        """
        Removing an external joiner must forward straight to the sync leader.

        A player grouped directly to the sync leader (e.g. via the Sonos app)
        appears in group_members via the leader's live state but is not in the
        group's tracked member list, so it must not be silently skipped.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        # the leader's live native group includes an external joiner the group
        # itself never registered
        leader.state.group_members = UniqueList(["leader", "external_x"])
        _make_member(mass, "external_x")
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

    async def test_tracked_member_removal_uses_managed_path(self) -> None:
        """A member the group actually manages is removed via the managed path."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.group_members = UniqueList(["leader", "member_b"])
        _make_member(mass, "member_b")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member_b"]

        await sgp.set_members(player_ids_to_remove=["member_b"])

        # tracked member is dropped from the internal list and forwarded
        assert "member_b" not in sgp._attr_group_members
        mass.players._handle_set_members.assert_awaited_once()
        call = mass.players._handle_set_members.await_args
        assert call.kwargs.get("player_ids_to_remove") == ["member_b"]


class TestPlayerFilters:
    """Test the allowed_members filter semantics."""

    def test_no_filter_allows_anything(self) -> None:
        """Empty filter: any player is allowed."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        assert sgp._is_member_allowed("p1") is True

    def test_allowed_members_restricts_to_listed(self) -> None:
        """allowed_members populated: only listed IDs pass."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, allowed_members=["p1"])
        assert sgp._is_member_allowed("p1") is True
        assert sgp._is_member_allowed("p2") is False

    async def test_set_members_rejects_filtered_player(self) -> None:
        """set_members must skip a candidate that fails the filter."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, allowed_members=["allowed"])
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"allowed", "blocked"}
        _make_member(mass, "allowed")
        _make_member(mass, "blocked")
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
        sgp = _make_sync_group(mass, allowed_members=["candidate"])
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"candidate", "outsider"}
        _make_member(mass, "candidate")
        _make_member(mass, "outsider")
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

    async def test_preset_member_can_be_unjoined(self) -> None:
        """A preset member in a dynamic group can be unjoined for the session."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["leader", "preset_b"])
        await sgp.on_config_updated()
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"preset_b"}
        _make_member(mass, "preset_b")
        sgp.sync_leader = leader

        await sgp.set_members(player_ids_to_remove=["preset_b"])

        assert "preset_b" not in sgp._attr_group_members

    async def test_static_group_members_empty_in_dynamic_mode(self) -> None:
        """In dynamic mode static_group_members is empty (frontend gating)."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["a", "b", "c"])
        await sgp.on_config_updated()
        assert sgp._attr_static_group_members == []
        # but the configured members are seeded into group_members
        assert set(sgp._attr_group_members) == {"a", "b", "c"}

    async def test_static_group_members_are_the_configured_ones_in_static_mode(self) -> None:
        """In static mode the configured members are the static ones, by player id."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, dynamic=False, members=["a", "b"])
        await sgp.on_config_updated()
        assert sgp.static_group_members == ["a", "b"]
        assert PlayerFeature.SET_MEMBERS not in sgp.supported_features

    async def test_power_on_re_applies_preset_members(self) -> None:
        """Powering on re-applies the configured preset, restoring any previously unjoined member."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["leader", "preset_b"])
        await sgp.on_config_updated()
        _make_member(mass, "leader")
        _make_member(mass, "preset_b")
        sgp._attr_group_members = ["leader"]  # preset_b was unjoined in a previous session

        with patch.object(sgp, "update_state"):
            await sgp.power(True)

        assert "preset_b" in sgp._attr_group_members

    async def test_form_syncgroup_does_not_re_add_unjoined_preset(self) -> None:
        """Mid-session unjoins must stick: _form_syncgroup must NOT re-add preset members."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["leader", "preset_b"])
        await sgp.on_config_updated()
        leader = _make_member(mass, "leader")
        leader.state.group_members = UniqueList(["leader"])
        _make_member(mass, "preset_b")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]  # preset_b was unjoined during this session

        with patch.object(sgp, "update_state"):
            await sgp._form_syncgroup()

        assert "preset_b" not in sgp._attr_group_members

    def test_preset_member_bypasses_allow_list_filter(self) -> None:
        """
        A preset member that was unjoined must still pass the allow-list filter.

        Regression: after unjoining a preset member, the user must be able to
        re-add it via the OSD even when allowed_members doesn't list it.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["preset_a", "preset_b"], allowed_members=["other"])

        # preset members bypass the allow-list filter
        assert sgp._is_member_allowed("preset_a") is True
        assert sgp._is_member_allowed("preset_b") is True
        # the explicit allow-list entry still passes
        assert sgp._is_member_allowed("other") is True
        # an unrelated player is rejected
        assert sgp._is_member_allowed("outsider") is False

    async def test_can_group_with_falls_back_when_all_members_offline(self) -> None:
        """
        An offline preset member must not block adding other players.

        Regression: with only offline members in the list, can_group_with
        returned an empty set instead of offering compatible players, so
        nothing could be added to the group until the member came back.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["offline_member"])
        await sgp.on_config_updated()
        _make_member(mass, "offline_member", available=False)
        candidate = _make_member(mass, "candidate")
        candidate.state.can_group_with = {"other"}

        assert "candidate" in sgp.can_group_with

    async def test_can_group_with_aggregates_from_available_members(self) -> None:
        """With at least one available member, candidates come from the members only."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass, members=["offline_member", "online_member"])
        await sgp.on_config_updated()
        _make_member(mass, "offline_member", available=False)
        online_member = _make_member(mass, "online_member")
        online_member.state.can_group_with = {"online_member", "friend"}
        _make_member(mass, "stranger").state.can_group_with = {"other"}

        with patch.object(
            mass.players, "iter_players", wraps=mass.players.iter_players
        ) as iter_players:
            result = sgp.can_group_with

        assert {"online_member", "friend"} <= result
        assert "stranger" not in result
        iter_players.assert_not_called()

    async def test_can_group_with_offers_a_member_still_reporting_stale_sync_state(self) -> None:
        """
        A player that still reports the just-dissolved group must stay joinable.

        Regression: for a few seconds after a group broke up, its former members
        still reported the old sync state (an empty can_group_with and an
        active_group pointing at this group), which excluded them here — so a
        join in that window was refused.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        await sgp.on_config_updated()
        candidate = _make_member(mass, "candidate")
        candidate.state.can_group_with = set()
        candidate.state.synced_to = "old_leader"
        candidate.state.active_group = _group_id(sgp)

        assert "candidate" in sgp.can_group_with

    async def test_can_group_with_offers_a_member_that_still_claims_this_group(self) -> None:
        """A player whose sync settled but whose active_group still points here is offered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        await sgp.on_config_updated()
        candidate = _make_member(mass, "candidate")
        candidate.state.can_group_with = {"other"}
        candidate.state.synced_to = None
        candidate.state.active_group = _group_id(sgp)

        assert "candidate" in sgp.can_group_with

    async def test_can_group_with_skips_a_member_held_by_another_group(self) -> None:
        """A player that is claimed by another group player is not offered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        await sgp.on_config_updated()
        candidate = _make_member(mass, "candidate")
        candidate.state.can_group_with = set()
        candidate.state.synced_to = "other_leader"
        candidate.state.active_group = "pl_syncgroup_other"
        # a claim naming the protocol id of this group is no claim of this group
        held_by_protocol_id = _make_member(mass, "held_by_protocol_id")
        held_by_protocol_id.state.can_group_with = {"other"}
        held_by_protocol_id.state.active_group = sgp.protocol_id

        assert "candidate" not in sgp.can_group_with
        assert "held_by_protocol_id" not in sgp.can_group_with

    async def test_can_group_with_skips_a_player_that_cannot_group_at_all(self) -> None:
        """A player that reports no grouping capability at all is not offered."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        await sgp.on_config_updated()
        candidate = _make_member(mass, "solo_only")
        candidate.state.can_group_with = set()
        candidate.state.synced_to = None
        candidate.state.active_group = None

        assert "solo_only" not in sgp.can_group_with

    async def test_can_group_with_ignores_a_member_that_is_still_slaved(self) -> None:
        """
        A member that is still slaved must not narrow down what the group offers.

        Regression: once one speaker had rejoined after a dissolve, it was the only
        candidate the group offered (a slaved player reports no compatible players),
        so the next speaker's join was refused.
        """
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        await sgp.on_config_updated()
        for player_id in ("m2", "m3"):
            player = _make_member(mass, player_id)
            player.state.can_group_with = set()
            player.state.synced_to = "old_leader"
            player.state.active_group = _group_id(sgp)
        sgp._attr_group_members = ["m2"]

        assert "m3" in sgp.can_group_with


class TestGetConfigEntriesMemberPicker:
    """Test the member options offered in the group settings dropdown."""

    async def test_slaved_follower_is_still_offered(self) -> None:
        """
        A synced follower must stay selectable in the member dropdown.

        Regression: a member removed from the config during active playback
        remains slaved at the protocol level. Its can_group_with is then empty,
        and since it is no longer in the saved ids it vanished from the
        dropdown, making it impossible to re-add without stopping playback.
        """
        mass = _make_mock_mass()
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"follower"}
        # slaved follower: empty can_group_with while synced_to is set
        follower = _make_member(mass, "follower")
        follower.state.synced_to = "leader"
        # idle player that cannot group with anything must NOT be offered
        solo = _make_member(mass, "solo")
        sgp = _make_sync_group(mass)
        mass.players.all_players = MagicMock(return_value=[leader, follower, solo, sgp.player])

        entries = await sgp.get_config_entries()
        members_entry = next(x for x in entries if x.key == CONF_GROUP_MEMBERS)
        option_ids = {option.value for option in members_entry.options}

        # the options name players, never their protocols
        assert option_ids == {"leader", "follower"}


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
        sgp.sync_leader = _make_member(mass, "leader")
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

    async def test_play_media_forms_group_when_dormant(self) -> None:
        """play_media on a dormant group should select a leader and forward to it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]
        assert sgp.sync_leader is None

        with patch.object(sgp, "update_state"):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp.sync_leader is leader
        # mypy narrows sync_leader to None from the earlier assert; the
        # play_media call is what re-populates it, but mypy can't track that
        # mutation through the patch context — hence the ignore.
        mass.players._handle_play_media.assert_awaited_once()  # type: ignore[unreachable]

    async def test_play_media_releases_linked_protocol_takeover_owner(self) -> None:
        """SyncGroup playback releases the protocol owner returned by controller dispatch."""
        mass = _make_mock_mass()
        mass.config.get_raw_core_config_value.return_value = "INFO"
        controller = PlayerController(mass)
        mass.players = controller
        wiim_provider = _provider(mass, "wiim")
        sendspin_provider = _provider(mass, "sendspin")
        parent, _ = attach_mock(wiim_provider, "wiim_parent", "Parent", player_id="parent")
        child, _ = attach_mock(wiim_provider, "wiim_child", "Child", player_id="child")
        _, protocol = attach_mock(
            sendspin_provider, "spb_parent", "Protocol", role=ProtocolRole.AUDIO, player=parent
        )
        _, child_protocol = attach_mock(
            sendspin_provider, "spb_child", "Child protocol", role=ProtocolRole.AUDIO, player=child
        )
        for player in (parent, child):
            for member_protocol in player.attached_protocols.values():
                member_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_any: Any = protocol
        protocol_any._content_takeover_pending = False
        protocol_any.snapshot = object()
        events: list[str] = []

        async def _takeover() -> int:
            events.append("clear")
            protocol_any.snapshot = None
            return 3

        async def _takeover_finished(_token: object) -> None:
            events.append("finish")
            protocol_any._content_takeover_pending = False

        async def _set_members(**_kwargs: object) -> None:
            events.append("set_members")
            assert protocol_any.snapshot is None

        protocol.on_group_content_takeover = AsyncMock(side_effect=_takeover)  # type: ignore[method-assign]
        protocol.on_group_content_takeover_finished = AsyncMock(side_effect=_takeover_finished)  # type: ignore[method-assign]
        protocol.set_members = AsyncMock(side_effect=_set_members)  # type: ignore[method-assign]
        protocol.play_media = AsyncMock()  # type: ignore[method-assign]
        child_protocol.set_members = AsyncMock()  # type: ignore[method-assign]
        parent.set_active_output_protocol("spb_parent")
        for player in (parent, child):
            player.update_state(signal_event=False)
        parent.state.can_group_with = {"child"}
        sgp = _make_sync_group(mass)
        sgp.sync_leader = parent
        sgp._attr_group_members = ["parent", "child"]

        with (
            patch.object(sgp, "update_state"),
            patch("music_assistant.providers.sync_group.player.PLAYBACK_START_TIMEOUT", 0.01),
        ):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        protocol.on_group_content_takeover.assert_awaited_once()
        protocol.on_group_content_takeover_finished.assert_awaited_once_with(3)
        assert protocol_any._content_takeover_pending is False
        assert protocol._attr_supported_features >= {PlayerFeature.SET_MEMBERS}
        assert events == ["clear", "set_members", "finish"]

    async def test_play_preparation_aborts_unconsumed_takeover_on_cancel(self) -> None:
        """Cancellation between fake power and play aborts the captured generation."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        owner = MagicMock()
        owner.on_group_content_takeover_aborted = AsyncMock()
        sgp._pending_content_takeover = (owner, 7)

        with pytest.raises(asyncio.CancelledError):
            async with sgp.prepare_play_media():
                raise asyncio.CancelledError

        owner.on_group_content_takeover_aborted.assert_awaited_once_with(7)
        assert sgp._pending_content_takeover is None

    async def test_play_media_aborts_takeover_when_play_start_fails(self) -> None:
        """A failed leader play aborts rather than finishing a takeover generation."""
        mass, sgp, owner = _make_play_media_takeover_case(9)
        mass.players._handle_play_media = AsyncMock(side_effect=RuntimeError("play failed"))

        with pytest.raises(RuntimeError, match="play failed"):
            await sgp.play_media(MagicMock(uri="new", source_id="src"))

        owner.on_group_content_takeover_aborted.assert_awaited_once_with(9)
        owner.on_group_content_takeover_finished.assert_not_awaited()

    async def test_play_media_finishes_takeover_after_play_start(self) -> None:
        """Successful leader playback finishes the captured takeover generation."""
        _mass, sgp, owner = _make_play_media_takeover_case(9)

        await sgp.play_media(MagicMock(uri="new", source_id="src"))

        owner.on_group_content_takeover_finished.assert_awaited_once_with(9)
        owner.on_group_content_takeover_aborted.assert_not_awaited()

    async def test_stop_dissolves_group_when_powerless(self) -> None:
        """stop() on a group without power control should dissolve the session."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]
        # _attr_powered is None by default (no power control) so stop dissolves
        assert sgp._attr_powered is None

        with patch.object(sgp, "update_state"):
            await sgp.stop()

        # leader was stopped via the internal handler ...
        mass.players._handle_cmd_stop.assert_any_await("leader")
        # ... and the session was dissolved (sync_leader cleared)
        assert sgp.sync_leader is None

    async def test_stop_preserves_group_when_pinned_with_fake_power(self) -> None:
        """stop() should NOT dissolve when the user has pinned the group on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True  # user explicitly pinned via Fake control

        await sgp.stop()

        mass.players._handle_cmd_stop.assert_any_await("leader")
        # session stays intact
        assert sgp.sync_leader is leader


@pytest.mark.parametrize("active_output", [None, "native", "sendspin_leader", "missing"])
def test_group_media_uses_active_session(active_output: str | None) -> None:
    """Group queue identity follows the stream's raw media, not the device's native input."""
    mass = _make_mock_mass()
    group = _make_sync_group(mass)
    leader = _make_member(
        mass, "leader", "wiim", audio=["sendspin"], playback_state=PlaybackState.PLAYING
    )
    leader._active_output_protocol = active_output
    native = _protocol(leader, "wiim")
    native._attr_current_media = PlayerMedia(uri="wiimu_airplay", source_id="airplay")
    bridge = _protocol(leader, "sendspin")
    bridge._attr_current_media = PlayerMedia(
        uri="library://radio/1", source_id=_group_id(group), queue_item_id="radio_item"
    )
    group.sync_leader = leader

    group._update_attributes()

    expected = bridge if active_output == "sendspin_leader" else native
    assert group.current_media is expected.current_media
    assert group.current_media is not leader.state.current_media


class TestIdleGraceTimer:
    """The idle-grace timer absorbs natural PLAYING→IDLE transitions."""

    def test_grace_scheduled_on_playing_to_idle(self) -> None:
        """_update_attributes schedules a grace task when the leader transitions to IDLE."""
        mass = _make_mock_mass()
        # create_task should return a sentinel we can inspect
        sentinel = MagicMock()
        mass.create_task = MagicMock(return_value=sentinel)
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader", playback_state=PlaybackState.IDLE)
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
        sgp.sync_leader = _make_member(mass, "leader", playback_state=PlaybackState.IDLE)
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
        sgp.sync_leader = _make_member(mass, "leader", playback_state=PlaybackState.PLAYING)
        # pretend a grace task is pending
        prior_task = MagicMock()
        prior_task.done.return_value = False
        sgp._idle_grace_task = prior_task
        sgp._attr_playback_state = PlaybackState.IDLE

        sgp._update_attributes()

        prior_task.cancel.assert_called_once()
        assert sgp._idle_grace_task is None

    async def test_grace_cancelled_on_explicit_stop(self) -> None:
        """An explicit stop cancels the pending grace task immediately."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader")
        prior_task = MagicMock()
        prior_task.done.return_value = False
        sgp._idle_grace_task = prior_task

        with patch.object(sgp, "update_state"):
            await sgp.stop()

        prior_task.cancel.assert_called_once()
        assert sgp._idle_grace_task is None


class TestIdleGraceOnFailedStart:
    """A group formed for a start that never materializes must arm the grace dissolve."""

    async def test_grace_scheduled_when_leader_never_starts_playing(self) -> None:
        """play_media where the leader stays IDLE (e.g. 404 media) arms the grace timer."""
        mass = _make_mock_mass()
        sentinel = MagicMock()
        mass.create_task = MagicMock(return_value=sentinel)
        sgp = _make_sync_group(mass)
        _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]

        with patch.object(sgp, "update_state"):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        # the leader stayed IDLE, so the failed start must leave a pending dissolve
        assert sgp._idle_grace_task is sentinel

    async def test_no_grace_when_leader_confirms_playback(self) -> None:
        """play_media where the leader reports PLAYING must not arm the grace timer."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        sgp = _make_sync_group(mass)
        _make_member(mass, "leader", playback_state=PlaybackState.PLAYING)
        sgp._attr_group_members = ["leader"]

        with patch.object(sgp, "update_state"):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp._idle_grace_task is None
        mass.create_task.assert_not_called()

    async def test_no_grace_on_failed_start_when_pinned(self) -> None:
        """No grace timer on a failed start when the user pinned the group with Fake power."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        sgp = _make_sync_group(mass)
        _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]
        sgp._attr_powered = True  # explicit pin via Fake power control

        with patch.object(sgp, "update_state"):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp._idle_grace_task is None
        mass.create_task.assert_not_called()

    async def test_grace_scheduled_when_play_command_raises(self) -> None:
        """A play command that raises still arms the grace timer for the formed group."""
        mass = _make_mock_mass()
        sentinel = MagicMock()
        mass.create_task = MagicMock(return_value=sentinel)
        mass.players._handle_play_media = AsyncMock(side_effect=RuntimeError("boom"))
        sgp = _make_sync_group(mass)
        _make_member(mass, "leader")
        sgp._attr_group_members = ["leader"]

        with patch.object(sgp, "update_state"), pytest.raises(RuntimeError):
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp._idle_grace_task is sentinel


def _recording_lock(events: list[str]) -> MagicMock:
    """Build a get_player_lock mock that records every lock enter/exit in ``events``."""

    def _make(
        player_id: str,
        _purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
    ) -> AsyncMock:
        ctx = AsyncMock()

        async def _enter(*_args: Any) -> None:
            events.append(f"lock:{player_id}")

        async def _exit(*_args: Any) -> bool:
            events.append(f"unlock:{player_id}")
            return False

        ctx.__aenter__.side_effect = _enter
        ctx.__aexit__.side_effect = _exit
        return ctx

    return MagicMock(side_effect=_make)


def _group_lock(group_id: str) -> tuple[MagicMock, asyncio.Lock]:
    """Build a get_player_lock mock backed by a real lock for the group; others are no-ops."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def _ctx(
        player_id: str,
        _purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
    ) -> AsyncIterator[None]:
        if player_id != group_id:
            yield
            return
        async with lock:
            yield

    return MagicMock(side_effect=_ctx), lock


def _run_scheduled_tasks(mass: MagicMock) -> None:
    """Let the mocked mass run scheduled coroutines for real, started eagerly like mass does."""
    mass.create_task = MagicMock(
        side_effect=lambda coro, *_a, **_k: asyncio.Task(
            coro, loop=asyncio.get_running_loop(), eager_start=True
        )
    )


class TestIdleGraceDissolveLocking:
    """The idle-grace dissolve runs under the group's lock, serialized with group commands."""

    def _setup_group(self, mass: MagicMock) -> tuple[SyncGroupPlayer, Player]:
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "wiim")
        leader.state.can_group_with = {"m2", "m3"}
        leader.state.group_members = UniqueList(["leader", "m2"])
        _make_member(mass, "m2", "wiim")
        _make_member(mass, "m3", "wiim")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2"]
        sgp._attr_powered = None  # no pin, so the grace dissolve applies
        # run scheduled coroutines for real so the grace runner actually fires
        _run_scheduled_tasks(mass)
        return sgp, leader

    async def _start_member_change(
        self,
        mass: MagicMock,
        sgp: SyncGroupPlayer,
        group_lock: asyncio.Lock,
        events: list[str],
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        """Start a member change that holds the group lock until the returned event is set."""
        leader_command_started = asyncio.Event()
        release_leader_command = asyncio.Event()

        async def _leader_command(*_args: Any, **_kwargs: Any) -> None:
            events.append("leader command")
            leader_command_started.set()
            await release_leader_command.wait()

        mass.players._handle_set_members = AsyncMock(side_effect=_leader_command)

        async def _group_command() -> None:
            # the controller runs group commands from under the group's lock
            async with group_lock:
                await sgp.set_members(player_ids_to_add=["m3"])
            events.append("member change done")

        command = asyncio.create_task(_group_command())
        await leader_command_started.wait()
        return command, release_leader_command

    async def test_dissolve_runs_under_the_group_lock(self) -> None:
        """The runner takes the group's lock first and the leader's lock from under it."""
        mass = _make_mock_mass()
        sgp, leader = self._setup_group(mass)
        events: list[str] = []
        mass.players.get_player_lock = _recording_lock(events)

        with (
            patch("music_assistant.providers.sync_group.player.IDLE_GRACE_SECONDS", 0),
            patch.object(sgp, "update_state"),
        ):
            sgp._schedule_idle_grace_timer()
            grace = sgp._idle_grace_task
            assert grace is not None
            await grace

        # the lock of the group is that of its player
        mass.players.get_player_lock.assert_any_call(_group_id(sgp), PlayerLockPurpose.PLAYBACK)
        assert events == [
            f"lock:{_group_id(sgp)}",
            "lock:leader",
            "unlock:leader",
            f"unlock:{_group_id(sgp)}",
        ]
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_remove=["m2"]
        )
        assert sgp.sync_leader is None
        assert sgp._idle_grace_task is None

    async def test_dissolve_waits_for_a_member_change_holding_the_group_lock(self) -> None:
        """A grace expiry during an in-flight member change dissolves only after it completes."""
        mass = _make_mock_mass()
        sgp, leader = self._setup_group(mass)
        mass.players.get_player_lock, group_lock = _group_lock(_group_id(sgp))
        events: list[str] = []

        async def _dissolve() -> None:
            events.append("dissolve")
            sgp.sync_leader = None

        dissolve = AsyncMock(side_effect=_dissolve)
        with (
            patch("music_assistant.providers.sync_group.player.IDLE_GRACE_SECONDS", 0),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_dissolve_syncgroup", dissolve),
        ):
            command, release = await self._start_member_change(mass, sgp, group_lock, events)
            # the grace window expires while the member change is still in flight
            sgp._schedule_idle_grace_timer()
            grace = sgp._idle_grace_task
            assert grace is not None
            await asyncio.sleep(0.01)

            dissolve.assert_not_awaited()
            assert sgp.sync_leader is leader
            # the group keeps claiming its members while the dissolve is queued
            assert sgp.is_active_session is True

            release.set()
            await asyncio.gather(command, grace)

        assert events == ["leader command", "member change done", "dissolve"]
        assert "m3" in sgp._attr_group_members

    async def test_cancel_while_queued_on_the_lock_skips_the_dissolve(self) -> None:
        """Playback resuming while the runner waits for the lock still cancels the dissolve."""
        mass = _make_mock_mass()
        sgp, leader = self._setup_group(mass)
        mass.players.get_player_lock, group_lock = _group_lock(_group_id(sgp))
        events: list[str] = []

        dissolve = AsyncMock()
        with (
            patch("music_assistant.providers.sync_group.player.IDLE_GRACE_SECONDS", 0),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_dissolve_syncgroup", dissolve),
        ):
            command, release = await self._start_member_change(mass, sgp, group_lock, events)
            sgp._schedule_idle_grace_timer()
            grace = sgp._idle_grace_task
            assert grace is not None
            await asyncio.sleep(0.01)

            sgp._cancel_idle_grace_timer()
            assert sgp._idle_grace_task is None
            release.set()
            await asyncio.gather(command, grace, return_exceptions=True)

        assert grace.cancelled()
        dissolve.assert_not_awaited()
        assert sgp.sync_leader is leader
        assert events == ["leader command", "member change done"]


class TestFormWaitsForLeaderUnsynced:
    """The form path must wait for a stale leader to report synced_to=None."""

    async def test_form_waits_when_leader_still_synced(self) -> None:
        """If the new leader still reports synced_to, we wait before proceeding."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        # leader reports stale synced_to
        leader.state.synced_to = "old_leader"
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
        assert sgp.sync_leader is leader

    async def test_form_aborts_when_leader_stuck(self) -> None:
        """If the wait returns False (leader genuinely stuck), abort the form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.synced_to = "old_leader"
        sgp._attr_group_members = ["leader"]

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_wait_member_unsynced", return_value=False),
        ):
            await sgp._form_syncgroup()

        # form must NOT have left a sync_leader set, so the caller's play_media
        # won't be issued against a stuck player (the original Poolhouse race).
        assert sgp.sync_leader is None

    async def test_form_aborts_when_leader_cleared_during_wait(self) -> None:
        """If a concurrent dissolve clears sync_leader during the wait, abort the stale form."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.synced_to = "old_leader"
        _make_member(mass, "m2")
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


class TestFormReconcilesIncompatibleMembers:
    """The form checks the tracked members against the settled leader."""

    async def test_form_drops_member_leader_cannot_group_with(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A member the settled leader can not group with is dropped with a warning."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.logger = logging.getLogger("test.sync_group")
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"m2"}
        _make_member(mass, "m2")
        _make_member(mass, "x", None, audio=["airplay"])
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2", "x"]

        with patch.object(sgp, "update_state"), caplog.at_level(logging.WARNING):
            await sgp._form_syncgroup()

        assert sgp._attr_group_members == ["leader", "m2"]
        assert "Removing x from group" in caplog.text
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_add=["m2"], new_content=False
        )

    async def test_form_keeps_an_unavailable_member(self) -> None:
        """An offline member is absent from the leader's list for that reason only: kept."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = {"m2"}
        _make_member(mass, "m2")
        _make_member(mass, "off", available=False)
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2", "off"]

        with patch.object(sgp, "update_state"):
            await sgp._form_syncgroup()

        assert sgp._attr_group_members == ["leader", "m2", "off"]
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_add=["m2", "off"], new_content=False
        )

    async def test_form_keeps_members_when_leader_reports_nothing(self) -> None:
        """A leader with an empty compatibility list has no say: nothing is dropped."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.can_group_with = set()
        _make_member(mass, "m2")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2"]

        with patch.object(sgp, "update_state"):
            await sgp._form_syncgroup()

        assert sgp._attr_group_members == ["leader", "m2"]
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_add=["m2"], new_content=False
        )

    async def test_form_syncs_only_the_members_the_leader_does_not_hold(self) -> None:
        """The members the leader reports (by player id) are not synced again."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        leader.state.group_members = UniqueList(["leader", "m2"])
        _make_member(mass, "m2")
        _make_member(mass, "m3")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2", "m3"]

        with patch.object(sgp, "update_state"):
            await sgp._form_syncgroup()

        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_add=["m3"], new_content=False
        )


class TestWaitMemberUnsynced:
    """The helper that waits for a member's synced_to to clear (with recovery)."""

    async def test_returns_true_when_already_unsynced(self) -> None:
        """Member already reports synced_to=None → return True without recovery."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        _make_member(mass, "m1")

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        mass.players._handle_set_members.assert_not_awaited()

    @pytest.mark.parametrize("registered", [True, False])
    async def test_does_not_wait_on_a_member_that_is_gone(self, registered: bool) -> None:
        """An offline or unregistered member can not clear its state, so it is not waited on."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        if registered:
            _make_member(mass, "m1", available=False)

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        mass.players.wait_for_player_update.assert_not_called()

    async def test_attempts_recovery_when_stuck(self) -> None:
        """If the first wait doesn't clear it, kick the member from its stale parent directly."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_member(mass, "m1")
        member.state.synced_to = "old_leader"  # still stale after first wait
        old_leader = _make_member(mass, "old_leader")

        # _handle_set_members on the stale parent is the expected recovery action.
        # We make it succeed by side-effect-clearing synced_to.
        async def _kick(_parent: Any, player_ids_to_remove: list[str]) -> None:
            assert player_ids_to_remove == ["m1"]
            member.state.synced_to = None

        mass.players._handle_set_members = AsyncMock(side_effect=_kick)

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        mass.players._handle_set_members.assert_awaited_once_with(
            old_leader, player_ids_to_remove=["m1"]
        )

    async def test_returns_false_when_genuinely_stuck(self) -> None:
        """If recovery doesn't help either, return False so callers can abort."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_member(mass, "m1")
        member.state.synced_to = "old_leader"  # stays stuck after recovery
        old_leader = _make_member(mass, "old_leader")
        mass.players._handle_set_members = AsyncMock()  # no-op: state stays stale

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is False
        mass.players._handle_set_members.assert_awaited_once_with(
            old_leader, player_ids_to_remove=["m1"]
        )

    @pytest.mark.parametrize("parent_registered", [True, False])
    async def test_member_leaves_on_its_own_when_stale_parent_is_offline(
        self, parent_registered: bool
    ) -> None:
        """A stale parent that is gone or offline can't release the member, so it leaves itself."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_member(mass, "m1")
        member.state.synced_to = "old_leader"
        if parent_registered:
            _make_member(mass, "old_leader", available=False)

        async def _leave() -> None:
            member.state.synced_to = None

        own = _protocol(member, "sonos")
        own.ungroup = AsyncMock(side_effect=_leave)  # type: ignore[method-assign]

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is True
        own.ungroup.assert_awaited_once()
        mass.players._handle_set_members.assert_not_awaited()

    async def test_returns_false_when_member_stays_synced_after_leaving(self) -> None:
        """A member that keeps reporting the offline parent after leaving is reported stuck."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_member(mass, "m1")
        member.state.synced_to = "old_leader"
        own = _protocol(member, "sonos")
        own.ungroup = AsyncMock()  # type: ignore[method-assign]  # no-op: state stays stale
        _make_member(mass, "old_leader", available=False)

        ok = await sgp._wait_member_unsynced("m1")

        assert ok is False
        own.ungroup.assert_awaited_once()
        mass.players._handle_set_members.assert_not_awaited()


def _raw_controls_none(_player_id: str, key: str, default: object = None) -> object:
    """Return the stored controls of a group whose leader had no volume when it was set up."""
    if key in (CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL):
        return PLAYER_CONTROL_NONE
    return default


class TestSupportedFeaturesFromActiveMembers:
    """Active groups inherit volume controls from all available current members."""

    def test_volume_member_is_not_hidden_by_a_volume_less_sync_leader(self) -> None:
        """A display-only sync leader must not hide a WiiM-like member's volume."""
        mass = _make_mock_mass()
        # the stored controls resolve to "none", which the final feature set must not
        # apply to a group
        mass.config.get_player_value = MagicMock(side_effect=_raw_controls_none)
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "display", "sendspin")
        # the live session is what the leader reports, not the configured list
        leader.state.group_members = UniqueList(["display", "speaker"])
        leader.state.volume_control = leader.state.mute_control = PLAYER_CONTROL_NONE
        member = _make_member(mass, "speaker")
        member.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        member.state.volume_control = member.state.mute_control = PLAYER_CONTROL_NATIVE
        member.state.volume_level = 40
        member.state.volume_muted = False
        sgp.sync_leader = leader
        sgp._attr_group_members = ["display", "speaker"]

        assert PlayerFeature.VOLUME_SET in sgp.supported_features
        assert PlayerFeature.VOLUME_MUTE in sgp.supported_features
        # PlayerState uses the final feature set, which must preserve the
        # member-derived controls even though the group has no native control.
        group = sgp.player
        assert group is not None
        group.update_state(signal_event=False)
        assert PlayerFeature.VOLUME_SET in group.state.supported_features
        assert PlayerFeature.VOLUME_MUTE in group.state.supported_features
        assert group.state.group_volume == 40

    def test_playback_features_stay_bound_to_the_sync_leader(self) -> None:
        """Enqueue, gapless and DSP come from the leader only; a member cannot add them."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader")
        member = _make_member(mass, "member")
        member.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
            PlayerFeature.MULTI_DEVICE_DSP,
        }
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "member"]

        features = sgp.supported_features
        assert PlayerFeature.ENQUEUE not in features
        assert PlayerFeature.GAPLESS_PLAYBACK not in features
        assert PlayerFeature.MULTI_DEVICE_DSP not in features

        leader.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
            PlayerFeature.MULTI_DEVICE_DSP,
        }
        features = sgp.supported_features
        assert PlayerFeature.ENQUEUE in features
        assert PlayerFeature.GAPLESS_PLAYBACK in features
        assert PlayerFeature.MULTI_DEVICE_DSP in features

    def test_volume_comes_from_the_live_session_members_only(self) -> None:
        """A configured member that did not join the session cannot add volume control."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "display", "sendspin")
        _make_member(mass, "joined", "sendspin")
        absent = _make_member(mass, "absent")
        absent.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}
        sgp.sync_leader = leader
        sgp._attr_group_members = ["display", "joined", "absent"]
        # group volume is fanned out to the leader's live session, which "absent" never joined
        leader.state.group_members = UniqueList(["display", "joined"])

        assert PlayerFeature.VOLUME_SET not in sgp.supported_features

        leader.state.group_members = UniqueList(["display", "joined", "absent"])
        assert PlayerFeature.VOLUME_SET in sgp.supported_features

    def test_dormant_group_derives_all_features_from_members(self) -> None:
        """Without a sync leader every member feature is advertised, as before."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        member = _make_member(mass, "member")
        member.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        sgp.sync_leader = None
        sgp._attr_group_members = ["member"]

        features = sgp.supported_features
        assert PlayerFeature.ENQUEUE in features
        assert PlayerFeature.VOLUME_SET in features
        assert PlayerFeature.VOLUME_MUTE in features


class TestSupportedFeaturesPower:
    """POWER feature is only advertised when the user opts in via Fake control."""

    def test_power_not_in_base_features_by_default(self) -> None:
        """No power_control config → POWER feature not advertised."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        assert PlayerFeature.POWER not in sgp.supported_features

    def test_power_advertised_when_fake_control_assigned(self) -> None:
        """User assigns Fake power control to the group's player → POWER feature shows up."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        stored = {(_group_id(sgp), "power_control"): "fake"}
        mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: stored.get((player_id, key), default)
        )
        assert PlayerFeature.POWER in sgp.supported_features

    def test_power_follows_the_player_not_the_protocol(self) -> None:
        """A power control stored under the protocol id is no power control of the group."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        stored = {(sgp.protocol_id, "power_control"): "fake"}
        mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: stored.get((player_id, key), default)
        )
        assert PlayerFeature.POWER not in sgp.supported_features


class TestLeaderTransportForwarding:
    """Transport commands on a formed group are handled by its sync leader."""

    def _make_group_with_leader(self) -> tuple[SyncGroupPlayer, Player, MagicMock]:
        mass = _make_mock_mass()
        mass.players._handle_cmd_pause = AsyncMock()
        mass.players._handle_cmd_play = AsyncMock()
        mass.players.cmd_resume = AsyncMock()
        leader = _make_member(mass, "leader", playback_state=PlaybackState.PLAYING)
        leader.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.NEXT_PREVIOUS,
        }
        native = _protocol(leader, "sonos")
        for method in ("seek", "next_track", "previous_track", "set_shuffle", "set_repeat"):
            setattr(native, method, AsyncMock())
        sgp = _make_sync_group(mass)
        sgp.sync_leader = leader
        return sgp, leader, mass

    def test_transport_features_follow_the_leader(self) -> None:
        """PAUSE, SEEK and NEXT_PREVIOUS are advertised only when the leader has them."""
        transport = {PlayerFeature.PAUSE, PlayerFeature.SEEK, PlayerFeature.NEXT_PREVIOUS}
        sgp, leader, _mass = self._make_group_with_leader()
        assert transport <= sgp.supported_features

        leader.state.supported_features = {PlayerFeature.PLAY_MEDIA}
        assert not transport & sgp.supported_features

    def test_transport_features_not_advertised_without_leader(self) -> None:
        """An unformed group does not borrow transport features from its members."""
        mass = _make_mock_mass()
        member = _make_member(mass, "member")
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.PAUSE}
        sgp = _make_sync_group(mass)
        sgp._attr_group_members = ["member"]
        assert PlayerFeature.PAUSE not in sgp.supported_features

    async def test_pause_goes_to_the_leader(self) -> None:
        """Pause runs the controller's internal handler on the leader, keeping the group."""
        sgp, leader, mass = self._make_group_with_leader()
        await sgp.pause()
        mass.players._handle_cmd_pause.assert_awaited_once_with(leader.player_id)
        mass.players._handle_cmd_stop.assert_not_awaited()

    async def test_source_commands_go_to_the_leader(self) -> None:
        """Seek, next/previous, shuffle and repeat are forwarded to the leader's device."""
        sgp, leader, _mass = self._make_group_with_leader()
        await sgp.seek(42)
        await sgp.next_track()
        await sgp.previous_track()
        await sgp.set_shuffle(True)
        await sgp.set_repeat(RepeatMode.ALL)
        native: Any = _protocol(leader, "sonos")
        native.seek.assert_awaited_once_with(42)
        native.next_track.assert_awaited_once()
        native.previous_track.assert_awaited_once()
        native.set_shuffle.assert_awaited_once_with(True)
        native.set_repeat.assert_awaited_once_with(RepeatMode.ALL)

    async def test_commands_without_leader_fail(self) -> None:
        """An unformed group refuses transport commands."""
        sgp = _make_sync_group(_make_mock_mass())
        with pytest.raises(PlayerCommandFailed):
            await sgp.pause()
        with pytest.raises(PlayerCommandFailed):
            await sgp.seek(10)

    async def test_play_resumes_the_leaders_own_source(self) -> None:
        """Play on a leader-owned source resumes it on the leader without restreaming."""
        sgp, leader, mass = self._make_group_with_leader()
        native = _protocol(leader, "sonos")
        native._attr_active_source = "spotify"
        native._attr_source_list = [PlayerSource(id="spotify", name="Spotify", passive=False)]
        await sgp.play()
        mass.players._handle_cmd_play.assert_awaited_once_with(leader.player_id)
        mass.players.cmd_resume.assert_not_awaited()

    async def test_play_restreams_group_content(self) -> None:
        """Play on the group's own content still resumes the group, by its player id."""
        sgp, leader, mass = self._make_group_with_leader()
        native = _protocol(leader, "sonos")
        native._attr_active_source = _group_id(sgp)
        native._attr_source_list = [PlayerSource(id="spotify", name="Spotify", passive=False)]
        with patch.object(sgp, "_form_syncgroup", AsyncMock()):
            await sgp.play()
        mass.players._handle_cmd_play.assert_not_awaited()
        mass.players.cmd_resume.assert_awaited_once()
        assert mass.players.cmd_resume.await_args.args[0] == _group_id(sgp)


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

    async def test_play_waits_for_leader_before_returning(self) -> None:
        """play() wraps the resume in a wait so the group lock isn't released early."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader", playback_state=PlaybackState.IDLE)
        sgp._attr_group_members = ["leader"]
        order: list[str] = []
        mass.players.wait_for_player_update = _recording_wait(order)
        mass.players.cmd_resume = AsyncMock(side_effect=lambda *_a, **_k: order.append("resume"))

        with patch.object(sgp, "_form_syncgroup", new=AsyncMock(return_value=None)):
            await sgp.play()

        # subscribe happens before the resume command, and the wait completes
        # after — i.e. the start is wrapped, not fire-and-forget.
        assert order == [
            f"wait_for:leader:{PlaybackState.PLAYING}",
            "subscribe",
            "resume",
            "await",
        ]

    async def test_play_media_waits_for_leader_before_returning(self) -> None:
        """play_media() wraps the leader start so a concurrent (un)group can't race it."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader", playback_state=PlaybackState.IDLE)
        sgp._attr_group_members = ["leader"]
        order: list[str] = []
        mass.players.wait_for_player_update = _recording_wait(order)
        mass.players._handle_play_media = AsyncMock(
            side_effect=lambda *_a, **_k: order.append("play_media")
        )
        media = MagicMock()
        media.source_id = _group_id(sgp)

        with patch.object(sgp, "_form_syncgroup", new=AsyncMock(return_value=None)):
            await sgp.play_media(media)

        assert order == [
            f"wait_for:leader:{PlaybackState.PLAYING}",
            "subscribe",
            "play_media",
            "await",
        ]
        mass.players.get_player_lock.assert_any_call("leader", PlayerLockPurpose.PLAYBACK)

    async def test_no_wait_when_group_has_no_leader(self) -> None:
        """With no leader to wait on, play() resumes without arming a playback wait."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = None
        mass.players.cmd_resume = AsyncMock()

        with patch.object(sgp, "_form_syncgroup", new=AsyncMock(return_value=None)):
            await sgp.play()

        mass.players.wait_for_player_update.assert_not_called()
        mass.players.cmd_resume.assert_awaited_once()


class TestPlaybackStartMarker:
    """A just-issued playback start counts as playing for group-command decisions."""

    def _setup_group(self, mass: MagicMock, group_state: PlaybackState) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        # a provider without dynamic leader switching, so leader removal takes the
        # dissolve+reform path
        sgp.sync_leader = _make_member(mass, "leader", "wiim")
        _make_member(mass, "other", "wiim")
        sgp._attr_group_members = ["leader", "other"]
        sgp._attr_playback_state = group_state
        # close scheduled background coroutines (idle-grace) instead of leaking them
        mass.create_task = MagicMock(side_effect=lambda coro, *_a, **_k: coro.close())
        return sgp

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

    async def test_await_leader_playback_stamps_the_marker(self) -> None:
        """The playback wait stamps the start so decision logic can see the window."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader")
        assert sgp._playback_recently_started is False

        async with sgp._await_leader_playback():
            pass

        assert sgp._playback_recently_started is True

    async def test_stop_voids_the_marker(self) -> None:
        """An explicit stop clears the marker so a later unjoin won't resume."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(mass, "leader")
        sgp._playback_start_at = time.monotonic()

        with patch.object(sgp, "update_state"):
            await sgp.stop()

        assert sgp._playback_recently_started is False


class TestDebouncedReform:
    """Leader removal re-forms debounced so cascaded unjoins coalesce."""

    def _setup_group(self, mass: MagicMock, members: list[str]) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        players = [_make_member(mass, member_id, "wiim") for member_id in members]
        sgp.sync_leader = players[0]
        sgp._attr_group_members = list(members)
        sgp._attr_playback_state = PlaybackState.PLAYING
        # run scheduled coroutines for real so the debounced re-form actually fires;
        # eager_start mirrors mass.create_task so the runner enters its sleep at
        # schedule time (a later cancel then always lands inside the body)
        _run_scheduled_tasks(mass)
        return sgp

    @staticmethod
    def _member(mass: MagicMock, player_id: str) -> Player:
        player = mass.players.get_player(player_id)
        assert player is not None
        return player  # type: ignore[no-any-return]

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

    async def test_unavailable_leader_removal_still_reforms(self) -> None:
        """A leader that dropped off the network can't be stopped, yet the group re-forms."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2", "m3"])
        mass.players.cmd_resume = AsyncMock()
        leader = self._member(mass, "leader")
        _set_reachable(leader, False)
        leader.state.available = False
        leader.state.group_members = UniqueList(["leader", "m2", "m3"])
        self._member(mass, "m2").state.can_group_with = {"m3"}

        def _fail_for_leader(player_id: str) -> None:
            if player_id == "leader":
                raise PlayerUnavailableError(player_id)

        mass.players._handle_cmd_stop.side_effect = _fail_for_leader
        mass.players._handle_set_members.side_effect = lambda parent, **_k: _fail_for_leader(
            parent.player_id
        )

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

            mass.players._handle_cmd_stop.assert_any_await("leader")
            assert sgp.sync_leader is None
            assert sgp._attr_group_members == ["m2", "m3"]
            task = sgp._reform_task
            assert task is not None

            await task

        # read through getattr: mypy can't see that the re-form set the leader again
        new_leader = getattr(sgp, "sync_leader")  # noqa: B009
        assert new_leader is self._member(mass, "m2")
        mass.players._handle_set_members.assert_any_await(
            new_leader, player_ids_to_add=["m3"], new_content=False
        )
        mass.players.cmd_resume.assert_awaited_once()
        assert sgp._reform_task is None

    async def test_reform_does_not_wait_on_a_member_that_is_gone(self) -> None:
        """A member the controller no longer knows can not delay the re-form by its unsync wait."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        mass.players.cmd_resume = AsyncMock()
        # recorded as a member, but no longer registered with the controller
        sgp._attr_group_members.append("gone")

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None
            await task

        assert sgp.sync_leader is self._member(mass, "m2")
        assert sgp._attr_group_members == ["m2", "gone"]
        waited_on = {call.args[0] for call in mass.players.wait_for_player_update.call_args_list}
        assert "gone" not in waited_on
        mass.players.cmd_resume.assert_awaited_once()

    async def test_reform_with_no_available_member_keeps_them_for_a_later_play(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A re-form with only offline members left logs why and does not try to resume."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        sgp.logger = logging.getLogger("test.sync_group")
        mass.players.cmd_resume = AsyncMock()
        offline = self._member(mass, "m2")
        _set_reachable(offline, False)
        offline.state.available = False

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            caplog.at_level(logging.INFO),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None
            await task

        assert "none of its members can lead right now" in caplog.text
        assert sgp.sync_leader is None
        assert sgp._attr_group_members == ["m2"]
        assert sgp._reform_task is None
        mass.players.cmd_resume.assert_not_awaited()
        mass.players._handle_play_media.assert_not_awaited()

    def _setup_offline_leader_with_stale_members(
        self, mass: MagicMock
    ) -> tuple[SyncGroupPlayer, list[Player]]:
        """Group whose leader went offline while its members keep reporting it as their leader."""
        sgp = self._setup_group(mass, members=["leader", "m2", "m3"])
        mass.players.cmd_resume = AsyncMock()
        leader = self._member(mass, "leader")
        _set_reachable(leader, False)
        leader.state.available = False
        leader.state.group_members = UniqueList(["leader", "m2", "m3"])
        members = [self._member(mass, "m2"), self._member(mass, "m3")]
        members[0].state.can_group_with = {"m3"}
        for member in members:
            member.state.synced_to = "leader"

        def _fail_for_leader(player_id: str) -> None:
            if player_id == "leader":
                raise PlayerUnavailableError(player_id)

        mass.players._handle_cmd_stop.side_effect = _fail_for_leader
        mass.players._handle_set_members.side_effect = lambda parent, **_k: _fail_for_leader(
            parent.player_id
        )
        return sgp, members

    async def test_reform_releases_members_stuck_on_the_offline_leader(self) -> None:
        """Members still synced to the offline leader leave on their own, then the group re-forms."""
        mass = _make_mock_mass()
        sgp, members = self._setup_offline_leader_with_stale_members(mass)
        for member in members:

            async def _leave(member: Player = member) -> None:
                member.state.synced_to = None

            _protocol(member, "wiim").ungroup = AsyncMock(side_effect=_leave)  # type: ignore[method-assign]

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None
            await task

        for member in members:
            ungroup: Any = _protocol(member, "wiim").ungroup
            ungroup.assert_awaited_once()
        assert sgp.sync_leader is members[0]
        mass.players.cmd_resume.assert_awaited_once()

    async def test_reform_aborts_when_members_stay_stuck_on_the_offline_leader(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A member that keeps reporting the offline leader after leaving aborts the re-form."""
        mass = _make_mock_mass()
        sgp, members = self._setup_offline_leader_with_stale_members(mass)
        sgp.logger = logging.getLogger("test.sync_group")
        for member in members:
            # no-op: state stays stale
            _protocol(member, "wiim").ungroup = AsyncMock()  # type: ignore[method-assign]

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            caplog.at_level(logging.ERROR),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None
            await task

        assert "still report synced_to after recovery attempts" in caplog.text
        assert sgp.sync_leader is None
        mass.players.cmd_resume.assert_not_awaited()

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

    async def test_reform_aborts_when_all_members_left(self) -> None:
        """When the window drains the whole group, the pending re-form is dropped at once."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            pending = sgp._reform_task
            assert pending is not None
            await sgp.set_members(player_ids_to_remove=["m2"])

            # no need to wait out the debounce window: an empty group has nothing
            # to re-form for, so the members are released immediately
            assert sgp._reform_task is None
            assert sgp.is_active_session is False

            await asyncio.gather(pending, return_exceptions=True)

        play.assert_not_awaited()

    async def test_a_skipped_reform_logs_why(self, caplog: pytest.LogCaptureFixture) -> None:
        """A re-form that finds nothing to do says so instead of returning silently."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        sgp.logger = logging.getLogger("test.sync_group")

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
            caplog.at_level(logging.DEBUG),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            task = sgp._reform_task
            assert task is not None
            # the group was re-formed by something else while the window was running
            sgp.sync_leader = self._member(mass, "m2")

            await task

        play.assert_not_awaited()
        assert "the group was re-formed meanwhile" in caplog.text

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

    async def test_join_after_dissolve_registers_stale_member(self) -> None:
        """A player re-joining while it still reports the dissolved group is registered."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        mass.players.cmd_resume = AsyncMock()
        m2 = self._member(mass, "m2")

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            # the leader is powered off and the remaining member leaves right after,
            # which drops the pending re-form: an empty group has nothing to resume
            await sgp.set_members(player_ids_to_remove=["leader"])
            await sgp.set_members(player_ids_to_remove=["m2"])
            assert sgp._attr_group_members == []
            assert sgp._reform_task is None

            # m2 still reports the old sync state (Sonos propagates group state async)
            m2.state.synced_to = "leader"
            m2.state.can_group_with = set()
            m2.state.active_group = _group_id(sgp)

            # the controller only forwards a join when the group offers the player
            assert "m2" in sgp.can_group_with
            await sgp.set_members(player_ids_to_add=["m2"])
            assert sgp._attr_group_members == ["m2"]

            # the sync state settles, then the next play forms the group with it
            m2.state.synced_to = None
            await sgp.play()

        mass.players.cmd_resume.assert_awaited_once()
        assert sgp.sync_leader is m2

    async def test_play_media_after_join_following_dissolve_forms_the_group(self) -> None:
        """Starting playback right after such a join forms the group instead of finding it empty."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2"])
        m2 = self._member(mass, "m2")

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])
            await sgp.set_members(player_ids_to_remove=["m2"])

            m2.state.synced_to = "leader"
            m2.state.can_group_with = set()
            m2.state.active_group = _group_id(sgp)

            assert "m2" in sgp.can_group_with
            await sgp.set_members(player_ids_to_add=["m2"])

            # the sync state settles, and a user play supersedes the pending re-form
            m2.state.synced_to = None
            sgp._cancel_reform_timer()
            await sgp.play_media(MagicMock(source_id="src", uri="x"))

        assert sgp.sync_leader is m2
        mass.players._handle_play_media.assert_awaited_once()

    async def test_second_join_after_dissolve_is_registered_too(self) -> None:
        """
        Two speakers re-joining in the same window both end up in the group.

        Regression: the first (still slaved) re-joiner was picked as prospective
        leader and reports no compatible players at all, so the second speaker
        was silently dropped as "incompatible".
        """
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, members=["leader", "m2", "m3"])
        mass.players.cmd_resume = AsyncMock()
        m2 = self._member(mass, "m2")
        m3 = self._member(mass, "m3")

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
        ):
            # the leader is powered off and the other members leave right after
            await sgp.set_members(player_ids_to_remove=["leader"])
            await sgp.set_members(player_ids_to_remove=["m2"])
            await sgp.set_members(player_ids_to_remove=["m3"])
            assert sgp._attr_group_members == []

            # both still report the old sync state (Sonos propagates group state async)
            for member in (m2, m3):
                member.state.synced_to = "leader"
                member.state.can_group_with = set()
                member.state.active_group = _group_id(sgp)

            # the controller only forwards a join when the group offers the player
            assert "m2" in sgp.can_group_with
            await sgp.set_members(player_ids_to_add=["m2"])
            assert "m3" in sgp.can_group_with
            await sgp.set_members(player_ids_to_add=["m3"])
            assert sgp._attr_group_members == ["m2", "m3"]

            # the sync state settles, then the next play forms the group with both
            for member in (m2, m3):
                member.state.synced_to = None
            await sgp.play()

        mass.players.cmd_resume.assert_awaited_once()
        assert sgp.sync_leader is m2
        assert any(
            call.kwargs.get("player_ids_to_add") == ["m3"]
            for call in mass.players._handle_set_members.await_args_list
        )


def _lock_with_side_effect(on_enter: Callable[[], None]) -> MagicMock:
    """Build a get_player_lock mock that mutates state while the lock is being awaited."""
    ctx = AsyncMock()

    async def _enter(*_args: Any) -> None:
        on_enter()

    ctx.__aenter__.side_effect = _enter
    ctx.__aexit__.return_value = False
    return MagicMock(return_value=ctx)


class TestLeaderChangedWhileWaitingForItsLock:
    """A member change is dropped when the leader it was meant for is gone."""

    def _setup_group(self, mass: MagicMock) -> tuple[SyncGroupPlayer, Player]:
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "wiim")
        leader.state.can_group_with = {"m2", "m3"}
        _make_member(mass, "m2", "wiim")
        _make_member(mass, "m3", "wiim")
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader", "m2"]
        sgp._attr_playback_state = PlaybackState.PLAYING
        return sgp, leader

    async def test_member_change_is_dropped_when_the_leader_was_dissolved(self) -> None:
        """A leader that dissolved while we waited for its lock is not commanded."""
        mass = _make_mock_mass()
        sgp, _ = self._setup_group(mass)
        sgp._reform_task = None
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(sgp, "sync_leader", None)
        )

        with patch.object(sgp, "update_state"):
            await sgp.set_members(player_ids_to_add=["m3"])

        mass.players._handle_set_members.assert_not_awaited()
        # the member is recorded, so the next form picks it up
        assert "m3" in sgp._attr_group_members
        assert sgp._reform_task is None

    async def test_leader_swapped_under_the_lock_is_not_commanded(self) -> None:
        """A leader that was replaced while we waited for its lock is not commanded either."""
        mass = _make_mock_mass()
        sgp, _ = self._setup_group(mass)
        sgp._reform_task = MagicMock()  # a re-form is pending
        new_leader = mass.players.get_player("m2")
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(sgp, "sync_leader", new_leader)
        )

        with (
            patch.object(sgp, "update_state"),
            patch.object(sgp, "_schedule_reform_timer") as schedule_reform,
        ):
            await sgp.set_members(player_ids_to_add=["m3"])

        mass.players._handle_set_members.assert_not_awaited()
        assert sgp.sync_leader is new_leader
        # a re-form only runs on a leaderless group, so re-arming it would be a no-op
        schedule_reform.assert_not_called()

    async def test_a_pending_reform_is_rearmed_for_the_final_member_list(self) -> None:
        """The pending re-form is the retry: it re-forms with the newly added member."""
        mass = _make_mock_mass()
        sgp, _ = self._setup_group(mass)
        _run_scheduled_tasks(mass)
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(sgp, "sync_leader", None)
        )

        with (
            patch("music_assistant.providers.sync_group.player.REFORM_DEBOUNCE_SECONDS", 0.05),
            patch.object(sgp, "update_state"),
            patch.object(sgp, "play", new=AsyncMock()) as play,
        ):
            sgp._schedule_reform_timer()
            pending = sgp._reform_task
            assert pending is not None

            await sgp.set_members(player_ids_to_add=["m3"])

            rearmed = sgp._reform_task
            assert rearmed is not None
            assert rearmed is not pending

            await asyncio.gather(pending, rearmed, return_exceptions=True)

        play.assert_awaited_once()
        assert "m3" in sgp._attr_group_members


class TestReformResumeGate:
    """Removing the sync leader resumes playback if (and only if) the group was playing."""

    def _setup_group(self, mass: MagicMock, group_state: PlaybackState) -> SyncGroupPlayer:
        sgp = _make_sync_group(mass)
        # a provider without dynamic leader switching, so leader removal takes the
        # dissolve+reform path
        sgp.sync_leader = _make_member(mass, "leader", "wiim")
        _make_member(mass, "other", "wiim")
        sgp._attr_group_members = ["leader", "other"]
        sgp._attr_playback_state = group_state
        # close scheduled background coroutines (idle-grace) instead of leaking them
        mass.create_task = MagicMock(side_effect=lambda coro, *_a, **_k: coro.close())
        return sgp

    async def test_leader_removal_resumes_when_playing(self) -> None:
        """Leader removal on a playing group must reform with a resume."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.PLAYING)

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()) as dyn_switch,
            patch.object(sgp, "_active_session_protocol", return_value=None),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        dyn_switch.assert_not_awaited()
        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is True

    async def test_leader_removal_does_not_resume_when_idle(self) -> None:
        """A genuinely idle group must not spuriously start playing on leader removal."""
        mass = _make_mock_mass()
        sgp = self._setup_group(mass, group_state=PlaybackState.IDLE)

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()),
            patch.object(sgp, "_active_session_protocol", return_value=None),
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        reform.assert_awaited_once()
        assert reform.await_args is not None
        assert reform.await_args.kwargs.get("resume_playback") is False

    async def test_leader_removal_hands_off_on_a_protocol_that_can_switch_leaders(self) -> None:
        """A playing group on a protocol that can switch leaders hands off instead."""
        mass = _make_mock_mass()
        sgp = _make_sync_group(mass)
        sgp.sync_leader = _make_member(
            mass, "leader", "sonos", audio=["airplay"], active_output="airplay"
        )
        _make_member(mass, "other", "sonos", audio=["airplay"])
        sgp._attr_group_members = ["leader", "other"]
        sgp._attr_playback_state = PlaybackState.PLAYING

        with (
            patch.object(sgp, "_dissolve_and_reform", new=AsyncMock()) as reform,
            patch.object(sgp, "_dynamic_leader_switch", new=AsyncMock()) as dyn_switch,
        ):
            await sgp.set_members(player_ids_to_remove=["leader"])

        dyn_switch.assert_awaited_once_with("leader")
        reform.assert_not_awaited()


def _static_group_with_a_member_away(
    mass: MagicMock,
) -> tuple[SyncGroupPlayer, Player, Player]:
    """Return a playing static group whose leader holds only itself; its other member is back."""
    sgp = _make_sync_group(mass, dynamic=False)
    leader = _make_member(mass, "leader", "sendspin")
    display = _make_member(mass, "display", "sendspin")
    leader.state.group_members = UniqueList(["leader"])
    leader.state.can_group_with = {"display"}
    sgp._attr_static_group_members = ["leader", "display"]
    sgp._attr_group_members = ["leader", "display"]
    sgp.sync_leader = leader
    return sgp, leader, display


class TestStaticMemberRejoin:
    """Test re-adding the configured members that return while a static group plays."""

    def test_a_returning_member_is_rejoined_after_a_debounce(self) -> None:
        """A configured member the leader can group with but does not hold is re-added."""
        mass = _make_mock_mass()
        sgp, leader, _display = _static_group_with_a_member_away(mass)

        sgp.on_group_member_updated(
            leader, {"can_group_with": (frozenset(), frozenset({"display"}))}
        )

        mass.call_later.assert_called_once_with(
            REFORM_DEBOUNCE_SECONDS,
            sgp._rejoin_members,
            task_id="sync_group_rejoin_syncgroup_test",
        )

    async def test_rejoin_adds_the_members_to_the_live_leader(self) -> None:
        """The re-add goes to the leader under the group's lock, leaving playback alone."""
        mass = _make_mock_mass()
        sgp, leader, _display = _static_group_with_a_member_away(mass)
        locks = MagicMock()
        locks.attach_mock(mass.players.get_player_lock_if_free, "if_free")
        locks.attach_mock(mass.players.get_player_lock, "waiting")

        await sgp._rejoin_members()

        assert [
            (name, args) for name, args, _ in locks.mock_calls if name in ("if_free", "waiting")
        ] == [
            ("if_free", (_group_id(sgp),)),
            ("waiting", ("leader", PlayerLockPurpose.PLAYBACK)),
            ("if_free", ("display",)),
        ]
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_add=["display"]
        )
        mass.players._handle_play_media.assert_not_awaited()

    def test_members_the_leader_holds_are_not_rejoined(self) -> None:
        """A leader update with every member in place schedules nothing."""
        mass = _make_mock_mass()
        sgp, leader, _display = _static_group_with_a_member_away(mass)
        leader.state.group_members = UniqueList(["leader", "display"])

        sgp.on_group_member_updated(leader, {"volume_level": (10, 20)})

        mass.call_later.assert_not_called()

    def test_a_member_the_leader_can_not_group_with_is_not_rejoined(self) -> None:
        """A member that is offline or incompatible is not on the leader's list, and left be."""
        mass = _make_mock_mass()
        sgp, leader, _display = _static_group_with_a_member_away(mass)
        leader.state.can_group_with = set()

        sgp.on_group_member_updated(leader, {"volume_level": (10, 20)})

        mass.call_later.assert_not_called()

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("synced_to", "other_leader"),
            ("active_group", "other_group"),
            ("group_members", ["display", "child"]),
        ],
    )
    def test_a_member_held_elsewhere_is_not_rejoined(self, attribute: str, value: Any) -> None:
        """A member another group holds, or that leads a group of its own, is not stolen."""
        mass = _make_mock_mass()
        sgp, leader, display = _static_group_with_a_member_away(mass)
        setattr(display.state, attribute, value)

        sgp.on_group_member_updated(leader, {"volume_level": (10, 20)})

        mass.call_later.assert_not_called()

    def test_a_member_this_group_holds_is_rejoined(self) -> None:
        """A member that names this group as its active group is still recovered."""
        mass = _make_mock_mass()
        sgp, leader, display = _static_group_with_a_member_away(mass)
        display.state.active_group = _group_id(sgp)

        sgp.on_group_member_updated(leader, {"volume_level": (10, 20)})

        mass.call_later.assert_called_once()

    def test_a_dynamic_or_leaderless_group_does_not_rejoin(self) -> None:
        """Only a static group with a live leader has configured members to recover."""
        mass = _make_mock_mass()
        sgp, leader, _display = _static_group_with_a_member_away(mass)
        sgp.sync_leader = None
        sgp.on_group_member_updated(leader, {"volume_level": (10, 20)})
        mass.call_later.assert_not_called()

        dynamic = _make_sync_group(mass, "syncgroup_dynamic")
        dynamic._attr_static_group_members = ["leader", "display"]
        dynamic._attr_group_members = ["leader", "display"]
        dynamic.sync_leader = leader
        dynamic.on_group_member_updated(leader, {"volume_level": (10, 20)})
        mass.call_later.assert_not_called()

    async def test_rejoin_is_dropped_once_the_leader_is_gone(self) -> None:
        """A re-add scheduled for a leader that changed or left meanwhile is not sent."""
        mass = _make_mock_mass()
        sgp, _leader, _display = _static_group_with_a_member_away(mass)
        other = _make_member(mass, "other", "sendspin")
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(sgp, "sync_leader", other)
        )
        await sgp._rejoin_members()
        mass.players._handle_set_members.assert_not_awaited()

        sgp.sync_leader = None
        await sgp._rejoin_members()
        mass.players._handle_set_members.assert_not_awaited()

    async def test_a_member_grouped_elsewhere_meanwhile_is_not_rejoined(self) -> None:
        """A member that joined another group while the re-add was pending is not taken back."""
        mass = _make_mock_mass()
        sgp, _leader, display = _static_group_with_a_member_away(mass)
        # the user started that other group between the leader's update and the debounce
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(display.state, "synced_to", "other_leader")
        )

        await sgp._rejoin_members()

        mass.players._handle_set_members.assert_not_awaited()

    async def test_a_rejoined_member_is_tracked_for_the_next_formation(self) -> None:
        """A member a form dropped as incompatible at the time is tracked again."""
        mass = _make_mock_mass()
        sgp, _leader, _display = _static_group_with_a_member_away(mass)
        sgp._attr_group_members = ["leader"]

        await sgp._rejoin_members()

        assert sgp._attr_group_members == ["leader", "display"]

    async def test_a_member_removed_from_the_group_meanwhile_is_not_rejoined(self) -> None:
        """A member the group was saved without while the re-add was pending is left out."""
        mass = _make_mock_mass()
        sgp, _leader, _display = _static_group_with_a_member_away(mass)
        sgp._attr_static_group_members = ["leader"]
        sgp._attr_group_members = ["leader"]

        await sgp._rejoin_members()

        mass.players._handle_set_members.assert_not_awaited()
        assert sgp._attr_group_members == ["leader"]

    @pytest.mark.parametrize("error", [PlayerCommandFailed("refused"), ResourceBusyError("busy")])
    async def test_a_refused_rejoin_is_logged_and_left_to_the_next_update(
        self, error: Exception
    ) -> None:
        """A leader that refuses the add does not fail the group; the next update retries."""
        mass = _make_mock_mass()
        sgp, _leader, _display = _static_group_with_a_member_away(mass)
        sgp._attr_group_members = ["leader"]
        mass.players._handle_set_members = AsyncMock(side_effect=error)

        await sgp._rejoin_members()

        assert sgp._attr_group_members == ["leader"]

    async def test_a_busy_leader_is_left_to_the_next_update(self) -> None:
        """A leader that stays busy does not fail the group, and nothing is sent to it."""
        mass = _make_mock_mass()
        sgp, _leader, _display = _static_group_with_a_member_away(mass)
        sgp._attr_group_members = ["leader"]
        mass.players.get_player_lock = _busy_lock()

        await sgp._rejoin_members()

        mass.players._handle_set_members.assert_not_awaited()
        assert sgp._attr_group_members == ["leader"]


class TestNextTrackHandoverLocksTheLeader:
    """The group's next-track handover commands the leader from under the leader's lock."""

    def _setup_group(self, mass: MagicMock) -> tuple[SyncGroupPlayer, Player]:
        sgp = _make_sync_group(mass)
        leader = _make_member(mass, "leader", "wiim")
        leader.state.supported_features = {PlayerFeature.ENQUEUE}
        mass.players._handle_enqueue_next_media = AsyncMock()
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        return sgp, leader

    async def test_the_leader_is_enqueued_under_its_lock(self) -> None:
        """The enqueue runs between taking and releasing the leader's playback lock."""
        mass = _make_mock_mass()
        sgp, _ = self._setup_group(mass)
        events: list[str] = []
        mass.players.get_player_lock = _recording_lock(events)
        mass.players._handle_enqueue_next_media = AsyncMock(
            side_effect=lambda *_args: events.append("enqueue")
        )
        media = MagicMock()

        await sgp.enqueue_next_media(media)

        mass.players._handle_enqueue_next_media.assert_awaited_once_with("leader", media)
        assert events == ["lock:leader", "enqueue", "unlock:leader"]
        mass.players.get_player_lock.assert_called_once_with("leader", PlayerLockPurpose.PLAYBACK)

    async def test_a_leader_swapped_while_waiting_is_not_enqueued(self) -> None:
        """A handover meant for a leader that was replaced while waiting is dropped."""
        mass = _make_mock_mass()
        sgp, _ = self._setup_group(mass)
        new_leader = _make_member(mass, "m2", "wiim")
        mass.players.get_player_lock = _lock_with_side_effect(
            lambda: setattr(sgp, "sync_leader", new_leader)
        )

        await sgp.enqueue_next_media(MagicMock())

        mass.players._handle_enqueue_next_media.assert_not_awaited()


class TestProtocolLookups:
    """The group reaches the protocols of its members through their players."""

    @staticmethod
    def _setup() -> tuple[SyncGroupPlayer, Player]:
        """Return a group whose leader plays on its AirPlay protocol."""
        mass = _make_mock_mass()
        leader = _make_member(mass, "leader", "sonos", audio=["airplay"], active_output="airplay")
        sgp = _make_sync_group(mass)
        sgp.sync_leader = leader
        sgp._attr_group_members = ["leader"]
        return sgp, leader

    def test_the_session_is_the_protocol_of_the_active_output(self) -> None:
        """The live session is owned by the protocol the leader plays on."""
        sgp, leader = self._setup()

        assert sgp._active_session_protocol() is _protocol(leader, "airplay")

    def test_the_session_of_a_leader_without_active_output_is_its_device(self) -> None:
        """Without an active output the session is the protocol of the leader's device."""
        sgp, leader = self._setup()
        leader._active_output_protocol = None
        assert sgp._active_session_protocol() is _protocol(leader, "sonos")

        mass = cast("MagicMock", sgp.mass)
        audio_only = _make_member(mass, "audio_only", None, audio=["airplay", "sendspin"])
        sgp.sync_leader = audio_only
        assert sgp._active_session_protocol() is _protocol(audio_only, "airplay")

    def test_the_session_target_of_a_member_is_its_protocol_of_the_domain(self) -> None:
        """A member is reached on a protocol domain through its protocol of that domain."""
        sgp, leader = self._setup()

        assert sgp._resolve_session_target(leader, "airplay") is _protocol(leader, "airplay")
        assert sgp._resolve_session_target(leader, "sonos") is _protocol(leader, "sonos")
        assert sgp._resolve_session_target(leader, None) is _protocol(leader, "sonos")
        assert sgp._resolve_session_target(leader, "chromecast") is None

    def test_protocol_ids_translate_to_the_player_they_belong_to(self) -> None:
        """A protocol id becomes the id of its player, any other id passes unchanged."""
        sgp, _leader = self._setup()

        assert sgp._to_player_ids(["airplay_leader", "member", "leader", "sonos_leader"]) == [
            "leader",
            "member",
        ]

    def test_the_source_of_the_active_output_protocol_is_no_source_of_the_leader(self) -> None:
        """A source the leader reports that is its own output protocol does not count."""
        sgp, leader = self._setup()
        _protocol(leader, "sonos")._attr_active_source = "AirPlay"

        assert sgp.active_source is None

    def test_the_source_the_leader_plays_itself_is_the_source_of_the_group(self) -> None:
        """A source the leader's device plays on its own is reported for the group."""
        sgp, leader = self._setup()
        leader._active_output_protocol = None
        native = _protocol(leader, "sonos")
        native._attr_active_source = "spotify"
        native._attr_source_list = [PlayerSource(id="spotify", name="Spotify", passive=False)]

        assert sgp.active_source == "spotify"
        assert sgp.source_list == native.source_list
