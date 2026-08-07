"""Tests for the Player.update_state change-detection path."""

from __future__ import annotations

import copy
import time
from statistics import median
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

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


class TestNativeCurrentMediaPosition:
    """The published position of native current_media and the timestamp it is paired with."""

    def _playing_player(
        self,
        mock_mass: MagicMock,
        *,
        media_elapsed_time: int | None,
        media_last_updated: float | None,
        player_elapsed_time: float | None,
        player_last_updated: float | None,
    ) -> MockPlayer:
        """Build a playing player with the given media and player level position anchors."""
        provider = MockProvider("test_provider", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = player_elapsed_time
        player._attr_elapsed_time_last_updated = player_last_updated
        player.set_current_media(uri="http://test/stream", title="Test")
        assert player._attr_current_media is not None
        player._attr_current_media.elapsed_time = media_elapsed_time
        player._attr_current_media.elapsed_time_last_updated = media_last_updated
        player.update_state(signal_event=False)
        return player

    def test_zero_position_is_reported(self, mock_mass: MagicMock) -> None:
        """A position of zero (start of a track) is reported instead of being dropped."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=0,
            media_last_updated=anchor,
            player_elapsed_time=0,
            player_last_updated=anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 0
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_media_position_wins_without_player_position(self, mock_mass: MagicMock) -> None:
        """A media position is reported even when the player reports none of its own."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=42,
            media_last_updated=anchor,
            player_elapsed_time=None,
            player_last_updated=None,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_media_position_is_never_paired_with_the_player_timestamp(
        self, mock_mass: MagicMock
    ) -> None:
        """A media position without its own timestamp does not borrow the player's."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=42,
            media_last_updated=None,
            player_elapsed_time=10.0,
            player_last_updated=time.time(),
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated is None

    def test_late_arriving_position_is_adopted(self, mock_mass: MagicMock) -> None:
        """A position reported after the media was published still reaches the state."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=None,
            player_elapsed_time=None,
            player_last_updated=None,
        )
        published = player.state.current_media
        assert published is not None
        assert published.elapsed_time is None

        anchor = time.time()
        player._attr_elapsed_time = 5.0
        player._attr_elapsed_time_last_updated = anchor
        player.update_state(signal_event=False)

        published = player.state.current_media
        assert published is not None
        assert published.elapsed_time == 5
        assert published.elapsed_time_last_updated == anchor

    def test_steady_playback_keeps_the_previous_anchor(self, mock_mass: MagicMock) -> None:
        """A regular playback tick keeps the anchor it already published."""
        anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=None,
            player_elapsed_time=10.0,
            player_last_updated=anchor,
        )

        player._attr_elapsed_time = 11.0
        player._attr_elapsed_time_last_updated = anchor + 1
        player.update_state(signal_event=False)

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 10
        assert player.state.current_media.elapsed_time_last_updated == anchor

    def test_player_position_is_paired_with_its_own_timestamp(self, mock_mass: MagicMock) -> None:
        """Falling back to the player position also takes the player level timestamp."""
        player_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=player_anchor - 30,
            player_elapsed_time=42.7,
            player_last_updated=player_anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 42
        assert player.state.current_media.elapsed_time_last_updated == player_anchor

    def test_no_position_reports_no_timestamp(self, mock_mass: MagicMock) -> None:
        """Without any position, the timestamp is dropped along with it."""
        player = self._playing_player(
            mock_mass,
            media_elapsed_time=None,
            media_last_updated=time.time(),
            player_elapsed_time=None,
            player_last_updated=time.time(),
        )

        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time is None
        assert player.state.current_media.elapsed_time_last_updated is None


class TestStreamMetadataPosition:
    """The published position of a queue item carrying live stream metadata."""

    def _playing_player(
        self,
        mock_mass: MagicMock,
        *,
        metadata_elapsed_time: int | None,
        metadata_last_updated: float | None,
        queue_elapsed_time: float,
        queue_last_updated: float,
        media_type: MediaType = MediaType.RADIO,
    ) -> MockPlayer:
        """Build a playing player whose active queue item carries live stream metadata."""
        queue = PlayerQueue(
            queue_id="player_1",
            active=True,
            display_name="Player 1",
            available=True,
            items=1,
            elapsed_time=queue_elapsed_time,
            elapsed_time_last_updated=queue_last_updated,
            current_item=QueueItem(
                queue_id="player_1",
                queue_item_id="item_1",
                name="Live Radio",
                duration=None,
                streamdetails=StreamDetails(
                    provider="test_provider",
                    item_id="item_1",
                    audio_format=AudioFormat(),
                    media_type=media_type,
                    stream_metadata=StreamMetadata(
                        title="Live Track",
                        elapsed_time=metadata_elapsed_time,
                        elapsed_time_last_updated=metadata_last_updated,
                    ),
                ),
            ),
        )
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        provider = MockProvider("test_provider", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        player._attr_playback_state = PlaybackState.PLAYING
        player.update_state(signal_event=False)
        return player

    def test_zero_metadata_position_is_reported(self, mock_mass: MagicMock) -> None:
        """A live track that just started reports zero, not the elapsed stream time."""
        metadata_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=0,
            metadata_last_updated=metadata_anchor,
            queue_elapsed_time=300.0,
            queue_last_updated=metadata_anchor - 30,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.title == "Live Track"
        assert player.state.current_media.elapsed_time == 0
        assert player.state.current_media.elapsed_time_last_updated == metadata_anchor

    def test_queue_position_is_paired_with_its_own_timestamp(self, mock_mass: MagicMock) -> None:
        """Without a metadata position, both values come from the queue."""
        queue_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=None,
            metadata_last_updated=queue_anchor - 30,
            queue_elapsed_time=300.5,
            queue_last_updated=queue_anchor,
        )

        assert player.state.current_media is not None
        assert player.state.current_media.title == "Live Track"
        assert player.state.current_media.elapsed_time == 300
        assert player.state.current_media.elapsed_time_last_updated == queue_anchor

    def test_audio_source_position_matches_player_position(self, mock_mass: MagicMock) -> None:
        """An upstream source position is reported identically on player and media level."""
        metadata_anchor = time.time()
        player = self._playing_player(
            mock_mass,
            metadata_elapsed_time=0,
            metadata_last_updated=metadata_anchor,
            queue_elapsed_time=300.0,
            queue_last_updated=metadata_anchor - 30,
            media_type=MediaType.AUDIO_SOURCE,
        )

        assert player.state.current_media is not None
        assert player.state.elapsed_time == 0
        assert player.state.current_media.elapsed_time == 0
        assert player.state.elapsed_time_last_updated == metadata_anchor
        assert player.state.current_media.elapsed_time_last_updated == metadata_anchor


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
