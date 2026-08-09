"""
Tests for the capability flags on the implicit "Music Assistant Queue" source.

Clients drive their transport controls off these flags, so they must follow the queue:
with nothing queued there is nothing to play, seek or skip through, and a queue that played to
its end can only be started over.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.player import PlayerSource

from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    return mass


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", mass=mock_mass)


def _queue_source(player: MockPlayer) -> PlayerSource | None:
    """Return the implicit MA queue source from the player's FINAL source list."""
    sources = player._Player__final_source_list  # type: ignore[attr-defined]
    return next((x for x in sources if x.id == player.player_id), None)


def test_filled_queue_advertises_transport_controls(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A queue holding items can be played, seeked and skipped through."""
    player = MockPlayer(provider, "player_1", "Player")
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock(items=5, ended=False))

    source = _queue_source(player)

    assert source is not None
    assert source.can_play_pause is True
    assert source.can_seek is True
    assert source.can_next_previous is True


def test_empty_queue_reports_transport_controls_unavailable(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """An empty queue has nothing to play, so clients can grey the controls out."""
    player = MockPlayer(provider, "player_2", "Player")
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock(items=0, ended=False))

    source = _queue_source(player)

    assert source is not None
    assert source.can_play_pause is False
    assert source.can_seek is False
    assert source.can_next_previous is False


def test_finished_queue_only_advertises_play(mock_mass: MagicMock, provider: MockProvider) -> None:
    """A queue that played to its end can be started over, but has nothing left to seek or skip."""
    player = MockPlayer(provider, "player_5", "Player")
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock(items=5, ended=True))

    source = _queue_source(player)

    assert source is not None
    assert source.can_play_pause is True
    assert source.can_seek is False
    assert source.can_next_previous is False


def test_missing_queue_reports_transport_controls_unavailable(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A player without a registered queue behaves like an empty one."""
    player = MockPlayer(provider, "player_3", "Player")
    mock_mass.player_queues.get = MagicMock(return_value=None)

    source = _queue_source(player)

    assert source is not None
    assert source.can_play_pause is False


def test_provider_supplied_queue_source_is_left_alone(
    mock_mass: MagicMock, provider: MockProvider
) -> None:
    """A provider publishing its own source under the player id keeps its own flags."""
    player = MockPlayer(provider, "player_4", "Player")
    player._attr_source_list = [
        PlayerSource(id="player_4", name="Native", passive=False, can_play_pause=True)
    ]
    player._cache.clear()
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock(items=0, ended=False))

    source = _queue_source(player)

    assert source is not None
    assert source.name == "Native"
    assert source.can_play_pause is True
