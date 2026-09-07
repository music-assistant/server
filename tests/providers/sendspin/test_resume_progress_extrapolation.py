"""
Tests for progress extrapolation across a resume-from-stop gap.

Pausing a Sendspin group falls back to STOP. On the first metadata push after
resume, PlayerMedia.corrected_elapsed_time still extrapolates from the pre-stop
elapsed_time_last_updated, which would report the entire stopped period (hours
or days) as track progress. The computation must fall back to the retained
elapsed time when the extrapolation window is not fresh.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _compute(current_media: PlayerMedia, *, is_playing: bool) -> int:
    """Run the real method against a mocked player."""
    mock = MagicMock()
    mock.corrected_elapsed_time = None
    mock.elapsed_time = None
    return SendspinPlayer._compute_track_progress_ms(mock, current_media, is_playing=is_playing)


def _media(elapsed: int | None, *, updated_ago: float | None = None) -> PlayerMedia:
    return PlayerMedia(
        uri="library://track/1",
        elapsed_time=elapsed,
        elapsed_time_last_updated=time.time() - updated_ago if updated_ago is not None else None,
    )


def test_fresh_extrapolation_is_trusted() -> None:
    """A few seconds of interpolation past the last update is normal playback."""
    result = _compute(_media(145, updated_ago=2.5), is_playing=True)
    assert 147_000 <= result <= 148_500


def test_stale_extrapolation_falls_back_to_retained_elapsed() -> None:
    """A days-old anchor means the gap is stopped time, not track progress."""
    five_days = 5 * 24 * 3600.0
    assert _compute(_media(145, updated_ago=five_days), is_playing=True) == 145_000


def test_not_playing_keeps_the_fixed_position() -> None:
    """Paused/idle positions must not advance at all."""
    assert _compute(_media(145, updated_ago=432_000.0), is_playing=False) == 145_000


def test_missing_media_elapsed_falls_back_to_player() -> None:
    """Without media bookkeeping, the player-level elapsed is used."""
    mock = MagicMock()
    mock.corrected_elapsed_time = 12.0
    mock.elapsed_time = 10.0
    result = SendspinPlayer._compute_track_progress_ms(mock, _media(None), is_playing=True)
    assert result == 12_000
