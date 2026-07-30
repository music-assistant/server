"""Tests for the Player.update_state change-detection path."""

from __future__ import annotations

import copy
import time
from statistics import median
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState

import music_assistant.models.player as player_module
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    # no queue registered: current_media resolves from the player's native media
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _player_id, volume: volume)
    return mass


@pytest.fixture
def player(mock_mass: MagicMock) -> MockPlayer:
    """Create a playing player with its state calculated once."""
    provider = MockProvider("test_provider", mass=mock_mass)
    player = MockPlayer(provider, "player_1", "Player 1")
    now = time.time()
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_volume_level = 20
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = now
    player.set_current_media(uri="http://test/stream", title="Test")
    player.update_state(signal_event=False)
    return player


class TestUpdateStateChangeDetection:
    """The no-change update_state path does no work; changed inputs recalculate."""

    def test_no_change_update_does_no_work(self, player: MockPlayer) -> None:
        """An update_state call without any changed input builds nothing."""
        with (
            patch.object(
                player_module, "PlayerState", wraps=player_module.PlayerState
            ) as state_cls,
            patch.object(copy, "deepcopy", wraps=copy.deepcopy) as deepcopy_mock,
        ):
            player.update_state()

        state_cls.assert_not_called()
        deepcopy_mock.assert_not_called()

    def test_position_tick_does_not_rebuild(self, player: MockPlayer) -> None:
        """A regular playback tick keeps the anchor and builds nothing."""
        assert player._attr_elapsed_time is not None
        assert player._attr_elapsed_time_last_updated is not None
        player._attr_elapsed_time += 1
        player._attr_elapsed_time_last_updated += 1

        with patch.object(
            player_module, "PlayerState", wraps=player_module.PlayerState
        ) as state_cls:
            player.update_state()

        state_cls.assert_not_called()
        assert player.state.elapsed_time == 10.0

    def test_position_jump_rebuilds_state(self, player: MockPlayer) -> None:
        """A corrected-position jump (seek) adopts the new anchor."""
        player._attr_elapsed_time = 61.0
        player._attr_elapsed_time_last_updated = time.time()

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 61.0

    def test_changed_input_rebuilds_state(self, player: MockPlayer) -> None:
        """A changed player attribute is picked up by the next update_state call."""
        player._attr_volume_level = 55

        with patch.object(
            player_module, "PlayerState", wraps=player_module.PlayerState
        ) as state_cls:
            player.update_state(signal_event=False)

        state_cls.assert_called_once()
        assert player.state.volume_level == 55

    def test_mark_state_dirty_forces_recalculation(self, player: MockPlayer) -> None:
        """mark_state_dirty recalculates even when no own input changed."""
        with patch.object(
            player_module, "PlayerState", wraps=player_module.PlayerState
        ) as state_cls:
            player.mark_state_dirty()
            player.update_state(signal_event=False)

        state_cls.assert_called_once()


class TestMediaUpdatedCallback:
    """The (debounced) media-updated callback fires on media identity changes."""

    def test_palette_resolution_fires_media_updated(
        self, mock_mass: MagicMock, player: MockPlayer
    ) -> None:
        """A late palette resolution re-fires the media-updated callback."""
        player.set_current_media(uri="http://test/stream", title="Test", image_url="http://img")
        player.update_state(signal_event=False)
        mock_mass.call_later.reset_mock()

        player.set_resolved_palette("http://img", MagicMock())
        player.update_state(force_update=True, signal_event=False)

        assert any(
            call.kwargs.get("task_id") == f"player_media_updated_{player.player_id}"
            for call in mock_mass.call_later.call_args_list
        )


class TestCacheInvalidationClasses:
    """Config-derived cached properties survive state updates, all others refresh."""

    def test_config_cached_props_survive_state_updates(self, player: MockPlayer) -> None:
        """Config-derived cached properties are not recomputed on state updates."""
        assert "icon" in player._cache
        marker = player._cache["icon"]
        player._attr_volume_level = 60
        player.update_state(signal_event=False)
        assert player._cache.get("icon") is marker

    def test_set_config_invalidates_all_cached_props(self, player: MockPlayer) -> None:
        """set_config invalidates every cached property, including config-derived ones."""
        assert "icon" in player._cache
        player.set_config(player.config)
        assert len(player._cache) == 0

    def test_player_implementation_cached_props_cleared_each_update(
        self, player: MockPlayer
    ) -> None:
        """Cached properties defined by player implementations refresh on every update."""
        player._cache["some_provider_prop"] = object()
        player.update_state()
        assert "some_provider_prop" not in player._cache


class TestUpdateStateTiming:
    """Micro-benchmarks guarding the cost of the update_state paths."""

    def test_no_change_update_is_fast(self, player: MockPlayer) -> None:
        """The no-change path completes well within the microsecond budget."""
        for _ in range(50):  # warmup
            player.update_state()
        timings = []
        for _ in range(200):
            start = time.perf_counter()
            player.update_state()
            timings.append(time.perf_counter() - start)
        duration = median(timings)
        # target is <50us on a dev machine; assert with generous CI headroom
        assert duration < 0.001, f"no-change update_state took {duration * 1e6:.0f}us (median)"

    def test_full_update_is_fast(self, player: MockPlayer) -> None:
        """A full recalculation completes well within the microsecond budget."""
        for volume in range(50):  # warmup
            player._attr_volume_level = volume
            player.update_state(signal_event=False)
        timings = []
        for volume in range(200):
            player._attr_volume_level = volume % 100
            start = time.perf_counter()
            player.update_state(signal_event=False)
            timings.append(time.perf_counter() - start)
        duration = median(timings)
        # target is <500us on a dev machine; assert with generous CI headroom
        assert duration < 0.005, f"full update_state took {duration * 1e6:.0f}us (median)"
