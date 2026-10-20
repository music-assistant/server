"""Tests for giving up on an external source that has been paused for too long."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT
from tests.common import MockPlayer, MockProvider

EXTERNAL_SOURCE = "spotify"


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _player_id, volume: volume)
    return mass


@pytest.fixture
def player(mock_mass: MagicMock) -> MockPlayer:
    """Create a player reporting a paused external source, with the check opted in."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player = MockPlayer(provider, "player_1", "Player 1")
    player._attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_active_source = EXTERNAL_SOURCE
    player.set_current_media(uri="spotify:track:1", title="Shout")
    player.update_state()
    return player


def _backdate_pause(player: MockPlayer, seconds: float) -> None:
    """Pretend the source has already been sitting paused for the given time."""
    player._Player__external_pause_since = time.time() - seconds  # type: ignore[attr-defined]


def test_a_source_that_just_paused_stays_resumable(
    player: MockPlayer, mock_mass: MagicMock
) -> None:
    """Test a real pause is still handed to the source, so pausing and resuming works."""
    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == EXTERNAL_SOURCE
    # the device reports nothing when the session goes stale, so we come back to it ourselves
    armed_checks = [
        call
        for call in mock_mass.call_later.call_args_list
        if call.kwargs.get("task_id") == "external_pause_player_1"
    ]
    assert len(armed_checks) == 1
    assert armed_checks[0].args[0] > EXTERNAL_PAUSE_IDLE_TIMEOUT


def test_a_source_paused_within_the_grace_period_stays_resumable(player: MockPlayer) -> None:
    """Test a genuine pause survives for as long as the grace period lasts."""
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT - 5)

    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == EXTERNAL_SOURCE
    assert player.state.current_media is not None


def test_a_source_paused_for_too_long_is_reported_as_idle(player: MockPlayer) -> None:
    """Test an abandoned session stops looking resumable, so play starts our own queue."""
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT + 1)

    player.update_state()

    assert player.state.playback_state is PlaybackState.IDLE
    assert player._attr_current_media is None
    # the player's own queue is reachable again, which is what makes the play button work
    assert player.state.active_source == player.player_id


def test_an_ended_source_is_not_picked_back_up_from_what_the_device_reports(
    player: MockPlayer,
) -> None:
    """Test the next update does not resurrect the source the device still reports as paused."""
    player.mark_external_source_ended()
    player.update_state()
    assert player.state.playback_state is PlaybackState.IDLE

    # the device keeps reporting the very same paused source on every update that follows
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_active_source = EXTERNAL_SOURCE
    player.update_state()

    assert player.state.playback_state is PlaybackState.IDLE
    assert player.state.active_source == player.player_id


def test_a_source_that_starts_playing_again_is_given_a_fresh_start(player: MockPlayer) -> None:
    """Test a source we gave up on is trusted again once the device really plays it."""
    player.mark_external_source_ended()
    player.update_state()

    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_active_source = EXTERNAL_SOURCE
    player.update_state()
    player._attr_playback_state = PlaybackState.PAUSED
    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == EXTERNAL_SOURCE


def test_another_source_is_not_tarred_with_the_same_brush(player: MockPlayer) -> None:
    """Test giving up on one source does not shorten the grace period of the next."""
    player.mark_external_source_ended()
    player.update_state()

    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_active_source = "tidal"
    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == "tidal"


def test_resuming_the_source_gives_a_later_pause_the_full_grace_period(
    player: MockPlayer,
) -> None:
    """Test the clock only runs while the player reports paused."""
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT - 5)
    player._attr_playback_state = PlaybackState.PLAYING
    player.update_state()

    player._attr_playback_state = PlaybackState.PAUSED
    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == EXTERNAL_SOURCE


def test_our_own_paused_playback_is_never_expired(player: MockPlayer) -> None:
    """Test pausing the Music Assistant queue is left alone, however long it lasts."""
    player._attr_active_source = player.player_id
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT + 1)

    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED


def test_a_paused_queue_source_is_never_expired(player: MockPlayer, mock_mass: MagicMock) -> None:
    """Test a source that resolves to a Music Assistant queue is not treated as external."""
    mock_mass.player_queues.get = MagicMock(return_value=MagicMock())
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT + 1)

    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED


def test_a_source_rendered_by_an_output_protocol_is_never_expired(player: MockPlayer) -> None:
    """Test playback that Music Assistant renders itself is left to the protocol player."""
    player.set_active_output_protocol("airplay_player_1")
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT + 1)

    player.update_state()

    assert player._attr_playback_state is PlaybackState.PAUSED
    assert player._attr_active_source == EXTERNAL_SOURCE


def test_a_player_that_does_not_opt_in_keeps_its_paused_source(
    mock_mass: MagicMock,
) -> None:
    """Test devices that report an abandoned source as stopped themselves are untouched."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player = MockPlayer(provider, "player_2", "Player 2")
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_active_source = EXTERNAL_SOURCE
    player.update_state()
    _backdate_pause(player, EXTERNAL_PAUSE_IDLE_TIMEOUT + 1)

    player.update_state()

    assert player.state.playback_state is PlaybackState.PAUSED
    assert player.state.active_source == EXTERNAL_SOURCE


@pytest.mark.asyncio
async def test_unloading_leaves_no_pending_check_behind(
    player: MockPlayer, mock_mass: MagicMock
) -> None:
    """Test unloading a player cancels the check that would re-evaluate its paused source."""
    await player.on_unload()

    mock_mass.cancel_timer.assert_any_call("external_pause_player_1")


@pytest.mark.asyncio
async def test_unloading_a_player_that_does_not_opt_in_cancels_nothing(
    mock_mass: MagicMock,
) -> None:
    """Test the check is not cleaned up for players that never armed it."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player = MockPlayer(provider, "player_2", "Player 2")

    await player.on_unload()

    mock_mass.cancel_timer.assert_not_called()
