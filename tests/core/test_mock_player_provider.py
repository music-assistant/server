"""Tests for the TrackingMockPlayer and MockPlayerProvider."""

from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

from tests.support.mock_player_provider import MockPlayerProvider, TrackingMockPlayer


def test_tracking_player_initial_state() -> None:
    """TrackingMockPlayer starts in Idle state with no current item."""
    provider = MockPlayerProvider(domain="test", mass=MagicMock())
    player = TrackingMockPlayer(provider=provider, player_id="p1", name="Test Player")
    assert player.playback_state == PlaybackState.IDLE
    assert player.current_item_id is None


def test_tracking_player_play_sets_state() -> None:
    """simulate_play transitions to Playing state."""
    provider = MockPlayerProvider(domain="test", mass=MagicMock())
    player = TrackingMockPlayer(provider=provider, player_id="p1", name="Test Player")
    player.simulate_play("track-1")
    assert player.playback_state == PlaybackState.PLAYING
    assert player.current_item_id == "track-1"


def test_tracking_player_stop_clears_state() -> None:
    """simulate_stop transitions back to Idle and clears current item."""
    provider = MockPlayerProvider(domain="test", mass=MagicMock())
    player = TrackingMockPlayer(provider=provider, player_id="p1", name="Test Player")
    player.simulate_play("track-1")
    player.simulate_stop()
    assert player.playback_state == PlaybackState.IDLE
    assert player.current_item_id is None


def test_tracking_player_pause() -> None:
    """simulate_pause transitions to Paused state."""
    provider = MockPlayerProvider(domain="test", mass=MagicMock())
    player = TrackingMockPlayer(provider=provider, player_id="p1", name="Test Player")
    player.simulate_play("track-1")
    player.simulate_pause()
    assert player.playback_state == PlaybackState.PAUSED
    assert player.current_item_id == "track-1"
