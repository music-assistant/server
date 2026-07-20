"""Tests for player queue output membership tracking."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.models.player import Player


def test_output_player_ids_resolve_protocol_parents() -> None:
    """Processing membership uses user-facing protocol parent IDs."""
    get_player = MagicMock()
    tracker = cast(
        "Any",
        SimpleNamespace(mass=SimpleNamespace(players=SimpleNamespace(get_player=get_player))),
    )
    player = MagicMock(player_id="queue-1", protocol_parent_id=None)
    player.state.group_members = ["queue-1", "protocol-1", "player-1", "missing-player"]
    protocol_player = MagicMock(protocol_parent_id="player-1")
    get_player.side_effect = lambda player_id: (
        protocol_player if player_id == "protocol-1" else None
    )

    result = PlaybackTrackerMixin._get_output_player_ids(
        tracker,
        cast("Player", player),
    )

    assert result == {"queue-1", "player-1", "missing-player"}
