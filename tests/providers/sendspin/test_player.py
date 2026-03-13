"""Unit tests for Sendspin player pause support."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models.types import PlaybackStateType
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.providers.sendspin.player import SendspinPlayer


def _create_mock_player() -> SendspinPlayer:
    """Create a SendspinPlayer with mocked dependencies.

    Bypasses the real __init__ and sets up the minimum attributes
    needed for testing player commands.
    """
    player = object.__new__(SendspinPlayer)

    # Base Player class uses propcache which needs _cache
    player._cache = {}
    player._player_id = "test-player-id"
    player._attr_name = "Test Sendspin Player"
    player._config = MagicMock()
    player._config.name = None

    # Mock the attributes that pause/stop use
    player.logger = MagicMock()
    player.playback_session = MagicMock()
    player.playback_session.cancel = AsyncMock()
    player.update_state = MagicMock()  # type: ignore[method-assign,misc]
    player.mark_stop_called = MagicMock()  # type: ignore[method-assign,misc]
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 42.0
    player._attr_elapsed_time_last_updated = 0.0
    player._attr_current_media = MagicMock()

    # Mock the sendspin client API
    player.api = MagicMock()
    player.api.group.stop = AsyncMock()
    player.api.group._set_playback_state = MagicMock()

    # Mock the mass instance for queue resume
    player.mass = MagicMock()
    player.mass.player_queues.resume = AsyncMock()

    return player


class TestPauseFeature:
    """Tests for PlayerFeature.PAUSE support."""

    def test_pause_in_supported_features(self) -> None:
        """Verify PAUSE is declared in the supported features set."""
        player = _create_mock_player()
        player._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.MULTI_DEVICE_DSP,
        }
        assert PlayerFeature.PAUSE in player._attr_supported_features

    @pytest.mark.asyncio
    async def test_pause_sets_playback_state(self) -> None:
        """Pause should set playback state to PAUSED."""
        player = _create_mock_player()
        await player.pause()
        assert player._attr_playback_state == PlaybackState.PAUSED

    @pytest.mark.asyncio
    async def test_pause_updates_elapsed_time_timestamp(self) -> None:
        """Pause should freeze the elapsed time timestamp."""
        player = _create_mock_player()
        player._attr_elapsed_time_last_updated = 0.0
        await player.pause()
        assert player._attr_elapsed_time_last_updated > 0.0

    @pytest.mark.asyncio
    async def test_pause_calls_update_state(self) -> None:
        """Pause should notify MA of the state change."""
        player = _create_mock_player()
        await player.pause()
        player.update_state.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_pause_cancels_playback_session(self) -> None:
        """Pause should cancel the active playback session."""
        player = _create_mock_player()
        await player.pause()
        player.playback_session.cancel.assert_awaited_once_with("pause command")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_pause_sets_group_state_to_paused(self) -> None:
        """Pause should set the sendspin group playback state to PAUSED."""
        player = _create_mock_player()
        await player.pause()
        player.api.group._set_playback_state.assert_called_once_with(  # type: ignore[attr-defined]
            PlaybackStateType.PAUSED
        )

    @pytest.mark.asyncio
    async def test_pause_preserves_current_media(self) -> None:
        """Pause should NOT clear current_media (unlike stop)."""
        player = _create_mock_player()
        original_media = player._attr_current_media
        await player.pause()
        assert player._attr_current_media is original_media

    @pytest.mark.asyncio
    async def test_pause_does_not_call_group_stop(self) -> None:
        """Pause should NOT call group.stop() (that would set state to STOPPED)."""
        player = _create_mock_player()
        await player.pause()
        player.api.group.stop.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_play_resumes_via_queue(self) -> None:
        """Play (resume) should delegate to the queue controller."""
        player = _create_mock_player()
        player._attr_playback_state = PlaybackState.PAUSED
        await player.play()
        player.mass.player_queues.resume.assert_awaited_once_with(  # type: ignore[attr-defined]
            "test-player-id"
        )

    @pytest.mark.asyncio
    async def test_stop_differs_from_pause(self) -> None:
        """Stop should clear media and set IDLE, unlike pause."""
        player = _create_mock_player()
        await player.stop()
        assert player._attr_playback_state == PlaybackState.IDLE
        assert player._attr_current_media is None
        assert player._attr_elapsed_time == 0
        player.api.group.stop.assert_awaited_once()  # type: ignore[attr-defined]
