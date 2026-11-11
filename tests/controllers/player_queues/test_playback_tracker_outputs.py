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


def test_parse_current_item_id_ignores_items_no_longer_in_the_queue() -> None:
    """Only accept player-reported item IDs that belong to the queue."""
    get_item = MagicMock(return_value=None)
    tracker = cast(
        "Any",
        SimpleNamespace(
            mass=SimpleNamespace(players=SimpleNamespace(get_player=MagicMock())),
            get_item=get_item,
        ),
    )
    player = MagicMock(active_output_protocol="native")
    player.current_media = SimpleNamespace(
        source_id="queue-1",
        queue_item_id="replaced-away-item",
        uri=None,
    )

    result = PlaybackTrackerMixin._parse_player_current_item_id(
        tracker,
        "queue-1",
        cast("Player", player),
    )

    assert result is None
    get_item.assert_called_once_with("queue-1", "replaced-away-item")

    get_item.reset_mock()
    get_item.return_value = SimpleNamespace(queue_item_id="replaced-away-item")
    result = PlaybackTrackerMixin._parse_player_current_item_id(
        tracker,
        "queue-1",
        cast("Player", player),
    )
    assert result == "replaced-away-item"


def test_parse_current_item_id_ignores_stale_sonos_item_id() -> None:
    """A Sonos-reported item id is only trusted while the item remains in the queue."""
    get_item = MagicMock(return_value=None)
    tracker = cast(
        "Any",
        SimpleNamespace(
            mass=SimpleNamespace(players=SimpleNamespace(get_player=MagicMock())),
            get_item=get_item,
        ),
    )
    player = MagicMock(active_output_protocol="native")
    player.current_media = SimpleNamespace(
        source_id=None,
        queue_item_id="replaced-away-item",
        uri="mass:queue-1:replaced-away-item",
    )

    result = PlaybackTrackerMixin._parse_player_current_item_id(
        tracker,
        "queue-1",
        cast("Player", player),
    )

    assert result is None
    get_item.assert_called_once_with("queue-1", "replaced-away-item")
