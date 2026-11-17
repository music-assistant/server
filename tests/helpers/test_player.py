"""Tests for the player helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent
from music_assistant_models.player import PlayerMedia

from music_assistant.helpers.player import (
    PlayerAvailabilityTracker,
    get_available_player,
    served_duration,
)


def _make_mass(players: dict[str, bool]) -> Any:
    """
    Create a mass mock whose registry holds the given players.

    :param players: Registered player ids mapped to their (state) availability; the
        caller may change it afterwards.
    """
    mass = MagicMock()

    def _player(player_id: str) -> SimpleNamespace:
        return SimpleNamespace(player_id=player_id, available=players[player_id])

    mass.players.get_player.side_effect = lambda player_id: (
        _player(player_id) if player_id in players else None
    )
    mass.players.iter_players.side_effect = lambda **_kwargs: [
        _player(player_id) for player_id, available in players.items() if available
    ]
    return mass


def _event(event: EventType, player_id: str | None = "p1") -> MassEvent:
    """Build a player event."""
    return MassEvent(event=event, object_id=player_id)


def test_get_available_player_returns_an_available_player() -> None:
    """An available player is returned, an unavailable or unknown one is not."""
    mass = _make_mass({"p1": True, "p2": False})

    available = get_available_player(mass, "p1")
    assert available is not None
    assert available.player_id == "p1"
    assert get_available_player(mass, "p2") is None
    assert get_available_player(mass, "unknown") is None


def test_tracker_ignores_players_available_at_creation() -> None:
    """A player that is already available is not reported on its next event."""
    mass = _make_mass({"p1": True})
    tracker = PlayerAvailabilityTracker(mass)

    assert not tracker.became_available(_event(EventType.PLAYER_UPDATED))


def test_tracker_reports_a_registering_player_once() -> None:
    """A player that registers available is reported once, its later updates are not."""
    players: dict[str, bool] = {}
    mass = _make_mass(players)
    tracker = PlayerAvailabilityTracker(mass)

    players["p1"] = True
    assert tracker.became_available(_event(EventType.PLAYER_ADDED))
    assert not tracker.became_available(_event(EventType.PLAYER_UPDATED))


def test_tracker_reports_a_registered_player_turning_available() -> None:
    """A registered player that turns available again is reported."""
    players = {"p1": True}
    mass = _make_mass(players)
    tracker = PlayerAvailabilityTracker(mass)

    players["p1"] = False
    assert not tracker.became_available(_event(EventType.PLAYER_UPDATED))
    players["p1"] = True
    assert tracker.became_available(_event(EventType.PLAYER_UPDATED))


def test_tracker_reports_a_player_that_registers_again() -> None:
    """A player that was unregistered for a while is reported when it registers again."""
    players = {"p1": True}
    mass = _make_mass(players)
    tracker = PlayerAvailabilityTracker(mass)

    # a temporary unregister drops the player and fires PLAYER_UPDATED
    del players["p1"]
    assert not tracker.became_available(_event(EventType.PLAYER_UPDATED))
    players["p1"] = True
    assert tracker.became_available(_event(EventType.PLAYER_ADDED))


def test_tracker_treats_a_removal_as_unavailable() -> None:
    """A player that is added again after its removal is reported, also when still registered."""
    mass = _make_mass({"p1": True})
    tracker = PlayerAvailabilityTracker(mass)

    assert not tracker.became_available(_event(EventType.PLAYER_REMOVED))
    assert tracker.became_available(_event(EventType.PLAYER_ADDED))


def test_tracker_ignores_an_event_without_player() -> None:
    """An event without an object id reports nothing."""
    mass = _make_mass({})
    tracker = PlayerAvailabilityTracker(mass)

    assert not tracker.became_available(_event(EventType.PLAYER_ADDED, player_id=None))


_BASE_URL = "http://192.168.1.124:8097"
_FLOW_URL = f"{_BASE_URL}/flow/session1/queue1/q2/player1.flac"
_SINGLE_URL = f"{_BASE_URL}/single/session1/queue1/q2/player1.flac"


@pytest.mark.parametrize(
    ("url", "duration"), [(_FLOW_URL, 198), (_SINGLE_URL, 4)], ids=["flow", "single_item"]
)
def test_only_a_flow_gets_the_whole_track_after_a_seek(url: str, duration: int) -> None:
    """A stream of one item ends after the seconds left; a flow carries on into the next track."""
    media = PlayerMedia(uri="library://track/1", duration=198, stream_duration=4)

    assert served_duration(media, url) == duration


@pytest.mark.parametrize("url", [_FLOW_URL, _SINGLE_URL], ids=["flow", "single_item"])
def test_a_track_played_from_its_start_gets_its_duration(url: str) -> None:
    """Without a seek, the duration is the track's own."""
    media = PlayerMedia(uri="library://track/1", duration=198)

    assert served_duration(media, url) == 198


def test_media_without_a_duration_has_none() -> None:
    """A radio station or other endless stream has no duration to tell."""
    assert served_duration(PlayerMedia(uri="library://radio/1"), _FLOW_URL) is None
