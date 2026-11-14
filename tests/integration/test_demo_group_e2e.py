"""
End-to-end demo: a sync group of fake players mirrors the leader's media.

Exercises the full stack with no hardware/network: form a sync group of demo
players, play a fake `test`-provider track, and assert the group's current media
propagates to every member.
"""

from __future__ import annotations

from functools import partial

import pytest

from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player

from .conftest import demo_players, group_players, play_test_track, wait_for


def _mirrors_uri(player: Player, uri: str) -> bool:
    """Return True if the player's current media matches the given uri."""
    current = player.current_media
    return current is not None and current.uri == uri


@pytest.mark.asyncio
async def test_demo_group_mirrors_current_media(e2e_mass: MusicAssistant) -> None:
    """A grouped set of demo players all report the sync leader's current media."""
    # hermetic boot: only the fake demo players exist (no real LAN devices discovered)
    players = demo_players(e2e_mass)
    assert {p.player_id for p in e2e_mass.players} == {p.player_id for p in players}
    assert [p.own_protocol.protocol_id for p in players if p.own_protocol] == [
        "demo_0",
        "demo_1",
        "demo_2",
    ]
    assert len(players) >= 3
    leader, *members = players[:3]

    # 1. form the sync group
    await group_players(e2e_mass, leader, members)
    assert await wait_for(lambda: len(leader.group_members) == 3), (
        f"group not formed: {leader.group_members}"
    )
    assert leader.group_members[0] == leader.player_id
    for member in members:
        assert member.synced_to == leader.player_id

    # 2. play a fake track (carries artwork) on the group
    await play_test_track(e2e_mass, leader.player_id)
    assert await wait_for(lambda: leader.current_media is not None), (
        "leader current_media never populated"
    )
    leader_media = leader.current_media
    assert leader_media is not None
    assert leader_media.image_url

    # 3. every member mirrors the leader's media end-to-end
    for member in members:
        assert await wait_for(partial(_mirrors_uri, member, leader_media.uri)), (
            f"{member.player_id} did not mirror the leader's media"
        )
        member_media = member.current_media
        assert member_media is not None
        assert member_media.image_url == leader_media.image_url
