"""
Tests for the Universal Group Player lifecycle.

Mirrors the structure of ``tests/providers/sync_group/test_sync_group.py`` but covers the
multicast-stream variant (Universal Group). The new lifecycle replaces the
mandatory power control with form-on-play, dissolve-on-stop, and a debounced
idle grace window; these tests pin that behavior in place.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import CONF_GROUP_MEMBERS
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.providers.universal_group.player import UniversalGroupPlayer


def _make_mock_mass() -> MagicMock:
    """Create a minimal mock MusicAssistant for the UGP tests."""
    mass = MagicMock()
    mass.players = MagicMock()
    mass.players.get_player = MagicMock(return_value=None)
    mass.players._handle_cmd_stop = AsyncMock()
    mass.players._handle_play_media = AsyncMock()
    mass.players.cmd_power = AsyncMock()
    mass.players.iter_group_members = MagicMock(return_value=[])

    lock_ctx = AsyncMock()
    lock_ctx.__aenter__.return_value = None
    lock_ctx.__aexit__.return_value = False
    mass.players.get_player_lock = MagicMock(return_value=lock_ctx)

    wait_ctx = AsyncMock()
    wait_ctx.__aenter__.return_value = None
    wait_ctx.__aexit__.return_value = False
    mass.players.wait_for_player_update = MagicMock(return_value=wait_ctx)

    mass.config = MagicMock()

    def _config_value(key: str, default: object = None) -> object:
        if key == "group_members":
            return []
        if key == "dynamic_members":
            return False
        return default

    mass.config.get_base_player_config.return_value = MagicMock(
        name=None, default_name="Test UGP", get_value=_config_value
    )
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)

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
    mass.cache = MagicMock()
    return mass


def _make_ugp(mass: MagicMock, player_id: str = "ugp_test") -> UniversalGroupPlayer:
    """Create a UniversalGroupPlayer with a mock provider."""
    provider = MagicMock()
    provider.domain = "universal_group"
    provider.instance_id = "universal_group_test"
    provider.name = "Universal Group"
    provider.mass = mass

    return UniversalGroupPlayer(provider, player_id)


def _make_mock_player(
    player_id: str,
    powered: bool = True,
    available: bool = True,
    enabled: bool = True,
    active_group: str | None = None,
    synced_to: str | None = None,
    playback_state: PlaybackState = PlaybackState.IDLE,
    player_type: PlayerType = PlayerType.PLAYER,
) -> MagicMock:
    """Create a mock child player."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.type = player_type
    player.available = available
    player.enabled = enabled
    player.powered = powered
    player.power_control = "native"
    player.synced_to = synced_to
    player.playback_state = playback_state
    player.active_source = None
    player.state = MagicMock()
    player.state.available = available
    player.state.enabled = enabled
    player.state.powered = powered
    player.state.power_control = "native"
    player.state.active_group = active_group
    player.state.synced_to = synced_to
    player.state.playback_state = playback_state
    player.state.supported_features = {PlayerFeature.PLAY_MEDIA}
    player.state.elapsed_time = 0
    player.state.elapsed_time_last_updated = None
    player.ungroup = AsyncMock()
    return player


async def test_config_members_exclude_groups_and_unknown_players() -> None:
    """Only known non-group player types should be offered as universal group members."""
    mass = _make_mock_mass()
    players = [
        _make_mock_player("speaker"),
        _make_mock_player("light", player_type=PlayerType.LIGHT),
        _make_mock_player("visualizer", player_type=PlayerType.VISUALIZER),
        _make_mock_player("display", player_type=PlayerType.DISPLAY),
        _make_mock_player("group", player_type=PlayerType.GROUP),
        _make_mock_player("capture-only", player_type=PlayerType.UNKNOWN),
    ]
    players[1].state.supported_features = set()
    players[2].state.supported_features = set()
    players[2].hide_in_ui = True
    players[3].state.supported_features = set()
    mass.players.all_players.return_value = players

    entries = await _make_ugp(mass).get_config_entries()
    members_entry = next(entry for entry in entries if entry.key == CONF_GROUP_MEMBERS)

    assert {option.value for option in members_entry.options} == {
        "speaker",
        "light",
        "visualizer",
        "display",
    }


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

    @pytest.mark.asyncio
    async def test_play_media_captures_members(self) -> None:
        """play_media should set up a stream and forward to members without a power command."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1", "m2"]
        member1 = _make_mock_player("m1")
        member2 = _make_mock_player("m2")
        mass.players.get_player = MagicMock(
            side_effect=lambda pid, *_args, **_kwargs: {"m1": member1, "m2": member2}.get(pid)
        )
        # iter_group_members must yield the captured members after _capture_members runs
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter(
            [member1, member2]
        )

        with patch.object(ugp, "update_state"):
            await ugp.play_media(
                MagicMock(
                    uri="track://x",
                    source_id="src",
                    queue_session_id="session-1",
                )
            )

        assert ugp.stream is not None
        assert mass.players._handle_play_media.await_count == 2
        for call in mass.players._handle_play_media.await_args_list:
            assert call.args[1].queue_session_id == "session-1"
        mass.players.get_player_lock.assert_any_call("m1", PlayerLockPurpose.PLAYBACK)
        mass.players.get_player_lock.assert_any_call("m2", PlayerLockPurpose.PLAYBACK)
        # power command was NOT used to capture the members
        mass.players.cmd_power.assert_not_awaited()

    @pytest.mark.asyncio
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
        member = _make_mock_player("m1")
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([member])
        # _attr_powered defaults to None (no power control)
        assert ugp._attr_powered is None

        with patch.object(ugp, "update_state"):
            await ugp.stop()

        stream.stop.assert_awaited()
        # session is no longer active
        assert ugp.stream is None
        # mypy narrows ugp.stream to None above; the property check below is
        # technically static after that narrowing, hence the ignore.
        assert ugp.is_active_session is False  # type: ignore[unreachable]

    @pytest.mark.asyncio
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
        member = _make_mock_player("m1")
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([member])

        with patch.object(ugp, "update_state"):
            await ugp.stop()

        # we keep the dynamic adds because the user is pinning this group
        assert "extra" in ugp._attr_group_members


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
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([])

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
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([])

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
        playing = _make_mock_player(
            "m1", playback_state=PlaybackState.PLAYING, active_group="ugp_test"
        )
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([playing])

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        prior.cancel.assert_called_once()
        assert ugp._idle_grace_task is None


class TestMemberPositionPropagation:
    """The group mirrors the playback position reported by its active member."""

    def test_position_is_adopted(self) -> None:
        """A member's position anchor becomes the group's position anchor."""
        ugp = self._ugp_with_member(self._member(42.0, 2000.0))

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 42.0
        assert ugp._attr_elapsed_time_last_updated == 2000.0

    def test_zero_position_is_adopted(self) -> None:
        """Position 0 is a real position and replaces the group's own anchor."""
        ugp = self._ugp_with_member(self._member(0.0, 2000.0))

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 0.0
        assert ugp._attr_elapsed_time_last_updated == 2000.0

    def test_position_without_timestamp_is_ignored(self) -> None:
        """A position without its timestamp is unusable, so the group keeps its anchor."""
        ugp = self._ugp_with_member(self._member(42.0, None))

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 12.0
        assert ugp._attr_elapsed_time_last_updated == 1000.0

    def test_unknown_position_is_ignored(self) -> None:
        """A member that reports no position leaves the group's own anchor in place."""
        ugp = self._ugp_with_member(self._member(None, 2000.0))

        with patch.object(ugp, "update_state"):
            ugp._set_attributes()

        assert ugp._attr_elapsed_time == 12.0
        assert ugp._attr_elapsed_time_last_updated == 1000.0

    @staticmethod
    def _member(elapsed_time: float | None, last_updated: float | None) -> MagicMock:
        """Create an active member reporting the given position anchor."""
        member = _make_mock_player(
            "m1", playback_state=PlaybackState.PLAYING, active_group="ugp_test"
        )
        member.state.elapsed_time = elapsed_time
        member.state.elapsed_time_last_updated = last_updated
        return member

    @staticmethod
    def _ugp_with_member(member: MagicMock) -> UniversalGroupPlayer:
        """Create a playing UGP holding a stale anchor and deriving state from the member."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp.stream = MagicMock()
        ugp.stream.done = False
        ugp._attr_playback_state = PlaybackState.PLAYING
        ugp._attr_elapsed_time = 12.0
        ugp._attr_elapsed_time_last_updated = 1000.0
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([member])
        return ugp


class TestFakePowerLifecycle:
    """When the user assigns Fake power control, power(True/False) drives form/dissolve."""

    @pytest.mark.asyncio
    async def test_power_on_captures_static_members(self) -> None:
        """power(True) should populate the effective group_members from the configured static set."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1", "m2"]
        member1 = _make_mock_player("m1")
        member2 = _make_mock_player("m2")
        mass.players.get_player = MagicMock(
            side_effect=lambda pid, *_args, **_kwargs: {"m1": member1, "m2": member2}.get(pid)
        )
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter(
            [member1, member2]
        )

        with patch.object(ugp, "update_state"):
            await ugp.power(True)

        assert ugp._attr_group_members == ["m1", "m2"]
        assert ugp._attr_powered is True

    @pytest.mark.asyncio
    async def test_power_off_releases_and_resets_members(self) -> None:
        """power(False) should reset group_members to static and clear powered."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_group_members = ["m1", "extra"]
        ugp._attr_powered = True
        # quietly powered-on member that should get cmd_power(False) on dissolve
        member = _make_mock_player("m1", powered=True)
        mass.players.iter_group_members.side_effect = lambda *_args, **_kwargs: iter([member])

        with patch.object(ugp, "update_state"):
            await ugp.power(False)

        assert ugp._attr_group_members == ["m1"]
        assert ugp._attr_powered is False


class TestSupportedFeaturesPower:
    """POWER feature is only advertised when the user opts in via Fake control."""

    @pytest.mark.asyncio
    async def test_power_not_in_features_by_default(self) -> None:
        """No power_control config → no POWER feature."""
        mass = _make_mock_mass()
        # explicitly ensure power_control config is empty
        mass.config.get_raw_player_config_value = MagicMock(return_value=None)
        ugp = _make_ugp(mass)
        # on_config_updated is what (re)evaluates the feature set
        await ugp.on_config_updated()
        assert PlayerFeature.POWER not in ugp.supported_features

    @pytest.mark.asyncio
    async def test_power_advertised_when_fake_control_assigned(self) -> None:
        """User assigns Fake power control → POWER feature shows up."""
        mass = _make_mock_mass()

        def _get_raw(_player_id: str, key: str, default: object = None) -> object:
            if key == "power_control":
                return PLAYER_CONTROL_FAKE
            return default

        mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)
        ugp = _make_ugp(mass)
        await ugp.on_config_updated()
        assert PlayerFeature.POWER in ugp.supported_features


class TestSupportedFeaturesSetMembers:
    """SET_MEMBERS tracks the dynamic-members config option."""

    @staticmethod
    def _make_mass(dynamic: bool) -> MagicMock:
        """Build a mock mass whose group is (or isn't) a dynamic group."""
        mass = _make_mock_mass()

        def _config_value(key: str, default: object = None) -> object:
            if key == "group_members":
                return []
            if key == "dynamic_members":
                return dynamic
            return default

        mass.config.get_base_player_config.return_value = MagicMock(
            name=None, default_name="Test UGP", get_value=_config_value
        )
        return mass

    def test_dynamic_group_advertises_set_members(self) -> None:
        """A dynamic group lets the user change its members."""
        ugp = _make_ugp(self._make_mass(dynamic=True))
        assert PlayerFeature.SET_MEMBERS in ugp.supported_features

    def test_static_group_does_not_advertise_set_members(self) -> None:
        """A static group's members are fixed by its config."""
        ugp = _make_ugp(self._make_mass(dynamic=False))
        assert PlayerFeature.SET_MEMBERS not in ugp.supported_features


class TestSupportedFeaturesFromMembers:
    """Volume and mute are inherited from the members those commands are fanned out to."""

    @staticmethod
    def _attach_members(mass: MagicMock, *members: MagicMock) -> None:
        """Register the given mock members on the mock player controller."""
        by_id = {member.player_id: member for member in members}
        mass.players.get_player = MagicMock(
            side_effect=lambda pid, *_args, **_kwargs: by_id.get(pid)
        )

    def test_volume_advertised_when_a_member_supports_it(self) -> None:
        """A volume-capable member gives the group a volume capability."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_mock_player("m1")
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}
        self._attach_members(mass, member)
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
        member = _make_mock_player("m1")
        self._attach_members(mass, member)
        ugp._attr_group_members = ["m1"]

        assert PlayerFeature.VOLUME_MUTE not in ugp.supported_features

    def test_mute_advertised_when_a_member_supports_it(self) -> None:
        """A single mute-capable member is enough to advertise mute on the group."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        plain_member = _make_mock_player("m1")
        mute_member = _make_mock_player("m2")
        mute_member.state.supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attach_members(mass, plain_member, mute_member)
        ugp._attr_group_members = ["m1", "m2"]

        assert PlayerFeature.VOLUME_MUTE in ugp.supported_features

    def test_unavailable_members_do_not_contribute(self) -> None:
        """An offline member's capabilities are not advertised by the group."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_mock_player("m1", available=False)
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_MUTE}
        self._attach_members(mass, member)
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
        member = _make_mock_player("m1")
        member.state.supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_MUTE}
        self._attach_members(mass, member)
        ugp._attr_static_group_members = ["m1"]
        ugp._attr_group_members = ["m1"]
        # a member update reaches the group as trigger_player_update -> update_state,
        # which is what drops the cached mute_control so it can be re-resolved
        ugp.update_state()

        assert not ugp.is_active_session
        assert ugp.mute_control == PLAYER_CONTROL_NATIVE

    def test_mute_control_stays_none_without_capable_members(self) -> None:
        """No member can be muted → no mute control to hand to HA."""
        mass = _make_mock_mass()
        ugp = _make_ugp(mass)
        member = _make_mock_player("m1")
        self._attach_members(mass, member)
        ugp._attr_group_members = ["m1"]
        ugp.update_state()

        assert ugp.mute_control == PLAYER_CONTROL_NONE
