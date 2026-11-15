"""Tests for the Snapcast player leaving a group that plays the stream of a sync group."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.sync_group.constants import SGP_PREFIX

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.snapcast.provider import SnapCastProvider

# the snapclients of the group the leader owns, by the protocol id of each
CLIENTS = {"ma_leader": "leader", "ma_member": "member", "ma_other": "other"}
# the sync group (a minted player id) plays its stream on the group; its members are players
SYNC_GROUP_ID = "pl0123456789ab"
SYNC_GROUP_MEMBERS = ["pl_leader", "pl_member"]


def _leader() -> tuple[SnapCastPlayer, MagicMock]:
    """Return the leader of a snapcast group that plays the stream of a sync group."""
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._protocol_id = "ma_leader"
    player._state_update_lock = asyncio.Lock()
    player._process_snapcast_client_state = AsyncMock(return_value=False)  # type: ignore[method-assign]
    group = MagicMock(clients=list(CLIENTS.values()), stream="sync-stream")
    provider = MagicMock()
    provider.ensure_player_owned_group = AsyncMock(return_value=group)
    provider.isolate_player_to_dedicated_group = AsyncMock()
    provider._get_ma_id.side_effect = {client: ma_id for ma_id, client in CLIENTS.items()}.get
    provider.get_snap_ma_stream.return_value = SimpleNamespace(
        media=SimpleNamespace(source_id=SYNC_GROUP_ID)
    )
    player._provider = cast("SnapCastProvider", provider)
    mass = MagicMock()
    sync_group = MagicMock(own_protocol=MagicMock(protocol_id=f"{SGP_PREFIX}abc"))
    sync_group.group_members = SYNC_GROUP_MEMBERS
    mass.players.get_player.side_effect = {SYNC_GROUP_ID: sync_group}.get
    mass.players.get_player_by_protocol.side_effect = {
        ma_id: SimpleNamespace(player_id=f"pl_{client}") for ma_id, client in CLIENTS.items()
    }.get
    player.mass = cast("MusicAssistant", mass)
    return player, provider


async def test_leaving_a_sync_group_keeps_its_other_members_together() -> None:
    """The leader leaves; a removed client outside the sync group goes first, members stay."""
    player, provider = _leader()

    await player.set_members(player_ids_to_remove=["ma_leader", "ma_member", "ma_other"])

    isolated = provider.isolate_player_to_dedicated_group.await_args_list
    assert [call.args for call in isolated[:-1]] == [("ma_other",)]
    assert isolated[-1].kwargs == {
        "target_player_id": "ma_leader",
        "target_stream_id": "default",
        "others_stream_id": "sync-stream",
    }
