"""Tests for sync groups formed from Apple groups (Apple TV home theater groups)."""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import (
    AIRPLAY_GROUP_HOME_THEATER,
    AIRPLAY_GROUP_STEREO_PAIR,
    ATTR_LINKED_VOLUME_SET_AT,
    CONF_AIRPLAY_GROUP_TYPE,
    CONF_GROUP_MEMBERS,
)
from music_assistant.models.player import Player
from music_assistant.providers.sync_group.player import SyncGroupPlayer


def _member(player_id: str, volume_level: int, powered: bool | None = True) -> MagicMock:
    """
    Create a mock member of an Apple group.

    :param player_id: The player id.
    :param volume_level: The member's current volume level.
    :param powered: The member's power state.
    """
    player = MagicMock()
    player.player_id = player_id
    player.state.volume_level = volume_level
    player.state.volume_control = "native"
    player.state.powered = powered
    player.provider.domain = "airplay"
    return player


def _home_theater_group(members: list[MagicMock]) -> SyncGroupPlayer:
    """
    Create a sync group formed from an Apple TV home theater group.

    :param members: The group members; the one with id "apple_tv" is the group leader.
    """
    mass = MagicMock()
    mass.players.get_player = MagicMock(
        side_effect=lambda pid: next((m for m in members if m.player_id == pid), None)
    )
    mass.players.iter_group_members = MagicMock(return_value=members)
    mass.players._set_member_volume = AsyncMock()
    mass.config.get_base_player_config.return_value = MagicMock(
        name=None, default_name="Living Room TV", get_value=MagicMock(return_value=None)
    )
    provider = MagicMock()
    provider.domain = "sync_group"
    provider.instance_id = "sync_group"
    provider.mass = mass
    group = SyncGroupPlayer(provider, "syncgroup_aptest")
    group._cache.clear()
    group._airplay_group_type = "home_theater"
    group._airplay_group_leader = "apple_tv"
    group._attr_group_volume_linked = True
    return group


def test_apple_tv_takes_no_part_in_the_group_volume() -> None:
    """The Apple TV plays through the speakers, so its level is not the group's."""
    apple_tv = _member("apple_tv", 50)
    speaker = _member("speaker", 30)
    group = _home_theater_group([speaker, apple_tv])
    assert group.takes_part_in_group_volume(speaker)
    assert not group.takes_part_in_group_volume(apple_tv)


async def test_linked_volume_levels_the_speakers_but_not_the_apple_tv() -> None:
    """A speaker's own change is followed by the other speakers, not by the Apple TV."""
    left = _member("left", 40)
    right = _member("right", 30)
    apple_tv = _member("apple_tv", 50)
    group = _home_theater_group([left, right, apple_tv])
    group._linked_volume_source = "left"
    await group._level_linked_volume()
    group.mass.players._set_member_volume.assert_awaited_once_with("right", 40)


def _report_volume(group: SyncGroupPlayer, member: MagicMock) -> MagicMock:
    """
    Report a volume change of a member to the group, and return the scheduler mock.

    :param group: The group the member belongs to.
    :param member: The member reporting a new volume level.
    """
    changed: dict[str, tuple[Any, Any]] = {"volume_level": (30, member.state.volume_level)}
    with (
        patch.object(SyncGroupPlayer, "_update_attributes"),
        patch.object(Player, "on_group_member_updated"),
    ):
        group.on_group_member_updated(member, changed)
    return group.mass.call_later


def _linked_volume_scheduled(call_later: MagicMock) -> bool:
    """
    Return whether the group scheduled bringing the other members to a new level.

    :param call_later: The mass.call_later mock.
    """
    return any(
        str(call.kwargs.get("task_id", "")).startswith("sync_group_linked_volume_")
        for call in call_later.call_args_list
    )


def test_a_speakers_own_volume_change_is_followed() -> None:
    """A level a speaker reports by itself (e.g. from Siri) is brought to the others."""
    speaker = _member("speaker", 40)
    group = _home_theater_group([speaker, _member("apple_tv", 50)])
    assert _linked_volume_scheduled(_report_volume(group, speaker))


def test_the_apple_tvs_volume_change_is_not_followed() -> None:
    """The Apple TV's own level never moves the speakers."""
    apple_tv = _member("apple_tv", 50)
    group = _home_theater_group([_member("speaker", 30), apple_tv])
    assert not _linked_volume_scheduled(_report_volume(group, apple_tv))


def test_the_echo_of_the_groups_own_volume_change_is_not_followed() -> None:
    """A level reported just after the group set its members (maybe rounded) is ignored."""
    speaker = _member("speaker", 28)
    group = _home_theater_group([speaker, _member("apple_tv", 50)])
    group.extra_data[ATTR_LINKED_VOLUME_SET_AT] = time.monotonic()
    assert not _linked_volume_scheduled(_report_volume(group, speaker))


def test_a_volume_command_to_one_speaker_is_followed() -> None:
    """A level set on one speaker directly (e.g. by an automation) reaches the others."""
    speaker = _member("speaker", 15)
    group = _home_theater_group([speaker, _member("apple_tv", 50)])
    # a direct command leaves a fresh volume target on the member, which is no echo
    group.mass.players._unexpired_volume_target = MagicMock(return_value=15)
    assert _linked_volume_scheduled(_report_volume(group, speaker))


def test_only_the_apple_tv_is_kept_asleep() -> None:
    """An Apple TV that is off is left out; a speaker that is off is powered on as before."""
    apple_tv = _member("apple_tv", 50, powered=False)
    speaker = _member("speaker", 30, powered=False)
    group = _home_theater_group([speaker, apple_tv])
    assert group._is_powered_off("apple_tv")
    assert not group._is_powered_off("speaker")
    apple_tv.state.powered = True
    assert not group._is_powered_off("apple_tv")


def test_a_user_made_group_keeps_no_member_asleep() -> None:
    """Outside a group formed from an Apple group, no member is left out for its power."""
    apple_tv = _member("apple_tv", 50, powered=False)
    group = _home_theater_group([apple_tv])
    group._airplay_group_type = None
    group._airplay_group_leader = None
    assert not group._is_powered_off("apple_tv")


def test_only_bookkeeping_leaders_are_interchangeable() -> None:
    """A leader that only anchors the session can be swapped; a distributing one cannot."""
    group = _home_theater_group([])
    airplay_leader = _member("airplay", 30)
    sonos_leader = _member("sonos", 30)
    sonos_leader.provider.domain = "sonos"
    assert group._leader_is_interchangeable(airplay_leader)
    assert not group._leader_is_interchangeable(sonos_leader)


def _configured_group(raw_values: dict[str, Any]) -> SyncGroupPlayer:
    """
    Create a sync group and load its config.

    :param raw_values: The group's raw config values.
    """
    mass = MagicMock()
    mass.config.get_base_player_config.return_value = MagicMock(
        name=None,
        default_name="Living Room TV",
        get_value=MagicMock(side_effect=lambda key, default=None: raw_values.get(key, default)),
    )
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, key, default=None: raw_values.get(key, default)
    )
    provider = MagicMock()
    provider.domain = "sync_group"
    provider.instance_id = "sync_group"
    provider.name = "Sync Group Player"
    provider.mass = mass
    return SyncGroupPlayer(provider, "syncgroup_aptest")


@pytest.mark.parametrize(
    ("group_type", "model"),
    [
        (AIRPLAY_GROUP_HOME_THEATER, "Apple TV Home Theater"),
        (AIRPLAY_GROUP_STEREO_PAIR, "HomePod Stereo Pair"),
    ],
)
async def test_an_apple_group_is_managed_by_the_home_app(group_type: str, model: str) -> None:
    """An Apple group shows what the Home app grouped, and can not be changed in MA."""
    group = _configured_group(
        {CONF_AIRPLAY_GROUP_TYPE: group_type, CONF_GROUP_MEMBERS: ["left", "right"]}
    )
    await group.on_config_updated()
    assert group.managed_externally
    assert (group.device_info.manufacturer, group.device_info.model) == ("AirPlay", model)


async def test_a_sync_group_the_user_made_stays_theirs() -> None:
    """A sync group without an Apple group type can be changed, and shows as before."""
    group = _configured_group({CONF_GROUP_MEMBERS: ["left", "kitchen"]})
    await group.on_config_updated()
    assert not group.managed_externally
    assert group.device_info.model == "Sync Group Player"
