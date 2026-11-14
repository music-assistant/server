"""
Tests for the Universal Group Player lifecycle.

Mirrors the structure of ``tests/providers/sync_group/test_sync_group.py`` but covers the
multicast-stream variant (Universal Group). The new lifecycle replaces the
mandatory power control with form-on-play, dissolve-on-stop, and a debounced
idle grace window; these tests pin that behavior in place.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import HttpVersion, HttpVersion10, HttpVersion11, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.dsp import AudioChannel, DSPConfig
from music_assistant_models.enums import (
    ContentType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import (
    CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH,
    CONF_GROUP_MEMBERS,
    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_REQUIRES_FIXED_CONTENT_LENGTH,
)
from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import locks as players_locks
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_processing import AudioOutputPlan
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.providers.universal_group.player import UniversalGroupPlayer
from tests.common import (
    MockProvider,
    attach_mock,
    make_player_state,
    mock_player_mass,
    record_lock_waits,
)

GROUP_PROTOCOL_ID = "ugp_test"

# the lock budget of the tests that wait for a real player lock, in seconds
LOCK_BUDGET = 0.25
# a group created after the player split: its player has an id of its own
GROUP_PLAYER_ID = "pl_ugp_test"


def _noop_context() -> AsyncMock:
    """Return an async context manager that does nothing and lets exceptions through."""
    ctx = AsyncMock()
    ctx.__aenter__.return_value = None
    ctx.__aexit__.return_value = False
    return ctx


def _make_mock_mass() -> MagicMock:
    """Create a mocked MusicAssistant whose players controller looks players up for real."""
    mass = mock_player_mass()
    players = mass.players
    players._handle_cmd_stop = AsyncMock()
    players._handle_play_media = AsyncMock()
    players._handle_set_members = AsyncMock()
    players.cmd_power = AsyncMock()
    players._handle_cmd_power = AsyncMock()
    players.iter_group_members = MagicMock(return_value=[])
    players.get_player_lock = MagicMock(return_value=_noop_context())
    players.get_group_and_player_lock = MagicMock(return_value=_noop_context())
    players.wait_for_player_update = MagicMock(return_value=_noop_context())
    # the real routing: through the sync leader, or from the member's own side
    players._detach_member = partial(PlayerController._detach_member, players)
    mass.streams = MagicMock()
    mass.streams.base_url = "http://test"
    mass.streams.register_dynamic_route = MagicMock(return_value=lambda: None)
    mass.streams.get_stream = MagicMock(return_value=MagicMock())

    # mass.create_task must return a real asyncio.Task so callers that do
    # `await asyncio.wait([task])` (TaskManager.__aexit__) don't hang on a
    # MagicMock that pretends to be a task. We wrap coroutines into real
    # tasks; the underlying mocked methods (_handle_play_media, etc.)
    # resolve immediately, so the tasks complete fast.
    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        if asyncio.iscoroutine(target):
            return asyncio.ensure_future(target)

        # not a coroutine - return a finished task so callers can still
        # treat it as awaitable / cancellable.
        async def _noop() -> None:
            return None

        return asyncio.ensure_future(_noop())

    mass.create_task = MagicMock(side_effect=_create_task)
    return mass


def _make_ugp(
    mass: MagicMock, *, dynamic: bool = False, members: list[str] | None = None
) -> UniversalGroupPlayer:
    """
    Create a UniversalGroupPlayer, attached to a player whose id differs from the protocol id.

    :param dynamic: Whether the group allows changing its members.
    :param members: The configured members.
    """
    provider = MockProvider("universal_group", instance_id="universal_group", mass=mass)
    values: dict[str, Any] = {"group_members": list(members or []), "dynamic_members": dynamic}
    mass.config.get_base_protocol_config.return_value = MagicMock(
        default_name="Test UGP",
        get_value=lambda key, default=None: values.get(key, default),
    )
    ugp = UniversalGroupPlayer(provider, GROUP_PROTOCOL_ID)  # type: ignore[arg-type]
    player = Player(
        mass,
        GROUP_PLAYER_ID,
        PlayerConfig(
            values={},
            provider=provider.instance_id,
            player_id=GROUP_PLAYER_ID,
            default_name="Test UGP",
            player_type=PlayerType.GROUP,
        ),
    )
    mass.players._players[GROUP_PLAYER_ID] = player
    mass.players._protocols[GROUP_PROTOCOL_ID] = ugp
    player.add_protocol(ugp, ProtocolRole.VIRTUAL)
    ugp.set_player(player)
    player.set_initialized()
    ugp._cache.clear()
    return ugp


def _make_member(
    mass: MagicMock,
    player_id: str,
    *,
    powered: bool = True,
    available: bool = True,
    active_group: str | None = None,
    synced_to: str | None = None,
    playback_state: PlaybackState = PlaybackState.IDLE,
    player_type: PlayerType = PlayerType.PLAYER,
) -> Player:
    """Create a member player with a native protocol that can power its device."""
    if not isinstance(provider := getattr(mass, "member_provider", None), MockProvider):
        provider = mass.member_provider = MockProvider("sonos", instance_id="sonos", mass=mass)
    player, protocol = attach_mock(
        provider, f"sonos_{player_id}", player_id, player_id=player_id, player_type=player_type
    )
    protocol._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.POWER}
    protocol._attr_available = available
    player._cache.clear()
    player._state = make_player_state(
        player_id,
        type=player_type,
        available=available,
        powered=powered,
        active_group=active_group,
        synced_to=synced_to,
        playback_state=playback_state,
        supported_features={PlayerFeature.PLAY_MEDIA},
        power_control=player._power_control,
        output_protocols=player._output_protocols,
    )
    return player


def _group_members_of(mass: MagicMock, *members: Player) -> None:
    """Let the players controller report the given members as those of the group."""
    mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter(members)


async def test_config_members_exclude_groups_and_unknown_players() -> None:
    """Only known non-group player types should be offered as universal group members."""
    mass = _make_mock_mass()
    ugp = _make_ugp(mass)
    players = [
        _make_member(mass, "speaker"),
        _make_member(mass, "light", player_type=PlayerType.LIGHT),
        _make_member(mass, "visualizer", player_type=PlayerType.VISUALIZER),
        _make_member(mass, "display", player_type=PlayerType.DISPLAY),
        _make_member(mass, "capture-only", player_type=PlayerType.UNKNOWN),
    ]
    assert ugp.player is not None
    mass.players.all_players.return_value = [*players, ugp.player]

    entries = await ugp.get_config_entries()
    members_entry = next(entry for entry in entries if entry.key == CONF_GROUP_MEMBERS)

    assert {option.value for option in members_entry.options} == {
        "speaker",
        "light",
        "visualizer",
        "display",
    }


class TestStreamRoutes:
    """The stream of the group is served under the id of its protocol while it is attached."""

    async def test_routes_exist_while_attached(self) -> None:
        """Attaching registers the routes, detaching removes them, a new attach brings them back."""
        mass = _make_mock_mass()
        unregistered: list[str] = []

        def _register(path: str, _handler: Any) -> Any:
            return lambda: unregistered.append(path)

        mass.streams.register_dynamic_route = MagicMock(side_effect=_register)
        ugp = _make_ugp(mass)
        assert ugp.player is not None
        # nothing is registered before the group is attached
        mass.streams.register_dynamic_route.assert_not_called()

        await ugp.on_attach(ugp.player)
        routes = [call.args[0] for call in mass.streams.register_dynamic_route.call_args_list]
        assert routes == [f"/ugp/{GROUP_PROTOCOL_ID}.flac", f"/ugp/{GROUP_PROTOCOL_ID}.mp3"]

        await ugp.on_detach()
        assert sorted(unregistered) == sorted(routes)

        await ugp.on_attach(ugp.player)
        assert mass.streams.register_dynamic_route.call_count == 4


class TestIsActiveSession:
    """is_active_session governs whether children report active_group to this UGP."""

    def test_dormant_group_is_not_active(self) -> None:
        """A freshly-initialized UGP has no stream and no grace task."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        assert ugp.is_active_session is False

    def test_group_with_live_stream_is_active(self) -> None:
        """A UGP with a live (not-done) stream is considered active."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = False
        assert ugp.is_active_session is True

    def test_group_with_done_stream_is_not_active(self) -> None:
        """If the stream has already ended, the session is not considered active."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = True
        assert ugp.is_active_session is False

    def test_group_in_grace_window_is_active(self) -> None:
        """While the idle-grace task is pending, the group still claims captured members."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp.stream = None
        ugp._idle_grace_task = MagicMock()
        assert ugp.is_active_session is True


class TestPowerlessLifecycle:
    """Form-on-play / dissolve-on-stop without explicit power control."""

    async def test_play_media_captures_members(self) -> None:
        """play_media should set up a stream and forward to members without a power command."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1", "m2"]
        member1 = _make_member(mass, "m1")
        member2 = _make_member(mass, "m2")
        # iter_group_members must yield the captured members after _capture_members runs
        _group_members_of(mass, member1, member2)

        with patch.object(ugp, "update_state"):
            await ugp.play_media(
                MagicMock(
                    uri="track://x",
                    source_id="src",
                    queue_session_id="session-1",
                )
            )

        assert ugp.stream is not None
        assert ugp._attr_group_members == ["m1", "m2"]
        assert mass.players._handle_play_media.await_count == 2
        for call in mass.players._handle_play_media.await_args_list:
            member_id, media = call.args
            assert media.queue_session_id == "session-1"
            # the stream is served under the protocol id, the media belongs to the
            # queue of the group's player
            assert media.uri == f"http://test/ugp/{GROUP_PROTOCOL_ID}.mp3?player_id={member_id}"
            assert media.source_id == GROUP_PLAYER_ID
        # the stream is fetched for the group protocol
        assert mass.streams.get_stream.call_args.args[2] == GROUP_PROTOCOL_ID
        # the members are those of the group's player
        assert mass.players.iter_group_members.call_args.args[0] is ugp.player
        mass.players.get_player_lock.assert_any_call("m1", PlayerLockPurpose.PLAYBACK)
        mass.players.get_player_lock.assert_any_call("m2", PlayerLockPurpose.PLAYBACK)
        # power command was NOT used to capture the members
        mass.players.cmd_power.assert_not_awaited()

    async def test_a_member_that_still_needs_setup_is_not_captured(self) -> None:
        """A device that answers but still has to be set up can not take the stream of the group."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1", "unpaired"]
        member = _make_member(mass, "m1")
        unpaired = _make_member(mass, "unpaired")
        assert unpaired.own_protocol is not None
        unpaired.own_protocol._attr_needs_setup = True
        _group_members_of(mass, member)

        await ugp._capture_members()

        assert ugp._attr_group_members == ["m1"]

    async def test_play_media_releases_a_member_from_another_group(self) -> None:
        """A member another dynamic group holds is taken out of it through the controller."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1"]
        member = _make_member(mass, "m1", active_group="pl_other_group")
        other_group = _make_member(mass, "pl_other_group", player_type=PlayerType.GROUP)
        other_group.state.supported_features = {PlayerFeature.SET_MEMBERS}
        _group_members_of(mass, member)

        with patch.object(ugp, "update_state"):
            await ugp.play_media(MagicMock(uri="track://x", source_id="src"))

        mass.players._handle_set_members.assert_awaited_once_with(
            other_group, player_ids_to_remove=["m1"]
        )

    async def test_play_media_releases_a_member_from_its_sync_leader(self) -> None:
        """A member synced to another player leaves that player through the controller."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1"]
        member = _make_member(mass, "m1", synced_to="leader")
        leader = _make_member(mass, "leader")
        _group_members_of(mass, member)

        with patch.object(ugp, "update_state"):
            await ugp.play_media(MagicMock(uri="track://x", source_id="src"))

        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_remove=["m1"]
        )

    async def test_stop_releases_members_when_powerless(self) -> None:
        """stop() on a powerless UGP should tear down the stream and release members."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        stream = MagicMock()
        stream.done = False
        stream.stop = AsyncMock()
        ugp.stream = stream
        ugp._attr_group_members = ["m1"]
        ugp._attr_static_group_members = ["m1"]
        _group_members_of(mass, _make_member(mass, "m1"))
        # _attr_powered defaults to None (no power control)
        assert ugp._attr_powered is None

        with patch.object(ugp, "update_state"):
            await ugp.stop()

        stream.stop.assert_awaited()
        mass.players._handle_cmd_stop.assert_awaited_once_with("m1")
        # session is no longer active
        assert ugp.stream is None
        # mypy narrows ugp.stream to None above; the property check below is
        # technically static after that narrowing, hence the ignore.
        assert ugp.is_active_session is False  # type: ignore[unreachable]

    async def test_stop_preserves_session_when_pinned(self) -> None:
        """stop() on a UGP pinned with Fake power should NOT reset _attr_group_members."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        stream = MagicMock()
        stream.done = False
        stream.stop = AsyncMock()
        ugp.stream = stream
        ugp._attr_group_members = ["m1", "extra"]
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_powered = True  # user explicitly pinned via Fake control
        _group_members_of(mass, _make_member(mass, "m1"))

        with patch.object(ugp, "update_state"):
            await ugp.stop()

        # we keep the dynamic adds because the user is pinning this group
        assert "extra" in ugp._attr_group_members


class TestSetMembers:
    """Members of a dynamic group are added and removed by player id."""

    async def test_the_player_of_the_group_can_not_be_added_to_itself(self) -> None:
        """The group's own player id is refused as a member."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass, dynamic=True)

        with pytest.raises(UnsupportedFeaturedException):
            await ugp.set_members(player_ids_to_add=[GROUP_PLAYER_ID])

    async def test_a_synced_member_leaves_its_leader_before_it_joins(self) -> None:
        """A player synced to another player is detached through the controller first."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass, dynamic=True)
        _make_member(mass, "m1", synced_to="leader")
        leader = _make_member(mass, "leader")

        with patch.object(ugp, "update_state"):
            await ugp.set_members(player_ids_to_add=["m1"])

        assert ugp._attr_group_members == ["m1"]
        mass.players._handle_set_members.assert_awaited_once_with(
            leader, player_ids_to_remove=["m1"]
        )

    async def test_a_playing_member_is_stopped_when_removed(self) -> None:
        """A removed member that plays the group stream is stopped by player id."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass, dynamic=True)
        _make_member(mass, "m1", playback_state=PlaybackState.PLAYING)
        ugp._attr_group_members = ["m1"]

        with patch.object(ugp, "update_state"):
            await ugp.set_members(player_ids_to_remove=["m1"])

        assert ugp._attr_group_members == []
        mass.players._handle_cmd_stop.assert_awaited_once_with("m1")


class TestIdleGraceTimer:
    """The idle-grace timer absorbs natural PLAYING→IDLE transitions for UGP."""

    def test_grace_scheduled_on_playing_to_idle(self) -> None:
        """_set_attributes schedules a grace task on transition while stream is still live."""
        mass = _make_mock_mass()
        sentinel = MagicMock()
        mass.create_task = MagicMock(return_value=sentinel)
        ugp = _make_ugp(mass)
        # simulate a live stream and a prior PLAYING state
        ugp.stream = MagicMock()
        ugp.stream.done = False
        ugp._attr_playback_state = PlaybackState.PLAYING
        # no active children → state derives IDLE
        _group_members_of(mass)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._idle_grace_task is sentinel
        mass.create_task.assert_called_once()

    def test_grace_not_scheduled_when_pinned(self) -> None:
        """No grace task when the user has pinned the UGP with Fake power on."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = False
        ugp._attr_playback_state = PlaybackState.PLAYING
        ugp._attr_powered = True  # explicit pin
        _group_members_of(mass)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._idle_grace_task is None
        mass.create_task.assert_not_called()

    def test_grace_cancelled_on_resume(self) -> None:
        """A pending grace task is cancelled when a member resumes playback."""
        mass = _make_mock_mass()
        mass.create_task = MagicMock()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = False
        prior = MagicMock()
        prior.done.return_value = False
        ugp._idle_grace_task = prior
        ugp._attr_playback_state = PlaybackState.IDLE
        playing = _make_member(
            mass, "m1", playback_state=PlaybackState.PLAYING, active_group=GROUP_PLAYER_ID
        )
        _group_members_of(mass, playing)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        prior.cancel.assert_called_once()
        assert ugp._idle_grace_task is None


class TestMemberPositionPropagation:
    """The group mirrors the playback position reported by its active member."""

    def test_position_is_adopted(self) -> None:
        """A member's position anchor becomes the group's position anchor."""
        ugp = self._ugp_with_member(42.0, 2000.0)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 42.0
        assert ugp._attr_elapsed_time_last_updated == 2000.0

    def test_zero_position_is_adopted(self) -> None:
        """Position 0 is a real position and replaces the group's own anchor."""
        ugp = self._ugp_with_member(0.0, 2000.0)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 0.0
        assert ugp._attr_elapsed_time_last_updated == 2000.0

    def test_position_without_timestamp_is_ignored(self) -> None:
        """A position without its timestamp is unusable, so the group keeps its anchor."""
        ugp = self._ugp_with_member(42.0, None)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 12.0
        assert ugp._attr_elapsed_time_last_updated == 1000.0

    def test_unknown_position_is_ignored(self) -> None:
        """A member that reports no position leaves the group's own anchor in place."""
        ugp = self._ugp_with_member(None, 2000.0)

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 12.0
        assert ugp._attr_elapsed_time_last_updated == 1000.0

    @staticmethod
    def _ugp_with_member(
        elapsed_time: float | None, last_updated: float | None
    ) -> UniversalGroupPlayer:
        """Create a playing UGP holding a stale anchor, with a member reporting a position."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = False
        ugp._attr_playback_state = PlaybackState.PLAYING
        ugp._attr_elapsed_time = 12.0
        ugp._attr_elapsed_time_last_updated = 1000.0
        member = _make_member(
            mass, "m1", playback_state=PlaybackState.PLAYING, active_group=GROUP_PLAYER_ID
        )
        member.state.elapsed_time = elapsed_time
        member.state.elapsed_time_last_updated = last_updated
        _group_members_of(mass, member)
        return ugp


class TestFakePowerLifecycle:
    """When the user assigns Fake power control, power(True/False) drives form/dissolve."""

    async def test_power_on_captures_static_members(self) -> None:
        """power(True) should populate the effective group_members from the configured static set."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1", "m2"]
        _group_members_of(mass, _make_member(mass, "m1"), _make_member(mass, "m2"))

        with patch.object(ugp, "update_state"):
            await ugp.power(True)

        assert ugp._attr_group_members == ["m1", "m2"]
        assert ugp._attr_powered is True

    async def test_power_off_releases_and_resets_members(self) -> None:
        """power(False) should reset group_members to static and clear powered."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_group_members = ["m1", "extra"]
        ugp._attr_powered = True
        # quietly powered-on member that should be powered off on dissolve
        member = _make_member(mass, "m1", powered=True)
        _group_members_of(mass, member)

        with patch.object(ugp, "update_state"):
            await ugp.power(False)

        assert ugp._attr_group_members == ["m1"]
        assert ugp._attr_powered is False
        mass.players._handle_cmd_power.assert_awaited_once_with("m1", False)

    async def test_power_off_of_a_member_runs_under_the_group_and_member_lock(self) -> None:
        """Each member is powered off from under the lock of its group and its own."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_powered = True
        member = _make_member(mass, "m1", powered=True)
        _group_members_of(mass, member)
        events: list[str] = []

        @asynccontextmanager
        async def _lock(player_id: str) -> AsyncIterator[None]:
            events.append(f"lock:{player_id}")
            yield
            events.append(f"unlock:{player_id}")

        mass.players.get_group_and_player_lock = MagicMock(side_effect=_lock)
        mass.players._handle_cmd_power = AsyncMock(
            side_effect=lambda player_id, _powered: events.append(f"power off:{player_id}")
        )

        with (
            patch.object(ugp, "update_state"),
            patch.object(member, "refresh_state", wraps=member.refresh_state) as refresh,
        ):
            await ugp.power(False)

        assert events == ["lock:m1", "power off:m1", "unlock:m1"]
        # the member stops naming the group before it is powered off
        refresh.assert_called_once()


class TestSupportedFeaturesPower:
    """POWER feature is only advertised when the user opts in via Fake control."""

    async def test_power_not_in_features_by_default(self) -> None:
        """No power_control config → no POWER feature."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        await ugp.on_config_updated()
        assert PlayerFeature.POWER not in ugp.supported_features

    async def test_power_advertised_when_fake_control_assigned(self) -> None:
        """User assigns Fake power control to the group's player → POWER feature shows up."""
        mass = _make_mock_mass()
        stored = {(GROUP_PLAYER_ID, "power_control"): PLAYER_CONTROL_FAKE}
        mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: stored.get((player_id, key), default)
        )
        ugp = _make_ugp(mass)
        await ugp.on_config_updated()
        assert PlayerFeature.POWER in ugp.supported_features

    async def test_power_follows_the_player_not_the_protocol(self) -> None:
        """A power control stored under the protocol id is no power control of the group."""
        mass = _make_mock_mass()
        stored = {(GROUP_PROTOCOL_ID, "power_control"): PLAYER_CONTROL_FAKE}
        mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: stored.get((player_id, key), default)
        )
        ugp = _make_ugp(mass)
        await ugp.on_config_updated()
        assert PlayerFeature.POWER not in ugp.supported_features


class TestSupportedFeaturesSetMembers:
    """SET_MEMBERS tracks the dynamic-members config option."""

    def test_dynamic_group_advertises_set_members(self) -> None:
        """A dynamic group lets the user change its members."""
        ugp = _make_ugp(_make_mock_mass(), dynamic=True)
        assert PlayerFeature.SET_MEMBERS in ugp.supported_features

    def test_static_group_does_not_advertise_set_members(self) -> None:
        """A static group's members are fixed by its config."""
        ugp = _make_ugp(_make_mock_mass(), dynamic=False)
        assert PlayerFeature.SET_MEMBERS not in ugp.supported_features

    async def test_the_configured_members_are_the_static_ones(self) -> None:
        """The configured members, by player id, are the static members of the group."""
        ugp = _make_ugp(_make_mock_mass(), members=["m1", "m2"])
        await ugp.on_config_updated()
        assert ugp.static_group_members == ["m1", "m2"]
        assert ugp._attr_group_members == ["m1", "m2"]


class TestSupportedFeaturesFromMembers:
    """Volume and mute are inherited from the members those commands are fanned out to."""

    def test_volume_advertised_when_a_member_supports_it(self) -> None:
        """A volume-capable member gives the group a volume capability."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_member(mass, "m1")
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}
        ugp._attr_group_members = ["m1"]

        assert PlayerFeature.VOLUME_SET in ugp.supported_features

    def test_volume_not_advertised_without_capable_members(self) -> None:
        """An empty group has nothing to send a volume command to."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)

        assert ugp._attr_group_members == []
        assert PlayerFeature.VOLUME_SET not in ugp.supported_features

    def test_mute_not_advertised_without_capable_members(self) -> None:
        """Members that can't be muted leave the group without a mute capability."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        _make_member(mass, "m1")
        ugp._attr_group_members = ["m1"]

        assert PlayerFeature.VOLUME_MUTE not in ugp.supported_features

    def test_mute_advertised_when_a_member_supports_it(self) -> None:
        """A single mute-capable member is enough to advertise mute on the group."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        _make_member(mass, "m1")
        mute_member = _make_member(mass, "m2")
        mute_member.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_MUTE,
        }
        ugp._attr_group_members = ["m1", "m2"]

        assert PlayerFeature.VOLUME_MUTE in ugp.supported_features

    def test_unavailable_members_do_not_contribute(self) -> None:
        """An offline member's capabilities are not advertised by the group."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_member(mass, "m1", available=False)
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_MUTE}
        ugp._attr_group_members = ["m1"]

        assert PlayerFeature.VOLUME_MUTE not in ugp.supported_features

    def test_dormant_group_resolves_a_native_mute_control(self) -> None:
        """
        An idle group resolves a mute control, which is what HA gates its mute button on.

        Without an inherited VOLUME_MUTE this lands on PLAYER_CONTROL_NONE and the
        capability is dropped from the player state again.
        """
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_member(mass, "m1")
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_MUTE}
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_group_members = ["m1"]
        # a member update reaches the group as trigger_player_update -> update_state,
        # which is what drops the cached mute_control so it can be re-resolved
        ugp.update_state()

        assert not ugp.is_active_session
        assert ugp.player is not None
        assert ugp.player._mute_control == PLAYER_CONTROL_NATIVE

    def test_mute_control_stays_none_without_capable_members(self) -> None:
        """No member can be muted → no mute control to hand to HA."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        _make_member(mass, "m1")
        ugp._attr_group_members = ["m1"]
        ugp.update_state()

        assert ugp.player is not None
        assert ugp.player._mute_control == PLAYER_CONTROL_NONE


class TestMemberStream:
    """The stream a member fetches is processed for the protocol that renders it."""

    async def test_a_pre_split_member_gets_the_output_of_its_audio_protocol(self) -> None:
        """A member whose player id is that of its own protocol, playing over AirPlay."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass, members=["m1"])
        member, _native = attach_mock(MockProvider("sonos", mass=mass), "m1", player_id="m1")
        attach_mock(
            MockProvider("airplay", mass=mass), "ap_m1", role=ProtocolRole.AUDIO, player=member
        )
        member.set_active_output_protocol("ap_m1")
        mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
        mass.config.get_protocol_value.side_effect = lambda protocol_id, _key, default: {
            "ap_m1": "left",
            "m1": "right",
        }.get(protocol_id, default)
        mass.config.get_dsp_irs.return_value = []
        mass.streams.audio = StreamsAudio(mass)
        plans: list[AudioOutputPlan] = []
        get_plan = mass.streams.audio.get_player_output_plan

        def _record(*args: Any, **kwargs: Any) -> AudioOutputPlan:
            plans.append(get_plan(*args, **kwargs))
            return plans[-1]

        async def _audio(*_args: Any, **_kwargs: Any) -> AsyncIterator[bytes]:
            yield b"audio"

        pcm = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32)
        ugp.stream = MagicMock(
            done=False, input_format=pcm, queue_id=None, session_id=None, get_stream=_audio
        )
        request = MagicMock(
            path=f"/ugp/{GROUP_PROTOCOL_ID}.mp3",
            query={"player_id": "m1"},
            method="GET",
            version=HttpVersion11,
        )
        with (
            patch.object(mass.streams.audio, "get_player_output_plan", side_effect=_record),
            patch(
                "music_assistant.providers.universal_group.player.web.StreamResponse",
                return_value=MagicMock(prepare=AsyncMock(), write=AsyncMock()),
            ),
        ):
            await ugp._serve_ugp_stream(request)

        assert [plan.output_details.source_channel for plan in plans] == [AudioChannel.FL]


class _MemberQueue:
    """Queue controller stand-in with one queue, whose play action is the real decorated one."""

    mass: MusicAssistant
    _queue_data: dict[str, PlayerQueueData]

    def __init__(self, mass: MagicMock, queue_id: str) -> None:
        """Initialize the stand-in with the queue of one player."""
        self.mass = mass
        queue = PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=0
        )
        self._queue_data = {queue_id: PlayerQueueData(queue=queue)}

    @handle_play_action
    async def play_index(self, queue_id: str) -> None:
        """Play the queue's next item on its player, the way play_index does."""
        await self.mass.players.play_media(queue_id, PlayerMedia(uri="x", source_id=queue_id))

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Ignore the queue updates."""

    def on_player_update(self, player: Player, changed_values: dict[str, tuple[Any, Any]]) -> None:
        """Ignore the player updates."""


async def _transfer_to_the_group(players: PlayerController, member_id: str) -> None:
    """Start the group the way a transfer of a member's queue to it does, under both locks."""
    async with players.get_playback_locks(member_id, GROUP_PLAYER_ID):
        await players.play_media(GROUP_PLAYER_ID, PlayerMedia(uri="x", source_id=GROUP_PLAYER_ID))


class TestCommandOnAMemberOfAHeldGroup:
    """A playback command addressed to a member of a group that holds it plays on every member."""

    @staticmethod
    def _held_group(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[PlayerController, UniversalGroupPlayer, list[str]]:
        """
        Build a powered universal group holding two members, on a real players controller.

        :return: The controller, the group and the record of the members asked to play.
        """
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", LOCK_BUDGET)
        mass = mock_player_mass()
        mass.config.get_raw_core_config_value.return_value = "GLOBAL"
        players = PlayerController(mass)
        mass.players = players
        mass.loop = asyncio.get_running_loop()

        def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> Any:
            # the plays the group hands its members run, background work is left out
            if inspect.iscoroutine(target) and target.__qualname__.endswith(
                "_play_media_on_member"
            ):
                return asyncio.ensure_future(target)
            if asyncio.iscoroutine(target):
                target.close()
            return MagicMock()

        mass.create_task = MagicMock(side_effect=_create_task)
        mass.streams = MagicMock()
        mass.streams.base_url = "http://test"
        mass.streams.register_dynamic_route = MagicMock(return_value=lambda: None)
        mass.streams.get_stream = MagicMock(return_value=MagicMock())
        ugp = _make_ugp(mass, members=["m1", "m2"])
        provider = MockProvider("sonos", instance_id="sonos", mass=mass)
        members = [
            attach_mock(provider, f"sonos_{pid}", pid, player_id=pid)[0] for pid in ("m1", "m2")
        ]
        ugp._attr_static_group_members = ["m1", "m2"]
        ugp._attr_group_members = ["m1", "m2"]
        ugp._attr_powered = True
        assert ugp.player is not None
        for player in (ugp.player, *members):
            player.refresh_state(signal_event=False)
        assert members[0].state.active_group == GROUP_PLAYER_ID
        played_on: list[str] = []
        route_play_media = players._handle_play_media

        async def _handle_play_media(player_id: str, media: PlayerMedia) -> None:
            # the group plays through its own protocol, a member is only recorded
            if player_id == GROUP_PLAYER_ID:
                await route_play_media(player_id, media)
                return
            played_on.append(player_id)

        players._handle_play_media = _handle_play_media  # type: ignore[method-assign]

        async def _resume(queue_id: str, fade_in: bool | None = None) -> None:  # noqa: ARG001
            # a queue play action: the group's lock, then the play of the queue's next item
            async with players.get_group_and_player_lock(queue_id):
                await players.play_media(queue_id, PlayerMedia(uri="x", source_id=queue_id))

        mass.player_queues.get = MagicMock(
            side_effect=lambda queue_id: MagicMock(queue_id=queue_id)
        )
        mass.player_queues.has_lost_paused_stream = MagicMock(return_value=False)
        mass.player_queues.resume = AsyncMock(side_effect=_resume)
        return players, ugp, played_on

    @pytest.mark.parametrize(
        "command",
        [
            "cmd_play",
            "cmd_resume",
            "play_media",
            "play action on its queue",
            "transfer of its queue to the group",
        ],
    )
    async def test_every_member_plays_and_the_command_returns_at_once(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """The member the command was addressed to is played on like the others."""
        players, _ugp, played_on = self._held_group(monkeypatch)
        if command in ("play_media", "play action on its queue"):
            # the member's play follows its group rather than taking it out of it
            cast("MagicMock", players.mass).config.get_player_value = MagicMock(
                side_effect=lambda _player_id, key, default=None: (
                    False if key == CONF_PLAY_MEDIA_OVERRIDES_GROUP else default
                )
            )
        if command == "play_media":
            call = players.play_media("m1", PlayerMedia(uri="x"))
        elif command == "play action on its queue":
            # a play action holds the member's own queue, and with it the member's lock
            call = _MemberQueue(cast("MagicMock", players.mass), "m1").play_index("m1")
        elif command == "transfer of its queue to the group":
            # a transfer holds the player of either queue until the group plays
            call = _transfer_to_the_group(players, "m1")
        else:
            call = getattr(players, command)("m1")

        waits = record_lock_waits(players)

        await call

        assert waits == []
        assert sorted(played_on) == ["m1", "m2"]


class TestStreamContentLength:
    """The group stream follows the fixed content length setting of the member fetching it."""

    UGP_CONTENT_LENGTH = 4294967296

    async def _serve(
        self, member_setting: bool, member_id: str, version: HttpVersion = HttpVersion11
    ) -> web.StreamResponse:
        """Serve a probe of the group stream to member_id and return the prepared response."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass, members=["m1"])
        ugp.stream = MagicMock(done=False)
        # the setting is one of the protocol that renders the member's audio
        _member, protocol = attach_mock(
            MockProvider("sonos", mass=mass), "proto_m1", player_id="m1"
        )
        config = PlayerConfig.parse(
            [CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH],
            {
                "player_id": "proto_m1",
                "provider": "sonos",
                "values": {CONF_REQUIRES_FIXED_CONTENT_LENGTH: member_setting},
            },
        )
        protocol.set_config(cast("PlayerConfig", config))
        request = make_mocked_request(
            "HEAD", f"/ugp/{GROUP_PROTOCOL_ID}.mp3?player_id={member_id}", version=version
        )
        return await ugp._serve_ugp_stream(request)

    async def test_member_that_requires_it_gets_a_content_length(self) -> None:
        """A member whose output protocol needs a fixed length gets one."""
        resp = await self._serve(member_setting=True, member_id="m1")

        assert resp.content_length == self.UGP_CONTENT_LENGTH

    @pytest.mark.parametrize("version", [HttpVersion10, HttpVersion11], ids=["http10", "http11"])
    async def test_member_without_the_setting_gets_no_length(self, version: HttpVersion) -> None:
        """Other members get no length and no forced chunking, on any HTTP version."""
        resp = await self._serve(member_setting=False, member_id="m1", version=version)

        assert resp.prepared
        assert resp.content_length is None
        assert not resp.chunked

    async def test_unknown_member_gets_no_length(self) -> None:
        """A stale member id in the stream url falls back to no fixed length."""
        resp = await self._serve(member_setting=True, member_id="ghost")

        assert resp.content_length is None
