"""Tests for the Teufel Raumfeld end-of-track advancement state machine."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant.providers.raumfeld.player import RaumfeldPlayer

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia


def _make_player() -> tuple[RaumfeldPlayer, MagicMock, MagicMock]:
    """Build a RaumfeldPlayer (without the device-dependent __init__) plus its mocks."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player._advance_armed = False
    player._prev_playing = False
    player._next_media = None
    mass = MagicMock()
    play_media = MagicMock()
    player.mass = mass
    player.play_media = play_media  # type: ignore[method-assign]
    return player, mass, play_media


def _media() -> PlayerMedia:
    return cast("PlayerMedia", MagicMock())


def test_advances_to_next_when_track_ends() -> None:
    """A playing->stopped transition with a queued next item advances the queue."""
    player, mass, play_media = _make_player()
    player._advance_armed = True
    player._prev_playing = True
    next_media: PlayerMedia | None = _media()
    player._next_media = next_media

    player._maybe_advance(playing=False)

    play_media.assert_called_once_with(next_media)
    mass.create_task.assert_called_once()
    assert player._next_media is None
    assert player._prev_playing is False


def test_no_advance_during_startup_gap() -> None:
    """The not-yet-playing gap after a play command (prev_playing reset) never advances."""
    player, _mass, play_media = _make_player()
    player._advance_armed = True
    player._prev_playing = False  # as _mark_play_started leaves it right after a play
    player._next_media = _media()

    player._maybe_advance(playing=False)

    play_media.assert_not_called()
    assert player._next_media is not None


def test_no_advance_after_user_stop() -> None:
    """A user stop disarms advancement, so a stopped reading does nothing."""
    player, _mass, play_media = _make_player()
    player._advance_armed = False
    player._prev_playing = True
    player._next_media = _media()

    player._maybe_advance(playing=False)

    play_media.assert_not_called()


def test_disarms_when_track_ends_without_next() -> None:
    """A track ending with no next item disarms instead of restarting anything."""
    player, _mass, play_media = _make_player()
    player._advance_armed = True
    player._prev_playing = True
    player._next_media = None

    player._maybe_advance(playing=False)

    play_media.assert_not_called()
    assert player._advance_armed is False


def test_no_advance_while_still_playing() -> None:
    """While playback continues nothing advances and the playing flag is tracked."""
    player, _mass, play_media = _make_player()
    player._advance_armed = True
    player._prev_playing = True
    player._next_media = _media()

    player._maybe_advance(playing=True)

    play_media.assert_not_called()
    assert player._prev_playing is True
