"""Tests for which source a player is resolved to be playing."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from tests.common import MockPlayer, MockProvider

PLAYER_ID = "player_1"


def _create_player() -> MockPlayer:
    """Create a player that was playing the Music Assistant queue."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _player_id, volume: volume)
    provider = MockProvider("test_provider", mass=mass)
    player = MockPlayer(provider, PLAYER_ID, "Player 1")
    player.set_active_mass_source(PLAYER_ID)
    return player


@pytest.mark.parametrize(
    "resumed_state", [PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.IDLE]
)
def test_ma_source_expires_only_if_player_remains_idle(
    resumed_state: PlaybackState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the group queue selected when playback resumes before the idle timeout."""
    player = _create_player()
    pending: dict[str, Callable[[], None]] = {}
    source_timer = f"set_mass_source_{PLAYER_ID}"

    def schedule(
        _delay: float,
        callback: Callable[..., None],
        *args: object,
        task_id: str | None = None,
        **kwargs: object,
    ) -> None:
        if task_id == source_timer:
            pending[task_id] = partial(callback, *args, **kwargs)

    monkeypatch.setattr(player.mass, "call_later", schedule)
    monkeypatch.setattr(player.mass, "cancel_timer", lambda task_id: pending.pop(task_id, None))
    player.set_active_mass_source("group_queue")
    player._attr_playback_state = PlaybackState.PLAYING
    player.update_state(signal_event=False)
    player._attr_playback_state = PlaybackState.IDLE
    player.update_state(signal_event=False)
    player._attr_playback_state = resumed_state
    player.update_state(signal_event=False)

    for callback in list(pending.values()):
        callback()

    expected_source = PLAYER_ID if resumed_state == PlaybackState.IDLE else "group_queue"
    assert player.state.active_source == expected_source
