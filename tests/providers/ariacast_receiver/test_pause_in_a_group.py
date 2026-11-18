"""Tests for a pause from the AriaCast sender while the stream plays into a sync group."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature

from music_assistant.providers.ariacast_receiver import AriaCastReceiver
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


async def test_a_pause_stops_the_group_leader_and_keeps_the_group_formed(
    scenario: Scenario,
) -> None:
    """The sender's pause stops the player that fetches the stream; its sync group stays."""
    provider = await scenario.add_provider("airplay")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    devices = [
        provider.protocol(
            protocol_id,
            name,
            role="audio",
            identifiers={IdentifierType.MAC_ADDRESS: mac},
            supported_features=features,
        )
        for protocol_id, name, mac in (
            ("ap_a", "Player A", "00:11:22:33:44:0A"),
            ("ap_b", "Player B", "00:11:22:33:44:0B"),
        )
    ]
    for device in devices:
        device._attr_can_group_with = {provider.instance_id}
        await scenario.register(device)
    for device in devices:
        # what each can group with now includes the other
        device.update_state(force_update=True)
    await scenario.settle()
    members = [cast("str", scenario.player_of(device)) for device in devices]
    group = await scenario.mass.players.create_group_player("sync_group", "Group Z", members, False)
    await scenario.play(group.player_id)
    leader = next(
        player_id
        for player_id in members
        if scenario.player(player_id).group_members  # type: ignore[union-attr]
    )
    leader_protocol = devices[members.index(leader)]
    # the leader renders the stream over its audio protocol
    assert scenario.player(leader).active_output_protocol == leader_protocol.protocol_id  # type: ignore[union-attr]
    leader_protocol.commands.clear()
    receiver = SimpleNamespace(
        _session_player_id=leader,
        _active_player_id=group.player_id,
        _in_use_by_player=group.player_id,
        _is_playing=True,
        _forward_action=AsyncMock(),
        _broadcast_meta=AsyncMock(),
        mass=scenario.mass,
        logger=MagicMock(),
    )

    await AriaCastReceiver._cmd_pause(cast("AriaCastReceiver", receiver))
    await scenario.advance(5)

    assert ("stop",) in leader_protocol.commands
    assert scenario.player(leader).group_members == members  # type: ignore[union-attr]
    assert scenario.player(group.player_id).group_members == members  # type: ignore[union-attr]
    receiver._forward_action.assert_awaited_once_with("pause")
