"""Tests for removing a player, which the players controller does for a player of any provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
)
from music_assistant.providers import sync_group, universal_group
from tests.scenarios.harness import Event

if TYPE_CHECKING:
    from tests.scenarios.harness import FakePlayerProvider, FakeProtocol, Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C2"}
DEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C3"}


async def test_a_player_without_a_removal_hook_of_its_provider_is_removable(
    scenario: Scenario,
) -> None:
    """A player is removed by the controller, whatever its provider knows about removal."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id)
    scenario.clear_events()

    await scenario.remove(player_id)

    assert hooks[0] == "on_detach"
    _assert_removed(scenario, player_id, speaker)
    assert scenario.events(EventType.PLAYER_REMOVED) == [Event(EventType.PLAYER_REMOVED, player_id)]


async def test_the_removal_hook_is_called_once_the_player_is_gone(scenario: Scenario) -> None:
    """The hook of the protocol runs after it was detached and the configs were deleted."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id)

    await scenario.remove(player_id)

    assert hooks == ["on_detach", {"attached": False, "registered": False, "config": False}]
    _assert_removed(scenario, player_id, speaker)


@pytest.mark.parametrize(
    ("player_type", "loaded", "expected_requests"),
    [
        pytest.param(PlayerType.PLAYER, True, 1, id="player"),
        pytest.param(PlayerType.GROUP, True, 0, id="group"),
        pytest.param(PlayerType.PLAYER, False, 0, id="provider-not-loaded"),
    ],
)
async def test_the_provider_looks_again_for_the_device_of_a_removed_player(
    scenario: Scenario, player_type: PlayerType, loaded: bool, expected_requests: int
) -> None:
    """The loaded provider of a removed player looks for devices again; a removed group is gone."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, type=player_type)
    player_id = await _register_with_settings(scenario, speaker)
    discovery_requests = _count_discovery_requests(sonos)
    if not loaded:
        await scenario.mass.unload_provider(sonos.instance_id)
        await scenario.settle()

    await scenario.remove(player_id)

    assert discovery_requests == [None] * expected_requests
    _assert_removed(scenario, player_id, speaker)


async def test_a_member_of_an_active_group_leaves_it_before_it_is_detached(
    scenario: Scenario,
) -> None:
    """A removed player is taken out of its active group while the group can still reach it."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    group, group_id = await _active_group_of(scenario, sonos, speaker)
    left_while_attached = _record_leaving_members(scenario, group)
    hooks = _record_hooks(scenario, speaker, player_id)

    await scenario.remove(player_id)

    assert left_while_attached == {player_id: True}
    assert len([hook for hook in hooks if isinstance(hook, dict)]) == 1
    _assert_removed(scenario, player_id, speaker)
    assert scenario.player(group_id) is not None


@pytest.mark.parametrize(
    "leave_error",
    [
        PlayerUnavailableError("member is not available"),
        PlayerCommandFailed("member is static"),
        UnsupportedFeaturedException("group is not dynamic"),
    ],
)
async def test_a_group_that_can_not_let_the_member_go_does_not_keep_the_player(
    scenario: Scenario, leave_error: Exception
) -> None:
    """A member its active group refuses to release, or can not reach, is removed all the same."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    group, _group_id = await _active_group_of(scenario, sonos, speaker)
    _record_leaving_members(scenario, group, leave_error)
    hooks = _record_hooks(scenario, speaker, player_id)

    await scenario.remove(player_id)

    assert len([hook for hook in hooks if isinstance(hook, dict)]) == 1
    _assert_removed(scenario, player_id, speaker)


async def test_a_failing_removal_hook_does_not_keep_the_player(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that fails to clean up its own leaves the player removed all the same."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id, RuntimeError("device unreachable"))
    scenario.expect_log("Error removing protocol RINCON_KITCHEN")

    with caplog.at_level(logging.ERROR):
        await scenario.remove(player_id)

    assert len([hook for hook in hooks if isinstance(hook, dict)]) == 1
    _assert_removed(scenario, player_id, speaker)
    assert "Error removing protocol RINCON_KITCHEN" in caplog.text


async def test_the_removal_hook_is_not_called_when_its_provider_removes_the_protocol(
    scenario: Scenario,
) -> None:
    """A provider that forgets its own device gets no removal hook; the player goes with it."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id)

    await scenario.mass.players.remove_protocol("RINCON_KITCHEN")
    await scenario.settle()

    assert hooks == ["on_detach"]
    _assert_removed(scenario, player_id, speaker)


async def test_removing_the_config_of_an_attached_player_removes_the_player(
    scenario: Scenario,
) -> None:
    """Removing a player's config is removing the player, also while it is attached."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id)

    await scenario.mass.config.remove_player_config(player_id)
    await scenario.settle()

    assert len([hook for hook in hooks if isinstance(hook, dict)]) == 1
    _assert_removed(scenario, player_id, speaker)


@pytest.mark.parametrize("command", ["players/remove", "players/remove_group_player"])
async def test_a_group_player_is_removed_like_any_other_player(
    scenario: Scenario, command: str
) -> None:
    """A group goes with either command through the same path, hook included."""
    sonos = await scenario.add_provider("sonos")
    group = sonos.protocol("group_1", "Group", role="virtual", type=PlayerType.GROUP)
    group_id = await _register_with_settings(scenario, group)
    hooks = _record_hooks(scenario, group, group_id)

    if command == "players/remove":
        await scenario.mass.players.remove(group_id)
    else:
        await scenario.mass.players.remove_group_player(group_id)
    await scenario.settle()

    assert len([hook for hook in hooks if isinstance(hook, dict)]) == 1
    _assert_removed(scenario, group_id, group)


async def test_the_group_command_only_removes_group_players(scenario: Scenario) -> None:
    """The group command refuses a player that is no group and leaves it alone."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    player_id = await _register_with_settings(scenario, speaker)
    hooks = _record_hooks(scenario, speaker, player_id)

    with pytest.raises(UnsupportedFeaturedException, match="is not a group player"):
        await scenario.mass.players.remove_group_player(player_id)

    assert hooks == []
    assert scenario.player_of(speaker) == player_id
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}") is not None
    assert scenario.mass.config.get_protocol_record("RINCON_KITCHEN") is not None


@pytest.mark.parametrize("module", [sync_group, universal_group])
def test_the_group_providers_still_offer_the_removal_of_groups(module: object) -> None:
    """The providers of the groups a user creates keep advertising their removal."""
    features: set[ProviderFeature] = module.SUPPORTED_FEATURES  # type: ignore[attr-defined]

    assert ProviderFeature.REMOVE_GROUP_PLAYER in features


async def _register_with_settings(scenario: Scenario, protocol: FakeProtocol) -> str:
    """
    Register a new device and give its player DSP and queue settings; return the player id.

    :param scenario: The scenario.
    :param protocol: The protocol of the device.
    """
    await scenario.register(protocol)
    player_id = scenario.single_player(protocol).player_id
    scenario.mass.config.set(f"{CONF_PLAYER_DSP}/{player_id}", {"enabled": True})
    scenario.mass.config.set(
        f"{CONF_PLAYER_QUEUES}/{player_id}", {"queue_id": player_id, "values": {}}
    )
    assert scenario.queue(player_id) is not None
    return player_id


async def _active_group_of(
    scenario: Scenario, provider: FakePlayerProvider, member_protocol: FakeProtocol
) -> tuple[FakeProtocol, str]:
    """
    Attach a powered group that holds a player; return its protocol and its player id.

    :param scenario: The scenario.
    :param provider: The provider of the group.
    :param member_protocol: The protocol of the player the group holds.
    """
    member_id = scenario.player_of(member_protocol)
    assert member_id is not None
    group = provider.protocol(
        "group_1", "Group", role="virtual", identifiers=DEN, type=PlayerType.GROUP
    )
    await scenario.register(group)
    group_id = scenario.single_player(group).player_id
    await scenario.report(group, powered=True, group_members=[group_id, member_id])
    await scenario.report(member_protocol)
    member = scenario.player(member_id)
    assert member is not None
    assert member.active_group == group_id
    return group, group_id


def _record_leaving_members(
    scenario: Scenario, group: FakeProtocol, leave_error: Exception | None = None
) -> dict[str, bool]:
    """
    Record whether each member taken out of a group still had its protocol attached then.

    :param scenario: The scenario.
    :param group: The protocol of the group.
    :param leave_error: What the group raises when a member is taken out.
    """
    left_while_attached: dict[str, bool] = {}
    set_members = group.set_members

    async def _set_members(
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        for member_id in player_ids_to_remove or []:
            member = scenario.mass.players.get_player(member_id)
            left_while_attached[member_id] = member is not None and bool(member.protocols)
        if leave_error:
            raise leave_error
        await set_members(player_ids_to_add, player_ids_to_remove)

    group.set_members = _set_members  # type: ignore[method-assign]
    return left_while_attached


def _record_hooks(
    scenario: Scenario,
    protocol: FakeProtocol,
    player_id: str,
    remove_error: Exception | None = None,
) -> list[str | dict[str, bool]]:
    """
    Record the detach and removal hooks of a protocol, and what the removal found then.

    :param scenario: The scenario.
    :param protocol: The protocol object.
    :param player_id: Id of its player.
    :param remove_error: What the removal hook raises.
    """
    calls: list[str | dict[str, bool]] = []
    on_detach = protocol.on_detach
    on_remove = protocol.on_remove

    async def _on_detach() -> None:
        calls.append("on_detach")
        await on_detach()

    async def _on_remove() -> None:
        config = scenario.mass.config
        calls.append(
            {
                "attached": protocol.player is not None,
                "registered": scenario.mass.players.get_player(player_id) is not None,
                "config": config.get(f"{CONF_PLAYERS}/{player_id}") is not None
                or config.get_protocol_record(protocol.protocol_id) is not None,
            }
        )
        if remove_error:
            raise remove_error
        await on_remove()

    protocol.on_detach = _on_detach  # type: ignore[method-assign]
    protocol.on_remove = _on_remove  # type: ignore[method-assign]
    return calls


def _count_discovery_requests(provider: FakePlayerProvider) -> list[None]:
    """
    Record the requests of the players controller to a provider to look for devices again.

    :param provider: The provider.
    """
    requests: list[None] = []

    def _schedule_discovery() -> None:
        requests.append(None)

    provider.schedule_discovery = _schedule_discovery  # type: ignore[method-assign]
    return requests


def _assert_removed(scenario: Scenario, player_id: str, protocol: FakeProtocol) -> None:
    """
    Assert that the player and everything stored for it and its protocol is gone.

    :param scenario: The scenario.
    :param player_id: Id of the removed player.
    :param protocol: Its protocol.
    """
    assert scenario.mass.players.get_player(player_id) is None
    assert scenario.mass.players.get_protocol(protocol.protocol_id) is None
    assert protocol.player is None
    assert scenario.queue(player_id) is None
    for tree in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        assert scenario.mass.config.get(f"{tree}/{player_id}") is None
    assert scenario.mass.config.get(f"{CONF_PLAYER_PROTOCOLS}/{protocol.protocol_id}") is None
