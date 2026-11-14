"""Tests for the lifecycle of a sync group on a running server: create, restart, remove."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import EventType, PlayerFeature, PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from tests.scenarios.harness import DEFAULT_FEATURES, Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers and the real group providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


async def _members(scenario: Scenario) -> list[str]:
    """Attach two devices that can group with each other and return their player ids."""
    provider = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    member_a = provider.protocol("member_a", "Player A", supported_features=features)
    member_b = provider.protocol("member_b", "Player B", supported_features=features)
    member_a._attr_can_group_with = {provider.instance_id}
    member_b._attr_can_group_with = {provider.instance_id}
    await scenario.register(member_a)
    await scenario.register(member_b)
    player_ids = [scenario.player_of(member_a), scenario.player_of(member_b)]
    assert None not in player_ids
    return [player_id for player_id in player_ids if player_id]


async def _create_group(scenario: Scenario, members: list[str], *, dynamic: bool) -> Player:
    """Create a sync group the way a client does."""
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Group Z", members, dynamic
    )
    await scenario.settle()
    return group


def _group_protocol(scenario: Scenario, player_id: str) -> SyncGroupPlayer:
    """Return the attached group protocol of the player of a group."""
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    protocol = player.own_protocol
    assert isinstance(protocol, SyncGroupPlayer)
    return protocol


async def test_a_new_group_gets_a_player_of_its_own(scenario: Scenario) -> None:
    """A group created now is a player with a minted id; its member lists are its protocol's."""
    members = await _members(scenario)

    group = await _create_group(scenario, members, dynamic=False)

    assert group.player_id.startswith(PLAYER_ID_PREFIX)
    assert group.type == PlayerType.GROUP
    assert group.name == "Group Z"
    protocol = _group_protocol(scenario, group.player_id)
    assert protocol.protocol_id != group.player_id
    config = scenario.mass.config
    assert config.get_protocol_value(protocol.protocol_id, CONF_GROUP_MEMBERS) == members
    assert protocol.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS) is False
    assert config.get_player_value(group.player_id, CONF_GROUP_MEMBERS) is None
    assert group.static_group_members == members


async def test_a_new_group_leaves_out_a_member_that_still_needs_setup(scenario: Scenario) -> None:
    """A device that answers but still has to be set up is no member of a new group."""
    members = await _members(scenario)
    await scenario.report(scenario.protocol("member_b"), needs_setup=True)

    group = await _create_group(scenario, members, dynamic=False)

    assert group.static_group_members == members[:1]


async def test_a_new_group_advertises_power_with_the_fake_power_control(
    scenario: Scenario,
) -> None:
    """The power control of the group's player decides whether the group protocol offers power."""
    group = await _create_group(scenario, await _members(scenario), dynamic=True)
    protocol = _group_protocol(scenario, group.player_id)
    assert PlayerFeature.POWER not in protocol.supported_features

    await scenario.configure_player(group.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})

    assert PlayerFeature.POWER in protocol.supported_features
    assert PlayerFeature.POWER in group.supported_features


async def test_a_new_group_comes_back_with_its_members_after_a_restart(
    scenario: Scenario,
) -> None:
    """After a restart the group is the same player again, with the same members and power."""
    members = await _members(scenario)
    group = await _create_group(scenario, members, dynamic=False)
    protocol_id = _group_protocol(scenario, group.player_id).protocol_id
    await scenario.configure_player(group.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})

    await scenario.restart()
    await scenario.idle()

    state = scenario.player(group.player_id)
    assert state is not None
    assert state.available
    assert state.name == "Group Z"
    assert state.static_group_members == members
    assert state.group_members == members
    protocol = _group_protocol(scenario, group.player_id)
    assert protocol.protocol_id == protocol_id
    assert PlayerFeature.POWER in protocol.supported_features


async def test_a_disabled_group_comes_back_when_enabled(scenario: Scenario) -> None:
    """Enabling a disabled group lets the provider register it again."""
    group = await _create_group(scenario, await _members(scenario), dynamic=True)
    await scenario.disable(group.player_id)
    assert scenario.player(group.player_id) is None

    await scenario.enable(group.player_id)
    await scenario.advance(15)
    await scenario.idle()

    state = scenario.player(group.player_id)
    assert state is not None
    assert state.available


async def test_a_new_group_is_announced_with_its_members(scenario: Scenario) -> None:
    """The player of a new dynamic group is announced with the members it was created with."""
    members = await _members(scenario)
    announced: list[tuple[list[str], bool]] = []
    signal_event = scenario.mass.signal_event

    def _record(event: EventType, object_id: str | None = None, data: Any = None) -> None:
        # the state as it is announced, not as it is by the time a subscriber runs
        if event == EventType.PLAYER_ADDED and isinstance(data, Player):
            announced.append(
                (
                    list(data.group_members),
                    PlayerFeature.SET_MEMBERS in data.supported_features,
                )
            )
        signal_event(event, object_id, data)

    with patch.object(scenario.mass, "signal_event", side_effect=_record):
        await _create_group(scenario, members, dynamic=True)

    assert announced == [(members, True)]


async def test_discovery_attaches_a_group_that_is_stored_without_a_player(
    scenario: Scenario,
) -> None:
    """A group whose settings were stored before it got a player comes up when discovered."""
    members = await _members(scenario)
    config = scenario.mass.config
    config.create_protocol_record(
        "sgp_stored",
        provider="sync_group",
        player_id=None,
        role=ProtocolRole.VIRTUAL,
        default_name="Group Y",
    )
    config.set_protocol_value("sgp_stored", CONF_GROUP_MEMBERS, members)
    config.set_protocol_value("sgp_stored", CONF_DYNAMIC_GROUP_MEMBERS, False)
    provider = scenario.mass.get_provider("sync_group")
    assert isinstance(provider, PlayerProvider)

    await provider.discover_players()
    await scenario.idle()

    protocol = scenario.mass.players.get_protocol("sgp_stored")
    assert protocol is not None
    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.name == "Group Y"
    assert protocol.player.static_group_members == members


async def test_discovery_leaves_an_attached_group_alone(scenario: Scenario) -> None:
    """Discovering again keeps the attached group object and attaches nothing else."""
    group = await _create_group(scenario, await _members(scenario), dynamic=True)
    protocol = _group_protocol(scenario, group.player_id)
    provider = scenario.mass.get_provider("sync_group")
    assert isinstance(provider, PlayerProvider)

    with patch.object(
        scenario.mass.players, "register", wraps=scenario.mass.players.register
    ) as attach:
        await provider.discover_players()

    attach.assert_not_called()
    assert _group_protocol(scenario, group.player_id) is protocol


@pytest.mark.parametrize("command", ["players/remove", "players/remove_group_player"])
async def test_removing_a_group_forgets_it(scenario: Scenario, command: str) -> None:
    """A group removed by either command is gone with its records and does not come back."""
    group = await _create_group(scenario, await _members(scenario), dynamic=True)
    protocol = _group_protocol(scenario, group.player_id)

    with (
        patch.object(SyncGroupPlayer, "on_remove", autospec=True) as on_remove,
        patch.object(PlayerProvider, "schedule_discovery", autospec=True) as schedule_discovery,
    ):
        if command == "players/remove":
            await scenario.mass.players.remove(group.player_id)
        else:
            await scenario.mass.players.remove_group_player(group.player_id)
        await scenario.settle()
    await scenario.idle()

    on_remove.assert_awaited_once_with(protocol)
    schedule_discovery.assert_not_called()
    assert scenario.player(group.player_id) is None
    assert scenario.mass.config.get_protocol_record(protocol.protocol_id) is None
    assert await scenario.player_config(group.player_id) is None
    provider = scenario.mass.get_provider("sync_group")
    assert isinstance(provider, PlayerProvider)
    await provider.discover_players()
    await scenario.idle()
    assert scenario.player(group.player_id) is None
    assert scenario.mass.players.get_protocol(protocol.protocol_id) is None
