"""Tests for the Sendspin group leader switch with a visualizer next in line (support#7029)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.sendspin import player as player_module
from music_assistant.providers.sendspin.player import SendspinPlayer, SendspinVisualizerPlayer

if TYPE_CHECKING:
    import pytest

_ROLES = {"living": ["player@v1"], "hue": ["visualizer@v1"], "badkamer": ["player@v1"]}


def _clients(*client_ids: str) -> list[MagicMock]:
    """Build Sendspin clients with the negotiated roles of the reported setup."""
    return [
        MagicMock(client_id=client_id, negotiated_role_ids=_ROLES[client_id])
        for client_id in client_ids
    ]


async def test_leader_hands_session_to_first_player_after_a_visualizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visualizer next in line is skipped so the session goes to the next audio player."""
    monkeypatch.setattr(player_module, "SendspinPlaybackSession", MagicMock())
    leader = MagicMock()
    leader.player_id = "living"
    leader.playback_session.playback_task.done.return_value = False
    leader.playback_session.transfer_to = AsyncMock()
    leader.api.group.remove_client = AsyncMock()
    leader.api.group.clients = _clients("living", "hue", "badkamer")
    session = leader.playback_session
    badkamer = MagicMock(spec=SendspinPlayer)
    players = {"living": leader, "hue": MagicMock(spec=SendspinVisualizerPlayer)}
    players["badkamer"] = badkamer
    leader.mass.players.get_player.side_effect = lambda player_id, *_args: players[player_id]

    await SendspinPlayer.set_members(leader, player_ids_to_remove=["living"])

    session.transfer_to.assert_awaited_once_with(badkamer)
    assert badkamer.playback_session is session


async def test_audio_player_leads_a_group_where_a_visualizer_comes_first() -> None:
    """The player that inherited the session keeps feeding the visualizer ahead of it."""
    badkamer = MagicMock()
    badkamer.player_id = "badkamer"
    badkamer._attr_group_members = []
    badkamer.playback_session.sync_members = AsyncMock()
    group = badkamer.api.group
    group.clients = _clients("hue", "badkamer")

    await SendspinPlayer._sync_membership_from_group(badkamer, group)

    assert badkamer._attr_group_members == ["badkamer", "hue"]
    badkamer.playback_session.sync_members.assert_awaited_once_with({"hue"})
