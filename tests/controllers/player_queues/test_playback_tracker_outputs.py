"""Tests for player queue output membership tracking."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.models.player import Player
from tests.common import MockProvider, attach_mock, mock_player_mass


def test_output_player_ids_name_the_player_of_a_protocol() -> None:
    """Processing membership names the player a protocol is attached to, not the protocol."""
    mass = mock_player_mass()
    attach_mock(MockProvider("test", mass=mass), "protocol-1", player_id="player-1")
    tracker = cast("Any", SimpleNamespace(mass=mass))
    player = MagicMock(player_id="queue-1")
    player.group_members = ["queue-1", "protocol-1", "player-1", "missing-player"]

    result = PlaybackTrackerMixin._get_output_player_ids(
        tracker,
        cast("Player", player),
    )

    assert result == {"queue-1", "player-1", "missing-player"}


def test_output_player_ids_name_a_player_also_when_a_protocol_of_another_has_its_id() -> None:
    """A member of the state is a player, also when another player has a protocol of its id."""
    mass = mock_player_mass()
    attach_mock(MockProvider("airplay", mass=mass), "airplay_living", player_id="wiim_living")
    attach_mock(MockProvider("wiim", mass=mass), "wiim_living")
    tracker = cast("Any", SimpleNamespace(mass=mass))
    player = MagicMock(player_id="group-1")
    player.group_members = ["wiim_living"]

    result = PlaybackTrackerMixin._get_output_player_ids(tracker, cast("Player", player))

    assert result == {"group-1", "wiim_living"}


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
    player = MagicMock()
    player.resolve_output_protocol.return_value.current_media = SimpleNamespace(
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
    player = MagicMock()
    player.resolve_output_protocol.return_value.current_media = SimpleNamespace(
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
