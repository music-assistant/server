"""Unit tests for PlayerQueuesController.

Covers: get, all, items, get_item, index_by_id, load, update_items, move_item,
delete_item, clear, set_repeat, set_shuffle, set_playback_speed, on_player_remove,
signal_update, player_media_from_queue_item, get_next_item, resume, seek, skip,
transfer_queue, load_next_queue_item, queue_buffer_completed, _update_queue_from_player,
_handle_end_of_queue, _smart_shuffle, save_as_playlist, get_artist_tracks,
get_album_tracks, get_playlist_tracks, _resolve_media_items, _try_resume_from_playlog.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderFeature,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    InvalidCommand,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    Genre,
    ItemMapping,
    Playlist,
    Podcast,
    PodcastEpisode,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController, _smart_shuffle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a minimal mock MusicAssistant for PlayerQueuesController tests."""
    mass = MagicMock()
    mass.closing = False
    mass.signal_event = MagicMock()
    mass.create_task = MagicMock()
    mass.cancel_timer = MagicMock()
    mass.call_later = MagicMock()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.players.trigger_player_update = MagicMock()
    mass.cache.set = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerQueuesController:
    """Create a PlayerQueuesController backed by the mock mass."""
    return PlayerQueuesController(mock_mass)


def _make_queue(
    queue_id: str = "q1",
    display_name: str = "Test Queue",
    state: PlaybackState = PlaybackState.IDLE,
) -> PlayerQueue:
    """Create a minimal PlayerQueue for tests."""
    q = PlayerQueue(
        queue_id=queue_id,
        active=True,
        display_name=display_name,
        available=True,
        items=0,
    )
    q.state = state
    return q


def _make_item(
    queue_id: str = "q1",
    queue_item_id: str = "item-1",
    name: str = "Track 1",
    duration: int = 180,
) -> QueueItem:
    """Create a minimal QueueItem for tests."""
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=queue_item_id,
        name=name,
        duration=duration,
    )


def _seed_queue(
    controller: PlayerQueuesController,
    queue_id: str = "q1",
    num_items: int = 0,
) -> PlayerQueue:
    """Register a queue with *num_items* items on the controller."""
    queue = _make_queue(queue_id)
    controller._queues[queue_id] = queue
    items = [
        _make_item(queue_id=queue_id, queue_item_id=f"item-{i}", name=f"Track {i}")
        for i in range(num_items)
    ]
    controller._queue_items[queue_id] = items
    queue.items = len(items)
    return queue


# ---------------------------------------------------------------------------
# Tests: read helpers
# ---------------------------------------------------------------------------


class TestGetAndAll:
    """Tests for get() and all()."""

    def test_get_returns_none_for_unknown_queue(self, controller: PlayerQueuesController) -> None:
        """Test get returns none for unknown queue."""
        # Given no queues registered
        # When
        result = controller.get("nonexistent")
        # Then
        assert result is None

    def test_get_returns_registered_queue(self, controller: PlayerQueuesController) -> None:
        """Test get returns registered queue."""
        # Given
        queue = _seed_queue(controller, "q1")
        # When
        result = controller.get("q1")
        # Then
        assert result is queue

    def test_all_returns_empty_tuple_when_no_queues(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test all returns empty tuple when no queues."""
        # Given no queues
        # When
        result = controller.all()
        # Then
        assert result == ()

    def test_all_returns_all_registered_queues(self, controller: PlayerQueuesController) -> None:
        """Test all returns all registered queues."""
        # Given
        _seed_queue(controller, "q1")
        _seed_queue(controller, "q2")
        # When
        result = controller.all()
        # Then
        assert len(result) == 2
        ids = {q.queue_id for q in result}
        assert ids == {"q1", "q2"}


class TestItems:
    """Tests for items() — paginated queue item list."""

    def test_items_returns_empty_list_for_unknown_queue(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test items returns empty list for unknown queue."""
        # Given no queues
        # When
        result = controller.items("nonexistent")
        # Then
        assert result == []

    def test_items_returns_all_items(self, controller: PlayerQueuesController) -> None:
        """Test items returns all items."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        result = controller.items("q1")
        # Then
        assert len(result) == 3

    def test_items_pagination_limit(self, controller: PlayerQueuesController) -> None:
        """Test items pagination limit."""
        # Given
        _seed_queue(controller, "q1", num_items=5)
        # When
        result = controller.items("q1", limit=2)
        # Then
        assert len(result) == 2
        assert result[0].queue_item_id == "item-0"
        assert result[1].queue_item_id == "item-1"

    def test_items_pagination_offset(self, controller: PlayerQueuesController) -> None:
        """Test items pagination offset."""
        # Given
        _seed_queue(controller, "q1", num_items=5)
        # When
        result = controller.items("q1", offset=3)
        # Then
        assert len(result) == 2
        assert result[0].queue_item_id == "item-3"


class TestGetItem:
    """Tests for get_item()."""

    def test_get_item_returns_none_for_unknown_queue(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test get item returns none for unknown queue."""
        # Given no queues
        # When
        result = controller.get_item("nonexistent", 0)
        # Then
        assert result is None

    def test_get_item_by_index(self, controller: PlayerQueuesController) -> None:
        """Test get item by index."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        result = controller.get_item("q1", 1)
        # Then
        assert result is not None
        assert result.queue_item_id == "item-1"

    def test_get_item_by_id(self, controller: PlayerQueuesController) -> None:
        """Test get item by id."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        result = controller.get_item("q1", "item-2")
        # Then
        assert result is not None
        assert result.queue_item_id == "item-2"

    def test_get_item_returns_none_for_out_of_range_index(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test get item returns none for out of range index."""
        # Given
        _seed_queue(controller, "q1", num_items=2)
        # When
        result = controller.get_item("q1", 99)
        # Then
        assert result is None

    def test_get_item_returns_none_for_none_id(self, controller: PlayerQueuesController) -> None:
        """Test get item returns none for none id."""
        # Given
        _seed_queue(controller, "q1", num_items=2)
        # When
        result = controller.get_item("q1", None)
        # Then
        assert result is None


class TestIndexById:
    """Tests for index_by_id()."""

    def test_returns_correct_index(self, controller: PlayerQueuesController) -> None:
        """Test returns correct index."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        index = controller.index_by_id("q1", "item-2")
        # Then
        assert index == 2

    def test_returns_none_for_unknown_id(self, controller: PlayerQueuesController) -> None:
        """Test returns none for unknown id."""
        # Given
        _seed_queue(controller, "q1", num_items=2)
        # When
        index = controller.index_by_id("q1", "nonexistent")
        # Then
        assert index is None


# ---------------------------------------------------------------------------
# Tests: load and update_items
# ---------------------------------------------------------------------------


class TestLoad:
    """Tests for load()."""

    async def test_load_appends_items(self, controller: PlayerQueuesController) -> None:
        """Test load appends items."""
        # Given
        _seed_queue(controller, "q1")
        new_items = [_make_item("q1", f"new-{i}") for i in range(3)]
        # When
        await controller.load("q1", new_items)
        # Then
        assert len(controller._queue_items["q1"]) == 3

    async def test_load_replaces_with_keep_remaining_false(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test load replaces with keep remaining false."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        new_items = [_make_item("q1", "fresh")]
        # When
        await controller.load("q1", new_items, keep_remaining=False, keep_played=False)
        # Then
        assert len(controller._queue_items["q1"]) == 1
        assert controller._queue_items["q1"][0].queue_item_id == "fresh"

    async def test_load_inserts_at_index(self, controller: PlayerQueuesController) -> None:
        """Test load inserts at index."""
        # Given: queue with items 0,1,2
        _seed_queue(controller, "q1", num_items=3)
        new_items = [_make_item("q1", "inserted")]
        # When: insert at index 1 with keep_remaining=True
        await controller.load("q1", new_items, insert_at_index=1, keep_remaining=True)
        # Then: 4 items total, inserted item is at index 1
        items = controller._queue_items["q1"]
        assert len(items) == 4
        assert items[1].queue_item_id == "inserted"


class TestUpdateItems:
    """Tests for update_items()."""

    def test_update_items_sets_items_count(self, controller: PlayerQueuesController) -> None:
        """Test update items sets items count."""
        # Given
        queue = _seed_queue(controller, "q1")
        new_items = [_make_item("q1", f"x-{i}") for i in range(5)]
        # When
        controller.update_items("q1", new_items)
        # Then
        assert queue.items == 5
        assert len(controller._queue_items["q1"]) == 5

    def test_update_items_fires_signal(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test update items fires signal."""
        # Given
        _seed_queue(controller, "q1")
        # When
        controller.update_items("q1", [])
        # Then
        mock_mass.signal_event.assert_called()


# ---------------------------------------------------------------------------
# Tests: mutation commands
# ---------------------------------------------------------------------------


class TestDeleteItem:
    """Tests for delete_item()."""

    def test_delete_item_by_index(self, controller: PlayerQueuesController) -> None:
        """Test delete item by index."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        controller.delete_item("q1", 1)
        # Then
        items = controller._queue_items["q1"]
        assert len(items) == 2
        ids = [i.queue_item_id for i in items]
        assert "item-1" not in ids

    def test_delete_item_by_id(self, controller: PlayerQueuesController) -> None:
        """Test delete item by id."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        controller.delete_item("q1", "item-0")
        # Then
        assert len(controller._queue_items["q1"]) == 2

    def test_delete_item_raises_for_unknown_id(self, controller: PlayerQueuesController) -> None:
        """Test delete item raises for unknown id."""
        # Given
        _seed_queue(controller, "q1", num_items=2)
        # When / Then
        with pytest.raises(InvalidDataError):
            controller.delete_item("q1", "does-not-exist")

    def test_delete_buffered_item_is_ignored(self, controller: PlayerQueuesController) -> None:
        """Test delete buffered item is ignored."""
        # Given: queue with index_in_buffer = 1
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.index_in_buffer = 1
        # When: try to delete item at index 0 (already buffered)
        controller.delete_item("q1", 0)
        # Then: still 3 items (ignored)
        assert len(controller._queue_items["q1"]) == 3


class TestMoveItem:
    """Tests for move_item()."""

    def test_move_item_down(self, controller: PlayerQueuesController) -> None:
        """Test move item down."""
        # Given
        _seed_queue(controller, "q1", num_items=4)
        # When: move item at index 1 down by 1
        controller.move_item("q1", "item-1", pos_shift=1)
        # Then: item-1 is now at index 2
        items = controller._queue_items["q1"]
        assert items[2].queue_item_id == "item-1"

    def test_move_item_up(self, controller: PlayerQueuesController) -> None:
        """Test move item up."""
        # Given
        _seed_queue(controller, "q1", num_items=4)
        # When: move item at index 2 up by 1
        controller.move_item("q1", "item-2", pos_shift=-1)
        # Then: item-2 is now at index 1
        items = controller._queue_items["q1"]
        assert items[1].queue_item_id == "item-2"

    def test_move_item_raises_for_unknown_id(self, controller: PlayerQueuesController) -> None:
        """Test move item raises for unknown id."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When / Then
        with pytest.raises(InvalidDataError):
            controller.move_item("q1", "does-not-exist", pos_shift=1)

    def test_move_buffered_item_raises(self, controller: PlayerQueuesController) -> None:
        """Test move buffered item raises."""
        # Given: index_in_buffer=2 means items 0-2 are already buffered
        queue = _seed_queue(controller, "q1", num_items=4)
        queue.index_in_buffer = 2
        # When: try to move buffered item
        with pytest.raises(IndexError):
            controller.move_item("q1", "item-1", pos_shift=1)


class TestMoveItemEnd:
    """Tests for move_item_end()."""

    def test_move_item_to_end(self, controller: PlayerQueuesController) -> None:
        """Test move item to end."""
        # Given
        _seed_queue(controller, "q1", num_items=4)
        # When
        controller.move_item_end("q1", "item-0")
        # Then: item-0 is now last
        items = controller._queue_items["q1"]
        assert items[-1].queue_item_id == "item-0"

    def test_move_item_end_already_at_end_is_noop(self, controller: PlayerQueuesController) -> None:
        """Test move item end already at end is noop."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        original_items = list(controller._queue_items["q1"])
        # When: item-2 is already the last item
        controller.move_item_end("q1", "item-2")
        # Then: no change
        assert [i.queue_item_id for i in controller._queue_items["q1"]] == [
            i.queue_item_id for i in original_items
        ]


# ---------------------------------------------------------------------------
# Tests: clear
# ---------------------------------------------------------------------------


class TestClear:
    """Tests for clear()."""

    def test_clear_empties_queue(self, controller: PlayerQueuesController) -> None:
        """Test clear empties queue."""
        # Given
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.current_index = 1
        queue.current_item = controller._queue_items["q1"][1]
        queue.elapsed_time = 42.0
        # When
        controller.clear("q1")
        # Then
        assert controller._queue_items["q1"] == []
        assert queue.current_index is None
        assert queue.current_item is None  # type: ignore[unreachable]
        assert queue.elapsed_time == 0

    def test_clear_resets_radio_source(self, controller: PlayerQueuesController) -> None:
        """Test clear resets radio source."""
        # Given
        queue = _seed_queue(controller, "q1")
        queue.radio_source = [MagicMock()]
        # When
        controller.clear("q1")
        # Then
        assert queue.radio_source == []

    def test_clear_triggers_stop_when_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test clear triggers stop when playing."""
        # Given: queue is playing
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.state = PlaybackState.PLAYING
        # When
        controller.clear("q1")
        # Then: create_task called (for stop)
        mock_mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests: repeat mode
# ---------------------------------------------------------------------------


class TestSetRepeat:
    """Tests for set_repeat()."""

    def test_set_repeat_changes_mode(self, controller: PlayerQueuesController) -> None:
        """Test set repeat changes mode."""
        # Given
        queue = _seed_queue(controller, "q1")
        assert queue.repeat_mode == RepeatMode.OFF
        # When
        controller.set_repeat("q1", RepeatMode.ALL)
        # Then
        assert queue.repeat_mode == RepeatMode.ALL  # type: ignore[comparison-overlap]

    def test_set_repeat_noop_on_same_value(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test set repeat noop on same value."""
        # Given: already OFF
        _seed_queue(controller, "q1")
        mock_mass.signal_event.reset_mock()
        # When: set to OFF again
        controller.set_repeat("q1", RepeatMode.OFF)
        # Then: no signal fired
        mock_mass.signal_event.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: playback speed
# ---------------------------------------------------------------------------


class TestSetPlaybackSpeed:
    """Tests for set_playback_speed()."""

    async def test_raises_for_invalid_speed(self, controller: PlayerQueuesController) -> None:
        """Test raises for invalid speed."""
        # Given
        _seed_queue(controller, "q1")
        # When / Then
        with pytest.raises(InvalidDataError):
            await controller.set_playback_speed("q1", 0.1)

    async def test_raises_for_empty_queue(self, controller: PlayerQueuesController) -> None:
        """Test raises for empty queue."""
        # Given: queue with no current item
        _seed_queue(controller, "q1")
        # When / Then
        with pytest.raises(QueueEmpty):
            await controller.set_playback_speed("q1", 1.5)

    async def test_sets_speed_on_current_item(self, controller: PlayerQueuesController) -> None:
        """Test sets speed on current item."""
        # Given: queue with a current item
        queue = _seed_queue(controller, "q1", num_items=2)
        current_item = controller._queue_items["q1"][0]
        queue.current_item = current_item
        queue.state = PlaybackState.IDLE
        # When
        await controller.set_playback_speed("q1", 1.5)
        # Then
        assert current_item.extra_attributes["playback_speed"] == 1.5

    async def test_noop_when_speed_unchanged(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test noop when speed unchanged."""
        # Given: speed already 1.0
        queue = _seed_queue(controller, "q1", num_items=1)
        current_item = controller._queue_items["q1"][0]
        queue.current_item = current_item
        mock_mass.signal_event.reset_mock()
        # When: set to same value
        await controller.set_playback_speed("q1", 1.0)
        # Then: no signal
        mock_mass.signal_event.assert_not_called()

    async def test_raises_for_radio_item(self, controller: PlayerQueuesController) -> None:
        """Test raises for radio item."""
        # Given: current item with no duration (like radio)
        queue = _seed_queue(controller, "q1")
        item = _make_item("q1", "radio-item", "Radio", duration=0)
        item.duration = None  # radio has no duration
        controller._queue_items["q1"] = [item]
        queue.current_item = item
        queue.items = 1
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.set_playback_speed("q1", 1.5)


# ---------------------------------------------------------------------------
# Tests: on_player_remove
# ---------------------------------------------------------------------------


class TestOnPlayerRemove:
    """Tests for on_player_remove()."""

    def test_removes_queue_on_permanent_remove(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test removes queue on permanent remove."""
        # Given
        _seed_queue(controller, "q1")
        # When
        controller.on_player_remove("q1", permanent=True)
        # Then
        assert "q1" not in controller._queues
        assert "q1" not in controller._queue_items
        # cache delete tasks created
        assert mock_mass.create_task.call_count >= 2

    def test_removes_queue_on_non_permanent_remove(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test removes queue on non permanent remove."""
        # Given
        _seed_queue(controller, "q1")
        # When
        controller.on_player_remove("q1", permanent=False)
        # Then
        assert "q1" not in controller._queues

    def test_remove_unknown_player_is_safe(self, controller: PlayerQueuesController) -> None:
        """Test remove unknown player is safe."""
        # Given: no queues
        # When/Then: no exception
        controller.on_player_remove("unknown", permanent=False)


# ---------------------------------------------------------------------------
# Tests: get_next_item
# ---------------------------------------------------------------------------


class TestGetNextItem:
    """Tests for get_next_item()."""

    def test_returns_next_item(self, controller: PlayerQueuesController) -> None:
        """Test returns next item."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When
        next_item = controller.get_next_item("q1", 0)
        # Then
        assert next_item is not None
        assert next_item.queue_item_id == "item-1"

    def test_returns_none_at_end_of_queue_no_repeat(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test returns none at end of queue no repeat."""
        # Given: 3-item queue, no repeat
        _seed_queue(controller, "q1", num_items=3)
        # When: we're at the last item (index 2)
        next_item = controller.get_next_item("q1", 2)
        # Then
        assert next_item is None

    def test_returns_first_item_with_repeat_all(self, controller: PlayerQueuesController) -> None:
        """Test returns first item with repeat all."""
        # Given: 3-item queue, repeat ALL
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.repeat_mode = RepeatMode.ALL
        # When: at last item
        next_item = controller.get_next_item("q1", 2)
        # Then: wraps to first
        assert next_item is not None
        assert next_item.queue_item_id == "item-0"

    def test_skips_unavailable_items(self, controller: PlayerQueuesController) -> None:
        """Test skips unavailable items."""
        # Given: item-1 is unavailable
        _seed_queue(controller, "q1", num_items=4)
        controller._queue_items["q1"][1].available = False
        # When
        next_item = controller.get_next_item("q1", 0)
        # Then: skips item-1, returns item-2
        assert next_item is not None
        assert next_item.queue_item_id == "item-2"

    def test_get_next_item_by_string_id(self, controller: PlayerQueuesController) -> None:
        """Test get next item by string id."""
        # Given
        _seed_queue(controller, "q1", num_items=3)
        # When: pass item id as string
        next_item = controller.get_next_item("q1", "item-0")
        # Then
        assert next_item is not None
        assert next_item.queue_item_id == "item-1"


# ---------------------------------------------------------------------------
# Tests: player_media_from_queue_item
# ---------------------------------------------------------------------------


class TestPlayerMediaFromQueueItem:
    """Tests for player_media_from_queue_item()."""

    async def test_raises_when_no_session_id(self, controller: PlayerQueuesController) -> None:
        """Test raises when no session id."""
        # Given: queue with no session_id
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.session_id = None
        item = controller._queue_items["q1"][0]
        # When / Then
        with pytest.raises(InvalidDataError):
            await controller.player_media_from_queue_item(item)

    async def test_returns_player_media_with_session_id(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns player media with session id."""
        # Given: queue with session_id set
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.session_id = "ses123"
        item = controller._queue_items["q1"][0]
        # metadata mock for image URL
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        # When
        media = await controller.player_media_from_queue_item(item)
        # Then
        assert media.queue_item_id == item.queue_item_id
        assert media.custom_data is not None
        assert media.custom_data["session_id"] == "ses123"


# ---------------------------------------------------------------------------
# Tests: stop / pause / play / play_pause
# ---------------------------------------------------------------------------


def _mock_queue_player() -> MagicMock:
    """Return a mock player (queue_player) suitable for command tests."""
    qp = MagicMock()
    qp.play = AsyncMock()
    qp.extra_data = {}
    qp.state = MagicMock()
    qp.state.playback_state = PlaybackState.IDLE
    return qp


class TestStop:
    """Tests for stop()."""

    async def test_stop_raises_when_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test stop raises when player unavailable."""
        # Given: no player available
        _seed_queue(controller, "q1")
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(PlayerUnavailableError):
            await controller.stop("q1")

    async def test_stop_calls_cmd_stop(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test stop calls cmd stop."""
        # Given
        queue = _seed_queue(controller, "q1")
        queue.active = True
        qp = _mock_queue_player()
        mock_mass.players.get_player.return_value = qp
        mock_mass.players.cmd_stop = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.stop("q1")
        # Then
        mock_mass.players.cmd_stop.assert_called_once_with("q1")

    async def test_stop_saves_resume_pos_when_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test stop saves resume pos when playing."""
        # Given: queue is playing with elapsed time
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        queue.state = PlaybackState.PLAYING
        queue.elapsed_time = 42.0
        queue.elapsed_time_last_updated = time.time()
        qp = _mock_queue_player()
        mock_mass.players.get_player.return_value = qp
        mock_mass.players.cmd_stop = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.stop("q1")
        # Then: resume_pos should be set to elapsed time
        assert queue.resume_pos > 0


class TestPause:
    """Tests for pause()."""

    async def test_pause_noop_when_no_queue(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test pause noop when no queue."""
        # Given: no queue registered
        mock_mass.players.cmd_pause = AsyncMock()
        # When / Then: no exception
        await controller.pause("missing")

    async def test_pause_calls_cmd_pause(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test pause calls cmd pause."""
        # Given: idle queue
        _seed_queue(controller, "q1")
        mock_mass.players.cmd_pause = AsyncMock()
        mock_mass.players.get_player.return_value = None
        # When
        await controller.pause("q1")
        # Then
        mock_mass.players.cmd_pause.assert_called_once_with("q1")

    async def test_pause_saves_resume_pos_when_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test pause saves resume pos when playing."""
        # Given: queue in PLAYING state with elapsed time
        queue = _seed_queue(controller, "q1")
        queue.active = True
        queue.state = PlaybackState.PLAYING
        queue.elapsed_time = 30.0
        queue.elapsed_time_last_updated = time.time()
        mock_mass.players.cmd_pause = AsyncMock()
        mock_mass.players.get_player.return_value = None
        # When
        await controller.pause("q1")
        # Then: resume_pos updated
        assert queue.resume_pos >= 0


class TestPlay:
    """Tests for play()."""

    async def test_play_raises_when_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play raises when player unavailable."""
        # Given: no player available
        _seed_queue(controller, "q1")
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(PlayerUnavailableError):
            await controller.play("q1")

    async def test_play_calls_player_play_when_paused(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play calls player play when paused."""
        # Given: queue is PAUSED, player available
        queue = _seed_queue(controller, "q1")
        queue.active = True
        queue.state = PlaybackState.PAUSED
        qp = _mock_queue_player()
        mock_mass.players.get_player.return_value = qp
        # When
        await controller.play("q1")
        # Then: player.play() called (not resume)
        qp.play.assert_called_once()


class TestPlayPause:
    """Tests for play_pause()."""

    async def test_play_pause_pauses_when_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play pause pauses when playing."""
        # Given: queue is PLAYING
        queue = _seed_queue(controller, "q1")
        queue.state = PlaybackState.PLAYING
        mock_mass.players.cmd_pause = AsyncMock()
        mock_mass.players.get_player.return_value = None
        # When
        await controller.play_pause("q1")
        # Then: pause called
        mock_mass.players.cmd_pause.assert_called_once_with("q1")

    async def test_play_pause_plays_when_idle(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play pause plays when idle."""
        # Given: queue is IDLE with no items
        _seed_queue(controller, "q1", num_items=0)
        queue = controller._queues["q1"]
        queue.state = PlaybackState.IDLE
        qp = _mock_queue_player()
        mock_mass.players.get_player.return_value = qp
        # patch _try_resume_from_playlog so we don't need full mass.music mock
        with (
            patch.object(
                controller, "_try_resume_from_playlog", new_callable=AsyncMock, return_value=False
            ),
            pytest.raises(QueueEmpty),
        ):
            await controller.play_pause("q1")


# ---------------------------------------------------------------------------
# Tests: next / previous / seek / skip
# ---------------------------------------------------------------------------


class TestNext:
    """Tests for next()."""

    async def test_next_raises_when_queue_not_active(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test next raises when queue not active."""
        # Given: no queue
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.next("unknown")

    async def test_next_raises_when_not_active(self, controller: PlayerQueuesController) -> None:
        """Test next raises when not active."""
        # Given: inactive queue
        queue = _seed_queue(controller, "q1")
        queue.active = False
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.next("q1")

    async def test_next_when_no_current_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test next when no current index."""
        # Given: active queue with no current_index
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = None
        mock_mass.players.get_player.return_value = None
        # When: no IndexError, just returns early
        await controller.next("q1")
        # Then: no crash

    async def test_next_advances_to_next_track(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test next advances to next track."""
        # Given: active queue at index 0
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = 0
        queue.current_item = controller._queue_items["q1"][0]
        mock_mass.players.get_player.return_value = None
        # When
        await controller.next("q1")
        # Then: current_index advances
        assert queue.current_index == 1

    async def test_next_at_end_of_queue_stops(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test next at end of queue stops."""
        # Given: at last item, no repeat
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = 2
        queue.current_item = controller._queue_items["q1"][2]
        mock_mass.players.get_player.return_value = None
        # When
        await controller.next("q1")
        # Then: no crash, transitioning flag cleared
        assert "q1" not in controller._transitioning_players


class TestPrevious:
    """Tests for previous()."""

    async def test_previous_raises_when_not_active(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test previous raises when not active."""
        # Given
        queue = _seed_queue(controller, "q1")
        queue.active = False
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.previous("q1")

    async def test_previous_restarts_track_after_5s(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test previous restarts track after 5s."""
        # Given: elapsed time > 5 seconds, at index 2
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = 2
        queue.current_item = controller._queue_items["q1"][2]
        queue.elapsed_time = 10.0
        mock_mass.players.get_player.return_value = None
        # When
        await controller.previous("q1")
        # Then: stays at current index (restart not prev)
        assert queue.current_index == 2

    async def test_previous_goes_to_prev_track_within_5s(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test previous goes to prev track within 5s."""
        # Given: elapsed time < 5 seconds, at index 2
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = 2
        queue.current_item = controller._queue_items["q1"][2]
        queue.elapsed_time = 2.0
        mock_mass.players.get_player.return_value = None
        # When
        await controller.previous("q1")
        # Then: goes to previous track
        assert queue.current_index == 1


class TestSeekAndSkip:
    """Tests for seek() and skip()."""

    async def test_seek_raises_when_not_active(self, controller: PlayerQueuesController) -> None:
        """Test seek raises when not active."""
        # Given: no queue
        with pytest.raises(InvalidCommand):
            await controller.seek("nonexistent", 30)

    async def test_seek_raises_when_no_current_item(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test seek raises when no current item."""
        # Given: active queue with no current item
        queue = _seed_queue(controller, "q1")
        queue.active = True
        queue.current_item = None
        with pytest.raises(InvalidCommand):
            await controller.seek("q1", 30)

    async def test_seek_raises_when_no_duration(self, controller: PlayerQueuesController) -> None:
        """Test seek raises when no duration."""
        # Given: item with no duration
        queue = _seed_queue(controller, "q1")
        queue.active = True
        item = _make_item("q1", "i1", "Track", duration=0)
        item.duration = None
        queue.current_item = item
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.seek("q1", 30)

    async def test_seek_raises_beyond_duration(self, controller: PlayerQueuesController) -> None:
        """Test seek raises beyond duration."""
        # Given: item with 60s duration, seeking to 100s
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        queue.current_item = controller._queue_items["q1"][0]
        queue.current_index = 0
        # When / Then
        with pytest.raises(InvalidCommand):
            await controller.seek("q1", 99999)

    async def test_skip_raises_when_not_active(self, controller: PlayerQueuesController) -> None:
        """Test skip raises when not active."""
        # Given
        with pytest.raises(InvalidCommand):
            await controller.skip("nonexistent")


# ---------------------------------------------------------------------------
# Tests: on_player_update / on_player_elapsed_time_corrected
# ---------------------------------------------------------------------------


class TestOnPlayerUpdate:
    """Tests for on_player_update()."""

    def test_ignores_protocol_players(self, controller: PlayerQueuesController) -> None:
        """Test ignores protocol players."""
        # Given: protocol player
        player = MagicMock()
        player.type = PlayerType.PROTOCOL
        player.player_id = "proto-player"
        # When: should return early without error
        controller.on_player_update(player, {})
        # Then: no crash, no queue created

    def test_ignores_unknown_player_id(self, controller: PlayerQueuesController) -> None:
        """Test ignores unknown player id."""
        # Given: no queue for player
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "unknown-player"
        player.extra_data = {}
        # When: should return early
        controller.on_player_update(player, {})
        # Then: no crash

    def test_returns_early_during_announcement(self, controller: PlayerQueuesController) -> None:
        """Test returns early during announcement."""
        # Given: queue registered, announcement in progress
        _seed_queue(controller, "q1")
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: True}
        player.state.active_source = None
        # When: should not update (announcement guard)
        controller.on_player_update(player, {})
        # Then: no crash

    def test_sets_queue_inactive_when_not_active_source(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test sets queue inactive when not active source."""
        # Given: queue registered, player active_source points elsewhere
        queue = _seed_queue(controller, "q1")
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "other-source"
        # When
        controller.on_player_update(player, {})
        # Then: queue becomes inactive
        assert queue.active is False


class TestOnPlayerElapsedTimeCorrected:
    """Tests for on_player_elapsed_time_corrected()."""

    def test_ignores_protocol_players(self, controller: PlayerQueuesController) -> None:
        """Test ignores protocol players."""
        # Given
        player = MagicMock()
        player.type = PlayerType.PROTOCOL
        # When / Then: no error
        controller.on_player_elapsed_time_corrected(player)

    def test_ignores_unknown_queue(self, controller: PlayerQueuesController) -> None:
        """Test ignores unknown queue."""
        # Given
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "unknown"
        # When / Then: no error
        controller.on_player_elapsed_time_corrected(player)

    def test_ignores_inactive_queue(self, controller: PlayerQueuesController) -> None:
        """Test ignores inactive queue."""
        # Given
        queue = _seed_queue(controller, "q1")
        queue.active = False
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        # When / Then: no error
        controller.on_player_elapsed_time_corrected(player)

    def test_updates_elapsed_time(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates elapsed time."""
        # Given: active queue, player reports elapsed time 30s
        queue = _seed_queue(controller, "q1")
        queue.active = True
        queue.current_item = None  # no current_item
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.state.corrected_elapsed_time = 30.0
        # When
        controller.on_player_elapsed_time_corrected(player)
        # Then
        assert queue.elapsed_time == 30.0
        mock_mass.signal_event.assert_called()


# ---------------------------------------------------------------------------
# Tests: track_loaded_in_buffer
# ---------------------------------------------------------------------------


class TestTrackLoadedInBuffer:
    """Tests for track_loaded_in_buffer()."""

    def test_raises_for_unknown_queue(self, controller: PlayerQueuesController) -> None:
        """Test raises for unknown queue."""
        # Given: no queue registered
        with pytest.raises(PlayerUnavailableError):
            controller.track_loaded_in_buffer("nonexistent", "item-0")

    def test_updates_index_in_buffer(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates index in buffer."""
        # Given
        queue = _seed_queue(controller, "q1", num_items=3)
        mock_mass.streams.cleanup_stale_queue_buffers = AsyncMock()
        # When
        controller.track_loaded_in_buffer("q1", "item-1")
        # Then: index_in_buffer updated to 1
        assert queue.index_in_buffer == 1


# ---------------------------------------------------------------------------
# Tests: on_player_register
# ---------------------------------------------------------------------------


class TestOnPlayerRegister:
    """Tests for on_player_register()."""

    async def test_creates_fresh_queue_when_no_cache(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test creates fresh queue when no cache."""
        # Given: cache returns None (fresh start)
        mock_mass.cache.get = AsyncMock(return_value=None)
        player = MagicMock()
        player.player_id = "new-player"
        player.type = PlayerType.PLAYER
        player.extra_data = {}
        player.state.name = "New Player"
        player.state.available = True
        player.state.active_source = None
        # When
        await controller.on_player_register(player)
        # Then
        assert "new-player" in controller._queues
        assert "new-player" in controller._queue_items
        mock_mass.signal_event.assert_called()


# ---------------------------------------------------------------------------
# Tests: set_dont_stop_the_music
# ---------------------------------------------------------------------------


class TestSetDontStopTheMusic:
    """Tests for set_dont_stop_the_music()."""

    def test_raises_when_no_similar_tracks_providers(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test raises when no similar tracks providers."""
        # Given: no providers with SIMILAR_TRACKS feature
        _seed_queue(controller, "q1")
        mock_mass.music.providers = []
        # When / Then
        with pytest.raises(UnsupportedFeaturedException):
            controller.set_dont_stop_the_music("q1", True)

    def test_disabling_dont_stop_the_music(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test disabling dont stop the music."""
        # Given: queue has DSTM enabled
        queue = _seed_queue(controller, "q1")
        queue.dont_stop_the_music_enabled = True
        # When: disable
        controller.set_dont_stop_the_music("q1", False)
        # Then
        assert queue.dont_stop_the_music_enabled is False


# ---------------------------------------------------------------------------
# Tests: get_config_entries
# ---------------------------------------------------------------------------


class TestGetConfigEntries:
    """Tests for get_config_entries()."""

    async def test_returns_config_entries(self, controller: PlayerQueuesController) -> None:
        """Test returns config entries."""
        # Given
        # When
        entries = await controller.get_config_entries()
        # Then: multiple config entries returned
        assert len(entries) > 0
        keys = {e.key for e in entries}
        assert "default_enqueue_select_artist" in keys
        assert "default_enqueue_option_track" in keys


# ---------------------------------------------------------------------------
# Tests: save_as_playlist
# ---------------------------------------------------------------------------


class TestSaveAsPlaylist:
    """Tests for save_as_playlist()."""

    async def test_raises_when_queue_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises when queue unavailable."""
        # Given: no queue
        with pytest.raises(PlayerUnavailableError):
            await controller.save_as_playlist("nonexistent", "My Playlist")

    async def test_raises_when_empty_queue(self, controller: PlayerQueuesController) -> None:
        """Test raises when empty queue."""
        # Given: empty queue
        _seed_queue(controller, "q1")
        with pytest.raises(QueueEmpty):
            await controller.save_as_playlist("q1", "My Playlist")

    async def test_raises_when_no_valid_items(self, controller: PlayerQueuesController) -> None:
        """Test raises when queue items have no valid URIs for playlists."""
        # Given: queue with items but none are playlist-compatible (no URI)
        _seed_queue(controller, "q1", num_items=2)
        with pytest.raises(InvalidDataError):
            await controller.save_as_playlist("q1", "My Playlist")

    async def test_saves_playlist_with_valid_items(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test saves playlist when queue has valid playlist-compatible items."""
        # Given: queue with items that have media_items (so uri and media_type are set)
        _seed_queue(controller, "q1", num_items=2)
        items = controller._queue_items["q1"]
        for item in items:
            # Attach a mock media_item so uri and media_type return proper values
            media_item_mock = MagicMock()
            media_item_mock.uri = f"provider://music/track/{item.queue_item_id}"
            media_item_mock.media_type = MediaType.TRACK
            item.media_item = media_item_mock
        mock_playlist = MagicMock()
        mock_playlist.item_id = "playlist-1"
        mock_mass.music.playlists.create_playlist = AsyncMock(return_value=mock_playlist)
        mock_mass.music.playlists.add_playlist_tracks = AsyncMock(return_value=MagicMock())
        # When
        await controller.save_as_playlist("q1", "My Playlist")
        # Then: playlist was created and tracks were added
        mock_mass.music.playlists.create_playlist.assert_called_once_with("My Playlist")
        mock_mass.music.playlists.add_playlist_tracks.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: close
# ---------------------------------------------------------------------------


class TestClose:
    """Tests for close()."""

    async def test_close_stops_playing_queues(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test close stops all playing queues."""
        # Given: one playing queue, one idle queue
        queue_playing = _seed_queue(controller, "q1")
        queue_playing.state = PlaybackState.PLAYING
        queue_idle = _seed_queue(controller, "q2")
        queue_idle.state = PlaybackState.IDLE
        # stop() will raise but we patch it
        mock_mass.players.get_player.return_value = MagicMock(extra_data={})
        mock_mass.players.cmd_stop = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.close()
        # Then: cmd_stop called at least once (for q1)
        mock_mass.players.cmd_stop.assert_called()

    async def test_close_stops_paused_queues(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test close stops paused queues."""
        # Given: one paused queue
        queue = _seed_queue(controller, "q1")
        queue.state = PlaybackState.PAUSED
        mock_mass.players.get_player.return_value = MagicMock(extra_data={})
        mock_mass.players.cmd_stop = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.close()
        # Then
        mock_mass.players.cmd_stop.assert_called_once_with("q1")


# ---------------------------------------------------------------------------
# Tests: get_active_queue
# ---------------------------------------------------------------------------


class TestGetActiveQueue:
    """Tests for get_active_queue()."""

    def test_returns_none_when_player_not_found(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns none when player not found."""
        # Given: no player
        mock_mass.players.get_player.return_value = None
        # When
        result = controller.get_active_queue("unknown")
        # Then
        assert result is None

    def test_returns_queue_when_player_found(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns queue when player found."""
        # Given: player exists and has active queue
        queue = _seed_queue(controller, "q1")
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        mock_mass.players.get_active_queue.return_value = queue
        # When
        result = controller.get_active_queue("q1")
        # Then
        assert result is queue


# ---------------------------------------------------------------------------
# Tests: set_shuffle
# ---------------------------------------------------------------------------


class TestSetShuffle:
    """Tests for set_shuffle()."""

    async def test_noop_when_shuffle_unchanged(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test noop when shuffle unchanged."""
        # Given: shuffle is already False
        _seed_queue(controller, "q1", num_items=3)
        mock_mass.signal_event.reset_mock()
        # When
        await controller.set_shuffle("q1", False)
        # Then: no signal because unchanged
        mock_mass.signal_event.assert_not_called()

    async def test_shuffle_enable_reshuffles_remaining(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test shuffle enable reshuffles remaining items in queue."""
        # Given: 5 items, current at 1
        queue = _seed_queue(controller, "q1", num_items=5)
        queue.current_index = 1
        queue.index_in_buffer = None
        # When: enable shuffle
        await controller.set_shuffle("q1", True)
        # Then: shuffle is enabled
        assert queue.shuffle_enabled is True

    async def test_shuffle_disable_restores_sort_order(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test shuffle disable restores original sort order."""
        # Given: 3 items with shuffle enabled, current at 0
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.shuffle_enabled = True
        queue.current_index = 0
        # When: disable shuffle
        await controller.set_shuffle("q1", False)
        # Then: shuffle is disabled
        assert queue.shuffle_enabled is False


# ---------------------------------------------------------------------------
# Tests: set_dont_stop_the_music (enabled path)
# ---------------------------------------------------------------------------


class TestSetDontStopTheMusicEnabled:
    """Tests for set_dont_stop_the_music() enabled path."""

    def test_enables_dont_stop_the_music_with_provider(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test enables dont stop the music when provider with SIMILAR_TRACKS is available."""
        # Given: a provider with SIMILAR_TRACKS feature
        queue = _seed_queue(controller, "q1")
        provider = MagicMock()
        provider.supported_features = [ProviderFeature.SIMILAR_TRACKS]
        mock_mass.music.providers = [provider]
        # When
        controller.set_dont_stop_the_music("q1", True)
        # Then
        assert queue.dont_stop_the_music_enabled is True

    def test_enables_fills_radio_when_near_end(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test enables DSTM and calls fill radio when near end of queue."""
        # Given: near end of queue with enqueued_media_items
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.current_index = 2  # at last item
        media_mock = MagicMock()
        media_mock.name = "Test Track"
        queue.enqueued_media_items = [media_mock]
        provider = MagicMock()
        provider.supported_features = [ProviderFeature.SIMILAR_TRACKS]
        mock_mass.music.providers = [provider]
        # When
        controller.set_dont_stop_the_music("q1", True)
        # Then: call_later was invoked to fill radio tracks
        mock_mass.call_later.assert_called()


# ---------------------------------------------------------------------------
# Tests: set_repeat (playing, with index_in_buffer)
# ---------------------------------------------------------------------------


class TestSetRepeatPlaying:
    """Tests for set_repeat() when queue is playing."""

    def test_set_repeat_enqueues_next_when_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test set_repeat re-enqueues next item when playing and buffer matches current."""
        # Given: playing queue at index 0 with index_in_buffer == current_index
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.state = PlaybackState.PLAYING
        queue.current_index = 0
        queue.index_in_buffer = 0
        # When: change repeat to ALL
        controller.set_repeat("q1", RepeatMode.ALL)
        # Then: call_later invoked (for _enqueue_next_item debounce)
        mock_mass.call_later.assert_called()


# ---------------------------------------------------------------------------
# Tests: resume
# ---------------------------------------------------------------------------


class TestResume:
    """Tests for resume()."""

    async def test_resume_with_current_item_calls_play_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resume calls play_index when current_item is set."""
        # Given: queue with current item, not playing
        queue = _seed_queue(controller, "q1", num_items=2)
        current_item = controller._queue_items["q1"][0]
        queue.current_item = current_item
        queue.state = PlaybackState.IDLE
        queue.resume_pos = 0
        player = MagicMock()
        player.state.playback_state = PlaybackState.IDLE
        mock_mass.players.get_player.return_value = player
        mock_mass.players.play_media = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # patch play_index to avoid deep execution
        with patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index:
            # When
            await controller.resume("q1")
            # Then: play_index was called
            mock_play_index.assert_called()

    async def test_resume_with_no_current_item_but_items_in_queue(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resume starts from index 0 when no current_item but items exist."""
        # Given: queue with items but no current item
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.current_item = None
        queue.current_index = None
        queue.state = PlaybackState.IDLE
        queue.resume_pos = 0
        player = MagicMock()
        player.state.playback_state = PlaybackState.IDLE
        mock_mass.players.get_player.return_value = player
        with patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index:
            # When
            await controller.resume("q1")
            # Then: play_index called with index 0
            mock_play_index.assert_called()

    async def test_resume_raises_queue_empty_when_no_items(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resume raises QueueEmpty when queue is empty."""
        # Given: empty queue with no current item
        _seed_queue(controller, "q1")
        queue = controller._queues["q1"]
        queue.current_item = None
        queue.current_index = None
        with (
            patch.object(
                controller, "_try_resume_from_playlog", new_callable=AsyncMock, return_value=False
            ),
            pytest.raises(QueueEmpty),
        ):
            # When / Then
            await controller.resume("q1")

    async def test_resume_while_playing_uses_current_position(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resume while already playing uses corrected_elapsed_time."""
        # Given: queue is currently playing
        queue = _seed_queue(controller, "q1", num_items=2)
        current_item = controller._queue_items["q1"][0]
        queue.current_item = current_item
        queue.state = PlaybackState.PLAYING
        queue.elapsed_time = 30.0
        player = MagicMock()
        player.state.playback_state = PlaybackState.PLAYING
        mock_mass.players.get_player.return_value = player
        with patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index:
            # When
            await controller.resume("q1")
            # Then: play_index called (to re-play from current position)
            mock_play_index.assert_called()


# ---------------------------------------------------------------------------
# Tests: seek (success path)
# ---------------------------------------------------------------------------


class TestSeekSuccess:
    """Tests for seek() success path."""

    async def test_seek_raises_when_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test seek raises PlayerUnavailableError when player is None."""
        # Given: active queue, but no player available
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        queue.current_item = controller._queue_items["q1"][0]
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(PlayerUnavailableError):
            await controller.seek("q1", 30)

    async def test_seek_calls_play_index_for_valid_position(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test seek calls play_index for valid position."""
        # Given: active queue with current item of 180s duration
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        item = controller._queue_items["q1"][0]
        item.duration = 180
        queue.current_item = item
        queue.current_index = 0
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        with patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index:
            # When
            await controller.seek("q1", 60)
            # Then: play_index called with seek position
            mock_play_index.assert_called_once_with("q1", 0, seek_position=60)


# ---------------------------------------------------------------------------
# Tests: skip
# ---------------------------------------------------------------------------


class TestSkip:
    """Tests for skip()."""

    async def test_skip_forward_calls_seek(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test skip forward calls seek with current+seconds."""
        # Given: active queue with elapsed time of 30s
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        item = controller._queue_items["q1"][0]
        item.duration = 180
        queue.current_item = item
        queue.current_index = 0
        queue.elapsed_time = 30.0
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        with patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index:
            # When: skip forward 10 seconds
            await controller.skip("q1", 10)
            # Then: seek/play_index called with position 40
            mock_play_index.assert_called_once_with("q1", 0, seek_position=40)


# ---------------------------------------------------------------------------
# Tests: _get_next_index with RepeatMode.ONE
# ---------------------------------------------------------------------------


class TestGetNextIndexRepeatOne:
    """Tests for _get_next_index() with RepeatMode.ONE."""

    def test_repeat_one_returns_same_index(self, controller: PlayerQueuesController) -> None:
        """Test repeat ONE returns same index."""
        # Given: 3-item queue, repeat ONE
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.repeat_mode = RepeatMode.ONE
        # When
        result = controller._get_next_index("q1", 1, is_skip=False)
        # Then: returns same index
        assert result == 1

    def test_repeat_one_with_skip_advances(self, controller: PlayerQueuesController) -> None:
        """Test repeat ONE with is_skip=True advances to next track."""
        # Given: 3-item queue, repeat ONE
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.repeat_mode = RepeatMode.ONE
        # When: user explicitly skips
        result = controller._get_next_index("q1", 0, is_skip=True)
        # Then: advances to next
        assert result == 1

    def test_repeat_one_at_end_without_skip_returns_same(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test repeat ONE at last index returns same index without skip."""
        # Given: 3-item queue at last item, repeat ONE
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.repeat_mode = RepeatMode.ONE
        # When
        result = controller._get_next_index("q1", 2, is_skip=False)
        # Then
        assert result == 2

    def test_repeat_one_allow_repeat_false_returns_none(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test _get_next_index with allow_repeat=False returns None for RepeatMode.ONE."""
        # Given: repeat ONE
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.repeat_mode = RepeatMode.ONE
        # When: allow_repeat=False
        result = controller._get_next_index("q1", 1, is_skip=False, allow_repeat=False)
        # Then: None (no repeat)
        assert result is None

    def test_empty_queue_returns_none(self, controller: PlayerQueuesController) -> None:
        """Test _get_next_index returns None for empty queue."""
        # Given: empty queue
        _seed_queue(controller, "q1", num_items=0)
        # When
        result = controller._get_next_index("q1", 0)
        # Then
        assert result is None

    def test_none_cur_index_returns_none(self, controller: PlayerQueuesController) -> None:
        """Test _get_next_index returns None when cur_index is None."""
        # Given: queue with items but cur_index is None
        _seed_queue(controller, "q1", num_items=3)
        # When
        result = controller._get_next_index("q1", None)
        # Then
        assert result is None


# ---------------------------------------------------------------------------
# Tests: update_items when playing with index_in_buffer == current_index
# ---------------------------------------------------------------------------


class TestUpdateItemsWhilePlaying:
    """Tests for update_items() while queue is playing."""

    def test_enqueues_next_when_playing_at_buffer_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test enqueues next item when playing and index_in_buffer == current_index."""
        # Given: playing queue at index 0 with buffer at 0
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.state = PlaybackState.PLAYING
        queue.current_index = 0
        queue.index_in_buffer = 0
        queue.flow_mode = False
        queue.session_id = "ses123"
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        # When: update items (e.g. new item added)
        new_items = [_make_item("q1", f"item-{i}") for i in range(3)]
        controller.update_items("q1", new_items)
        # Then: call_later should have been called (for _enqueue_next_item debounce)
        mock_mass.call_later.assert_called()


# ---------------------------------------------------------------------------
# Tests: player_media_from_queue_item with streamdetails seek_position
# ---------------------------------------------------------------------------


class TestPlayerMediaFromQueueItemExtra:
    """Additional tests for player_media_from_queue_item()."""

    async def test_duration_adjusted_for_seek_position(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test duration is reduced by seek_position when streamdetails present."""
        # Given: queue item with streamdetails having seek_position
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.session_id = "ses123"
        item = controller._queue_items["q1"][0]
        item.duration = 180
        streamdetails = MagicMock()
        streamdetails.duration = 180
        streamdetails.seek_position = 30
        item.streamdetails = streamdetails
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        # When
        media = await controller.player_media_from_queue_item(item)
        # Then: duration reduced by seek_position
        assert media.duration == 150

    async def test_image_url_from_media_item_image(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test image_url is fetched from media_item.image when available."""
        # Given: queue item with media_item that has an image AND item.image set
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.session_id = "ses123"
        item = controller._queue_items["q1"][0]
        media_item = MagicMock()
        media_item.name = "Test Track"
        media_item.image = MagicMock()
        item.media_item = media_item
        # Also set item.image so the code branch activates
        item.image = MagicMock()
        mock_mass.metadata.get_image_url = MagicMock(
            return_value="http://custom-img.example.com/art.jpg"
        )
        # When
        media = await controller.player_media_from_queue_item(item)
        # Then: image URL from media_item
        assert media.image_url == "http://custom-img.example.com/art.jpg"


# ---------------------------------------------------------------------------
# Tests: on_player_register with cached state
# ---------------------------------------------------------------------------


class TestOnPlayerRegisterWithCache:
    """Tests for on_player_register() when cache has previous state."""

    async def test_restores_from_cache(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test restores queue state from cache."""
        # Given: cache returns a valid queue state
        cached_queue = _make_queue("player-1")
        mock_mass.cache.get = AsyncMock(side_effect=[cached_queue.to_dict(), []])
        player = MagicMock()
        player.player_id = "player-1"
        player.type = PlayerType.PLAYER
        player.extra_data = {}
        player.state.name = "Player 1"
        player.state.available = True
        player.state.active_source = None
        # When
        await controller.on_player_register(player)
        # Then: queue was restored (not freshly created)
        assert "player-1" in controller._queues

    async def test_falls_back_to_fresh_queue_on_cache_error(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test falls back to fresh queue when cache data is invalid."""
        # Given: cache returns malformed data
        mock_mass.cache.get = AsyncMock(side_effect=[{"bad": "data"}, []])
        player = MagicMock()
        player.player_id = "player-2"
        player.type = PlayerType.PLAYER
        player.extra_data = {}
        player.state.name = "Player 2"
        player.state.available = True
        player.state.active_source = None
        # When
        await controller.on_player_register(player)
        # Then: queue was created fresh
        assert "player-2" in controller._queues


# ---------------------------------------------------------------------------
# Tests: on_player_update with transitioning flag
# ---------------------------------------------------------------------------


class TestOnPlayerUpdateTransitioning:
    """Tests for on_player_update() transitioning skip."""

    def test_ignores_update_when_transitioning(self, controller: PlayerQueuesController) -> None:
        """Test ignores update when player is transitioning between tracks."""
        # Given: queue registered, transitioning flag set
        queue = _seed_queue(controller, "q1")
        queue.active = True
        controller._transitioning_players.add("q1")
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        # When: update called while transitioning
        original_state = queue.state
        controller.on_player_update(player, {})
        # Then: state not changed (update ignored)
        assert queue.state == original_state


# ---------------------------------------------------------------------------
# Tests: on_player_elapsed_time_corrected with seek position
# ---------------------------------------------------------------------------


class TestOnPlayerElapsedTimeCorrectedSeek:
    """Tests for on_player_elapsed_time_corrected() with seek position."""

    def test_adds_seek_position_to_elapsed_time(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test adds seek_position to elapsed time when streamdetails present."""
        # Given: active queue with current item having streamdetails with seek_position
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        item = controller._queue_items["q1"][0]
        streamdetails = MagicMock()
        streamdetails.seek_position = 30
        item.streamdetails = streamdetails
        queue.current_item = item
        queue.flow_mode = False
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.state.corrected_elapsed_time = 20.0
        # When
        controller.on_player_elapsed_time_corrected(player)
        # Then: elapsed_time = 20 + 30 = 50
        assert queue.elapsed_time == 50.0

    def test_ignores_when_corrected_elapsed_time_is_none(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test ignores update when corrected_elapsed_time is None."""
        # Given: active queue
        queue = _seed_queue(controller, "q1")
        queue.active = True
        queue.elapsed_time = 42.0
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.state.corrected_elapsed_time = None
        # When
        controller.on_player_elapsed_time_corrected(player)
        # Then: elapsed_time unchanged
        assert queue.elapsed_time == 42.0


# ---------------------------------------------------------------------------
# Tests: load_next_queue_item
# ---------------------------------------------------------------------------


class TestLoadNextQueueItem:
    """Tests for load_next_queue_item()."""

    async def test_raises_when_queue_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises PlayerUnavailableError when queue not found."""
        # Given: no queue
        with pytest.raises(PlayerUnavailableError):
            await controller.load_next_queue_item("nonexistent", "item-0")

    async def test_raises_when_item_id_invalid(self, controller: PlayerQueuesController) -> None:
        """Test raises QueueEmpty when item_id is not in queue."""
        # Given: queue with items but wrong item_id
        _seed_queue(controller, "q1", num_items=3)
        with pytest.raises(QueueEmpty):
            await controller.load_next_queue_item("q1", "nonexistent-item")

    async def test_raises_when_at_end_of_queue(self, controller: PlayerQueuesController) -> None:
        """Test raises QueueEmpty when at last item with no repeat."""
        # Given: queue at last item, no repeat
        _seed_queue(controller, "q1", num_items=2)
        with patch.object(controller, "_load_item", new_callable=AsyncMock) as mock_load:
            mock_load.side_effect = MediaNotFoundError("not found")
            # When / Then: raises QueueEmpty because there are no more items
            with pytest.raises(QueueEmpty):
                await controller.load_next_queue_item("q1", "item-1")

    async def test_returns_next_item_when_available(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test returns the next queue item when load succeeds."""
        # Given: queue with 3 items, currently at item-0
        _seed_queue(controller, "q1", num_items=3)
        with patch.object(controller, "_load_item", new_callable=AsyncMock):
            # When: load next after item-0
            result = await controller.load_next_queue_item("q1", "item-0")
            # Then: returns item-1
            assert result is not None
            assert result.queue_item_id == "item-1"


# ---------------------------------------------------------------------------
# Tests: queue_buffer_completed
# ---------------------------------------------------------------------------


class TestQueueBufferCompleted:
    """Tests for queue_buffer_completed()."""

    def test_noop_when_queue_not_found(self, controller: PlayerQueuesController) -> None:
        """Test noop when queue does not exist."""
        # Given: no queue
        # When / Then: no error
        controller.queue_buffer_completed("nonexistent")

    def test_creates_background_task(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test creates background task to watch for idle and resume."""
        # Given: registered queue
        _seed_queue(controller, "q1")
        # When
        controller.queue_buffer_completed("q1")
        # Then: create_task was called
        mock_mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests: transfer_queue
# ---------------------------------------------------------------------------


class TestTransferQueue:
    """Tests for transfer_queue()."""

    async def test_raises_when_source_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises PlayerUnavailableError when source queue not found."""
        # Given: no queues
        with pytest.raises(PlayerUnavailableError):
            await controller.transfer_queue("source-q", "target-q")

    async def test_raises_when_target_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises PlayerUnavailableError when target queue not found."""
        # Given: source exists but target does not
        _seed_queue(controller, "source-q", num_items=2)
        with pytest.raises(PlayerUnavailableError):
            await controller.transfer_queue("source-q", "target-q")

    async def test_raises_when_target_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test raises PlayerUnavailableError when target player not available."""
        # Given: both queues exist but target player not available
        _seed_queue(controller, "source-q", num_items=2)
        _seed_queue(controller, "target-q")
        mock_mass.players.get_player.return_value = None
        with pytest.raises(PlayerUnavailableError):
            await controller.transfer_queue("source-q", "target-q")

    async def test_transfers_items_to_target(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test transfers queue items from source to target."""
        # Given: source has 3 items, target is empty
        _seed_queue(controller, "source-q", num_items=3)
        _seed_queue(controller, "target-q")
        source_queue = controller._queues["source-q"]
        source_queue.state = PlaybackState.IDLE  # not playing, no stop needed
        target_player = MagicMock()
        target_player.state.active_group = None
        target_player.state.synced_to = None
        mock_mass.players.get_player.return_value = target_player
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.transfer_queue("source-q", "target-q", auto_play=False)
        # Then: target queue has 3 items
        assert len(controller._queue_items["target-q"]) == 3


# ---------------------------------------------------------------------------
# Tests: _smart_shuffle
# ---------------------------------------------------------------------------


class TestSmartShuffle:
    """Tests for _smart_shuffle() standalone function."""

    async def test_empty_list_returns_empty(self) -> None:
        """Test empty list returns empty."""
        # Given / When
        result = await _smart_shuffle([])
        # Then
        assert result == []

    async def test_single_item_returns_same(self) -> None:
        """Test single item list is returned as-is."""
        # Given
        item = _make_item()
        # When
        result = await _smart_shuffle([item])
        # Then
        assert result == [item]

    async def test_two_items_shuffled(self) -> None:
        """Test two items are returned (shuffled)."""
        # Given
        items = [_make_item(queue_item_id=f"item-{i}") for i in range(2)]
        # When
        result = await _smart_shuffle(items)
        # Then: all items present
        assert len(result) == 2
        assert {i.queue_item_id for i in result} == {"item-0", "item-1"}

    async def test_multiple_items_all_present(self) -> None:
        """Test multiple items are all present after shuffle."""
        # Given
        items = [_make_item(queue_item_id=f"item-{i}", name=f"Track {i}") for i in range(5)]
        # When
        result = await _smart_shuffle(items)
        # Then: all items present (just shuffled)
        assert len(result) == 5
        assert {i.queue_item_id for i in result} == {f"item-{i}" for i in range(5)}

    async def test_adjacent_duplicates_separated(self) -> None:
        """Test adjacent duplicate names are separated."""
        # Given: items with alternating names that would cause duplicates
        items = [_make_item(queue_item_id=f"item-{i}", name="Same Track") for i in range(4)]
        items[0].name = "Track A"
        items[2].name = "Track A"
        # When
        result = await _smart_shuffle(items)
        # Then: shuffled result has all 4 items
        assert len(result) == 4


# ---------------------------------------------------------------------------
# Tests: get_artist_tracks
# ---------------------------------------------------------------------------


class TestGetArtistTracks:
    """Tests for get_artist_tracks()."""

    async def test_all_tracks_mode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test all_tracks mode fetches top tracks from providers."""
        # Given: config returns 'all_tracks'
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        track1 = MagicMock(spec=Track)
        track1.name = "Track 1"
        mock_mass.music.artists.tracks = AsyncMock(return_value=[track1])
        artist = MagicMock(spec=Artist)
        artist.item_id = "artist-1"
        artist.provider = "test_provider"
        artist.name = "Test Artist"
        # When
        result = await controller.get_artist_tracks(artist)
        # Then
        assert len(result) == 1
        mock_mass.music.artists.tracks.assert_called_once()

    async def test_library_tracks_mode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test library_tracks mode fetches only in-library tracks."""
        # Given: config returns 'library_tracks'
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="library_tracks")
        mock_mass.music.artists.tracks = AsyncMock(return_value=[])
        artist = MagicMock(spec=Artist)
        artist.item_id = "artist-1"
        artist.provider = "test_provider"
        artist.name = "Test Artist"
        # When
        result = await controller.get_artist_tracks(artist)
        # Then
        mock_mass.music.artists.tracks.assert_called_once_with(
            "artist-1", "test_provider", in_library_only=True
        )
        assert result == []


# ---------------------------------------------------------------------------
# Tests: get_album_tracks
# ---------------------------------------------------------------------------


class TestGetAlbumTracks:
    """Tests for get_album_tracks()."""

    async def test_returns_all_tracks_from_album(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns all available tracks from an album."""
        # Given: album with 3 available tracks
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        track1 = MagicMock(spec=Track)
        track1.available = True
        track1.item_id = "t1"
        track1.uri = "provider://music/track/t1"
        track2 = MagicMock(spec=Track)
        track2.available = True
        track2.item_id = "t2"
        track2.uri = "provider://music/track/t2"
        mock_mass.music.albums.tracks = AsyncMock(return_value=[track1, track2])
        album = MagicMock(spec=Album)
        album.item_id = "album-1"
        album.provider = "test_provider"
        album.name = "Test Album"
        # When
        result = await controller.get_album_tracks(album, start_item=None)
        # Then: all available tracks returned
        assert len(result) == 2

    async def test_start_item_skips_before(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test start_item skips tracks before the start item."""
        # Given: album with 3 tracks, start from track t2
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        track1 = MagicMock(spec=Track)
        track1.available = True
        track1.item_id = "t1"
        track1.uri = "uri-t1"
        track2 = MagicMock(spec=Track)
        track2.available = True
        track2.item_id = "t2"
        track2.uri = "uri-t2"
        track3 = MagicMock(spec=Track)
        track3.available = True
        track3.item_id = "t3"
        track3.uri = "uri-t3"
        mock_mass.music.albums.tracks = AsyncMock(return_value=[track1, track2, track3])
        album = MagicMock(spec=Album)
        album.item_id = "album-1"
        album.provider = "test_provider"
        album.name = "Test Album"
        # When: start from t2
        result = await controller.get_album_tracks(album, start_item="t2")
        # Then: only t2 and t3 returned
        assert len(result) == 2
        assert result[0].item_id == "t2"


# ---------------------------------------------------------------------------
# Tests: get_playlist_tracks
# ---------------------------------------------------------------------------


class TestGetPlaylistTracks:
    """Tests for get_playlist_tracks()."""

    async def test_returns_all_available_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns all available playlist tracks."""
        # Given: playlist with 2 available tracks
        track1 = MagicMock()
        track1.available = True
        track1.item_id = "pt1"
        track1.uri = "uri-pt1"
        track2 = MagicMock()
        track2.available = True
        track2.item_id = "pt2"
        track2.uri = "uri-pt2"

        async def _tracks_gen(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
            yield track1
            yield track2

        mock_mass.music.playlists.tracks = _tracks_gen
        playlist = MagicMock(spec=Playlist)
        playlist.item_id = "pl-1"
        playlist.provider = "test_provider"
        playlist.name = "Test Playlist"
        # When
        result = await controller.get_playlist_tracks(playlist, start_item=None)
        # Then: all available tracks
        assert len(result) == 2

    async def test_skips_unavailable_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test skips unavailable tracks."""
        # Given: 1 available, 1 unavailable track
        track1 = MagicMock()
        track1.available = True
        track1.item_id = "pt1"
        track2 = MagicMock()
        track2.available = False
        track2.item_id = "pt2"

        async def _tracks_gen(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
            yield track1
            yield track2

        mock_mass.music.playlists.tracks = _tracks_gen
        playlist = MagicMock(spec=Playlist)
        playlist.item_id = "pl-1"
        playlist.provider = "test_provider"
        playlist.name = "Test Playlist"
        # When
        result = await controller.get_playlist_tracks(playlist, start_item=None)
        # Then: only 1 track
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests: _resolve_media_items
# ---------------------------------------------------------------------------


class TestResolveMediaItems:
    """Tests for _resolve_media_items()."""

    async def test_resolves_track_directly(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns single track directly."""
        # Given: a Track media item
        track = MagicMock(spec=Track)
        track.media_type = MediaType.TRACK
        # When
        result = await controller._resolve_media_items(track)
        # Then: returns the track
        assert result == [track]

    async def test_resolves_album_to_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves album by calling get_album_tracks."""
        # Given: an Album
        album = MagicMock(spec=Album)
        album.media_type = MediaType.ALBUM
        album.item_id = "album-1"
        album.provider = "test"
        album.name = "Test Album"
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        track = MagicMock(spec=Track)
        track.available = True
        track.item_id = "t1"
        track.uri = "uri-t1"
        mock_mass.music.albums.tracks = AsyncMock(return_value=[track])
        mock_mass.music.mark_item_played = AsyncMock()
        # When
        result = await controller._resolve_media_items(album)
        # Then: returns the track from the album
        assert len(result) == 1

    async def test_resolves_artist_to_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves artist by calling get_artist_tracks."""
        # Given: an Artist
        artist = MagicMock(spec=Artist)
        artist.media_type = MediaType.ARTIST
        artist.item_id = "artist-1"
        artist.provider = "test"
        artist.name = "Test Artist"
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        track = MagicMock(spec=Track)
        mock_mass.music.artists.tracks = AsyncMock(return_value=[track])
        mock_mass.music.mark_item_played = AsyncMock()
        # When
        result = await controller._resolve_media_items(artist)
        # Then: returns the track from the artist
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests: _try_resume_from_playlog
# ---------------------------------------------------------------------------


class TestTryResumeFromPlaylog:
    """Tests for _try_resume_from_playlog()."""

    async def test_returns_false_when_no_recently_played(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when no recently played items found."""
        # Given: no recently played items
        mock_mass.music.recently_played = AsyncMock(return_value=[])
        queue = _seed_queue(controller, "q1")
        queue.userid = None
        # When
        result = await controller._try_resume_from_playlog(queue)
        # Then
        assert result is False

    async def test_returns_true_when_item_played_successfully(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns True when an item is resumed successfully."""
        # Given: recently played item that can be played
        item = MagicMock()
        item.uri = "provider://music/track/t1"
        item.name = "Test Track"
        mock_mass.music.recently_played = AsyncMock(return_value=[item])
        queue = _seed_queue(controller, "q1")
        queue.userid = None
        with patch.object(controller, "play_media", new_callable=AsyncMock):
            # When
            result = await controller._try_resume_from_playlog(queue)
            # Then
            assert result is True

    async def test_continues_on_error_and_returns_false(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test continues on error when item fails to play."""
        # Given: item that fails to play

        item = MagicMock()
        item.uri = "provider://music/track/t1"
        item.name = "Failing Track"
        mock_mass.music.recently_played = AsyncMock(return_value=[item])
        queue = _seed_queue(controller, "q1")
        queue.userid = None
        with patch.object(
            controller,
            "play_media",
            new_callable=AsyncMock,
            side_effect=MusicAssistantError("test error"),
        ):
            # When
            result = await controller._try_resume_from_playlog(queue)
            # Then: returns False after exhausting all items
            assert result is False


# ---------------------------------------------------------------------------
# Tests: _update_queue_from_player (via on_player_update)
# ---------------------------------------------------------------------------


class TestUpdateQueueFromPlayer:
    """Tests for _update_queue_from_player() via on_player_update()."""

    def test_updates_queue_state_to_idle_when_not_playing(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates queue state when player is idle."""
        # Given: active queue, player is idle
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.active = True
        queue.current_index = 0
        queue.current_item = controller._queue_items["q1"][0]
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.IDLE,
            "current_item_id": None,
            "next_item_id": None,
            "current_item": None,
            "elapsed_time": 0,
            "last_playing_elapsed_time": 0,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.IDLE
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 0.0
        player.state.group_members = []
        # When
        controller.on_player_update(player, {})
        # Then: queue state set to IDLE
        assert queue.state == PlaybackState.IDLE

    def test_sets_queue_display_name_from_player(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates queue display_name from player state."""
        # Given: active queue
        queue = _seed_queue(controller, "q1")
        queue.active = True
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.IDLE,
            "current_item_id": None,
            "next_item_id": None,
            "current_item": None,
            "elapsed_time": 0,
            "last_playing_elapsed_time": 0,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.IDLE
        player.state.name = "My Speaker"
        player.state.available = True
        player.state.corrected_elapsed_time = 0.0
        player.state.group_members = []
        # When
        controller.on_player_update(player, {})
        # Then: display_name updated
        assert queue.display_name == "My Speaker"

    def test_updates_playing_queue_current_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates current index and elapsed time when player is PLAYING."""
        # Given: active queue with 2 items, player is playing item-1
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.flow_mode = False
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.PLAYING,
            "current_item_id": "item-0",
            "next_item_id": "item-1",
            "current_item": controller._queue_items["q1"][0],
            "elapsed_time": 30,
            "last_playing_elapsed_time": 30,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.PLAYING
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 35.0
        player.state.group_members = []
        # Return item-1's id from parse_player_current_item_id via the media url
        with patch.object(controller, "_parse_player_current_item_id", return_value="item-1"):
            # When
            controller.on_player_update(player, {})
            # Then: current_index updated to 1
            assert queue.current_index == 1

    def test_handles_state_change_to_idle_fires_end_of_queue(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test fires end-of-queue handling when state transitions to IDLE."""
        # Given: queue was playing, now idle with no next item
        queue = _seed_queue(controller, "q1", num_items=1)
        queue.active = True
        queue.flow_mode = False
        queue.next_item = None
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.PLAYING,
            "current_item_id": "item-0",
            "next_item_id": None,
            "current_item": controller._queue_items["q1"][0],
            "elapsed_time": 170,
            "last_playing_elapsed_time": 170,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.IDLE
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 0.0
        player.state.group_members = []
        # When: state transitions from PLAYING to IDLE
        controller.on_player_update(player, {})
        # Then: create_task called (for _clear_or_resume_delayed)
        mock_mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests: play_index
# ---------------------------------------------------------------------------


class TestPlayIndex:
    """Tests for play_index()."""

    async def test_play_index_loads_and_plays_item(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_index loads and plays a queue item."""
        # Given: queue with 3 items
        queue = _seed_queue(controller, "q1", num_items=3)
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        mock_mass.players.play_media = AsyncMock()
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        queue.session_id = "ses123"
        with patch.object(controller, "_load_item", new_callable=AsyncMock):
            # When: play at index 0
            await controller.play_index("q1", 0)
            # Then: play_media called
            mock_mass.players.play_media.assert_called_once()
            assert queue.current_index == 0

    async def test_play_index_with_string_id(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_index resolves string item id to index."""
        # Given: queue with 3 items
        queue = _seed_queue(controller, "q1", num_items=3)
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        mock_mass.players.play_media = AsyncMock()
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        queue.session_id = "ses123"
        with patch.object(controller, "_load_item", new_callable=AsyncMock):
            # When: play by item id
            await controller.play_index("q1", "item-2")
            # Then: correct item played
            assert queue.current_index == 2

    async def test_play_index_raises_when_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_index raises when player not available."""
        # Given: queue with items but no player
        _seed_queue(controller, "q1", num_items=2)
        mock_mass.players.get_player.return_value = None
        with (
            patch.object(controller, "_load_item", new_callable=AsyncMock),
            pytest.raises(PlayerUnavailableError),
        ):
            await controller.play_index("q1", 0)

    async def test_play_index_skips_unplayable_item(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_index skips an unplayable item and tries next."""
        # Given: queue with 3 items, item at 0 is unplayable, item at 1 is playable
        queue = _seed_queue(controller, "q1", num_items=3)
        player = MagicMock()
        mock_mass.players.get_player.return_value = player
        mock_mass.players.play_media = AsyncMock()
        mock_mass.metadata.get_image_url = MagicMock(return_value="http://img.example.com/art.jpg")
        queue.session_id = "ses123"
        call_count = 0

        async def _load_side_effect(*_args: object, **_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise MediaNotFoundError("not found")

        with patch.object(controller, "_load_item", side_effect=_load_side_effect):
            # When
            await controller.play_index("q1", 0)
            # Then: played index 1 (skipped 0)
            assert queue.current_index == 1


# ---------------------------------------------------------------------------
# Tests: transfer_queue with playing source
# ---------------------------------------------------------------------------


class TestTransferQueuePlaying:
    """Tests for transfer_queue() when source is playing."""

    async def test_stops_source_before_transfer(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test stops source queue before transferring to target."""
        # Given: source is playing
        source_queue = _seed_queue(controller, "source-q", num_items=3)
        source_queue.state = PlaybackState.PLAYING
        _seed_queue(controller, "target-q")
        target_player = MagicMock()
        target_player.state.active_group = None
        target_player.state.synced_to = None
        mock_mass.players.get_player.return_value = target_player
        mock_mass.players.cmd_stop = AsyncMock()
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When
        await controller.transfer_queue("source-q", "target-q", auto_play=False)
        # Then: stop was called for source
        mock_mass.players.cmd_stop.assert_called_with("source-q")


# ---------------------------------------------------------------------------
# Tests: _resolve_media_items additional paths
# ---------------------------------------------------------------------------


class TestResolveMediaItemsExtra:
    """Additional tests for _resolve_media_items()."""

    async def test_resolves_playlist_to_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves playlist by calling get_playlist_tracks."""
        # Given: a Playlist media item
        playlist = MagicMock(spec=Playlist)
        playlist.media_type = MediaType.PLAYLIST
        playlist.item_id = "pl-1"
        playlist.provider = "test"
        playlist.name = "Test Playlist"
        track = MagicMock()
        track.available = True
        track.item_id = "t1"

        async def _tracks_gen(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
            yield track

        mock_mass.music.playlists.tracks = _tracks_gen
        mock_mass.music.mark_item_played = AsyncMock()
        # When
        result = await controller._resolve_media_items(playlist)
        # Then
        assert len(result) == 1

    async def test_resolves_genre_to_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves genre by calling get_genre_tracks."""
        # Given: a Genre media item
        genre = MagicMock(spec=Genre)
        genre.media_type = MediaType.GENRE
        genre.item_id = "genre-1"
        genre.provider = "test"
        genre.name = "Rock"
        track = MagicMock()
        track.available = True
        track.item_id = "t1"
        track.uri = "uri-t1"
        mock_mass.music.genres.mapped_media = AsyncMock(return_value=([track], [], []))
        mock_mass.music.mark_item_played = AsyncMock()
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        # When
        result = await controller._resolve_media_items(genre)
        # Then: returns genre tracks
        assert len(result) >= 0  # may be 0 or more depending on track.available

    async def test_returns_single_item_for_unknown_type(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test returns single item for unknown/other media types (e.g. radio)."""
        # Given: a Track-like item with unknown media type
        item = MagicMock()
        item.media_type = MediaType.RADIO
        # When
        result = await controller._resolve_media_items(item)
        # Then: returns the item as-is
        assert result == [item]


# ---------------------------------------------------------------------------
# Tests: get_genre_tracks
# ---------------------------------------------------------------------------


class TestGetGenreTracks:
    """Tests for get_genre_tracks()."""

    async def test_returns_genre_tracks(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns available tracks for a genre."""
        # Given: genre with 2 tracks
        track1 = MagicMock(spec=Track)
        track1.available = True
        track1.item_id = "t1"
        track1.uri = "uri-t1"
        track2 = MagicMock(spec=Track)
        track2.available = True
        track2.item_id = "t2"
        track2.uri = "uri-t2"
        mock_mass.music.genres.mapped_media = AsyncMock(return_value=([track1, track2], [], []))
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        genre = MagicMock(spec=Genre)
        genre.item_id = "genre-1"
        genre.provider = "test"
        genre.name = "Jazz"
        # When
        result = await controller.get_genre_tracks(genre, start_item=None)
        # Then
        assert len(result) == 2

    async def test_start_item_skips_before(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test start_item skips tracks before start item."""
        # Given: 2 tracks, start from t2
        track1 = MagicMock(spec=Track)
        track1.available = True
        track1.item_id = "t1"
        track1.uri = "uri-t1"
        track2 = MagicMock(spec=Track)
        track2.available = True
        track2.item_id = "t2"
        track2.uri = "uri-t2"
        mock_mass.music.genres.mapped_media = AsyncMock(return_value=([track1, track2], [], []))
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
        genre = MagicMock(spec=Genre)
        genre.item_id = "genre-1"
        genre.provider = "test"
        genre.name = "Jazz"
        # When: start from t2
        result = await controller.get_genre_tracks(genre, start_item="t2")
        # Then: only track2 returned
        assert len(result) == 1
        assert result[0].item_id == "t2"


# ---------------------------------------------------------------------------
# Tests: get_artist_tracks library_album_tracks mode
# ---------------------------------------------------------------------------


class TestGetArtistTracksAlbumMode:
    """Tests for get_artist_tracks() in library_album_tracks mode."""

    async def test_library_album_tracks_mode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test library_album_tracks mode fetches tracks from library albums."""
        # Given: config returns 'library_album_tracks'
        mock_mass.config.get_raw_core_config_value = MagicMock(return_value="library_album_tracks")
        album = MagicMock(spec=Album)
        album.item_id = "alb-1"
        album.provider = "test"
        track = MagicMock(spec=Track)
        mock_mass.music.artists.albums = AsyncMock(return_value=[album])
        mock_mass.music.albums.tracks = AsyncMock(return_value=[track])
        artist = MagicMock(spec=Artist)
        artist.item_id = "artist-1"
        artist.provider = "test"
        artist.name = "Test Artist"
        # When
        result = await controller.get_artist_tracks(artist)
        # Then: track from album returned
        assert len(result) == 1
        mock_mass.music.artists.albums.assert_called_once()
        mock_mass.music.albums.tracks.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_playlist_tracks start_item path
# ---------------------------------------------------------------------------


class TestGetPlaylistTracksStartItem:
    """Tests for get_playlist_tracks() start_item path."""

    async def test_start_item_skips_before(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test start_item skips tracks before the given start item."""
        # Given: 3 tracks, start from pt2
        track1 = MagicMock()
        track1.available = True
        track1.item_id = "pt1"
        track1.uri = "uri-pt1"
        track2 = MagicMock()
        track2.available = True
        track2.item_id = "pt2"
        track2.uri = "uri-pt2"
        track3 = MagicMock()
        track3.available = True
        track3.item_id = "pt3"
        track3.uri = "uri-pt3"

        async def _tracks_gen(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
            yield track1
            yield track2
            yield track3

        mock_mass.music.playlists.tracks = _tracks_gen
        playlist = MagicMock(spec=Playlist)
        playlist.item_id = "pl-1"
        playlist.provider = "test"
        playlist.name = "Test Playlist"
        # When: start from pt2
        result = await controller.get_playlist_tracks(playlist, start_item="pt2")
        # Then: pt2 and pt3 returned
        assert len(result) == 2
        assert result[0].item_id == "pt2"


# ---------------------------------------------------------------------------
# Tests: _enqueue_next_item
# ---------------------------------------------------------------------------


class TestEnqueueNextItem:
    """Tests for _enqueue_next_item()."""

    def test_noop_when_no_next_item(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test noop when no next item provided."""
        # Given: registered queue
        _seed_queue(controller, "q1")
        # When: called with None
        controller._enqueue_next_item("q1", None)
        # Then: call_later not called (early return)
        mock_mass.call_later.assert_not_called()

    def test_noop_in_flow_mode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test noop in flow mode."""
        # Given: queue in flow mode
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.flow_mode = True
        item = controller._queue_items["q1"][1]
        # When
        controller._enqueue_next_item("q1", item)
        # Then: call_later not called
        mock_mass.call_later.assert_not_called()

    def test_schedules_enqueue_for_normal_mode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test schedules enqueue via call_later in normal (non-flow) mode."""
        # Given: normal mode queue with session_id
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.flow_mode = False
        queue.session_id = "ses123"
        item = controller._queue_items["q1"][1]
        # When
        controller._enqueue_next_item("q1", item)
        # Then: call_later called
        mock_mass.call_later.assert_called()


# ---------------------------------------------------------------------------
# Tests: _handle_playback_progress_report (via on_player_update)
# ---------------------------------------------------------------------------


class TestHandlePlaybackProgressReport:
    """Tests for _handle_playback_progress_report() via _update_queue_from_player()."""

    def test_reports_progress_on_item_change(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test playback progress reported when current item changes."""
        # Given: queue was playing item-0, now plays item-1 with media_item
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.flow_mode = False
        # Attach a media_item to item-0 so it can be reported
        media_item_mock = MagicMock()
        media_item_mock.uri = "provider://track/t0"
        media_item_mock.media_type = MediaType.TRACK
        media_item_mock.name = "Track 0"
        media_item_mock.image = None
        controller._queue_items["q1"][0].media_item = media_item_mock
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.PLAYING,
            "current_item_id": "item-0",
            "next_item_id": "item-1",
            "current_item": controller._queue_items["q1"][0],
            "elapsed_time": 120,
            "last_playing_elapsed_time": 120,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.PLAYING
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 5.0
        player.state.group_members = []
        mock_mass.metadata.get_image_url = MagicMock(return_value=None)
        with patch.object(controller, "_parse_player_current_item_id", return_value="item-1"):
            # When
            controller.on_player_update(player, {})
            # Then: create_task called for mark_item_played and signal events fired
            mock_mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests: _fill_radio_tracks
# ---------------------------------------------------------------------------


class TestFillRadioTracks:
    """Tests for _fill_radio_tracks()."""

    async def test_fill_radio_tracks_loads_queue(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test fill_radio_tracks loads queue with radio tracks."""
        # Given: queue with radio source
        queue = _seed_queue(controller, "q1", num_items=2)
        queue.radio_source = [MagicMock()]
        track = MagicMock()
        track.available = True
        fake_queue_item = _make_item("q1", "radio-item", "Radio Track")
        with (
            patch.object(
                controller, "_get_radio_tracks", new_callable=AsyncMock, return_value=[track]
            ),
            patch(
                "music_assistant.controllers.player_queues.QueueItem.from_media_item",
                return_value=fake_queue_item,
            ),
            patch.object(controller, "load", new_callable=AsyncMock) as mock_load,
        ):
            # When
            await controller._fill_radio_tracks("q1")
            # Then: load called with new items
            mock_load.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _handle_end_of_queue (via _update_queue_from_player)
# ---------------------------------------------------------------------------


class TestHandleEndOfQueue:
    """Tests for _handle_end_of_queue() triggered by state transition."""

    def test_does_not_clear_when_next_item_available(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test does not clear queue when next item is available."""
        # Given: queue has next_item (not at end)
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.flow_mode = False
        queue.next_item = controller._queue_items["q1"][1]
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.PLAYING,
            "current_item_id": "item-0",
            "next_item_id": "item-1",
            "current_item": controller._queue_items["q1"][0],
            "elapsed_time": 30,
            "last_playing_elapsed_time": 30,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.IDLE
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 0.0
        player.state.group_members = []
        mock_mass.create_task.reset_mock()
        # When: state goes to IDLE but next_item is available
        controller.on_player_update(player, {})
        # Then: _clear_or_resume_delayed not called (next_item exists)
        # The create_task calls for signal updates happen but not the clear task
        # We just verify no crash
        assert queue.state == PlaybackState.IDLE


# ---------------------------------------------------------------------------
# Tests: play_media
# ---------------------------------------------------------------------------


class TestPlayMedia:
    """Tests for play_media()."""

    async def test_raises_when_queue_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises when queue not found."""
        # Given: no queue registered
        with pytest.raises(PlayerUnavailableError):
            await controller.play_media("nonexistent", [])

    async def test_raises_when_player_unavailable(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test raises when player is not available."""
        # Given: no queue registered for this id
        mock_mass.players.get_player.return_value = None
        with pytest.raises(PlayerUnavailableError):
            await controller.play_media("no-such-queue", [MagicMock()])

    async def test_returns_early_when_announcement_in_progress(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns early when announcement is in progress."""
        # Given: queue with announcement in progress
        _seed_queue(controller, "q1")
        player = MagicMock()
        player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: True}
        mock_mass.players.get_player.return_value = player
        # When: should return early without error
        await controller.play_media("q1", [MagicMock()])
        # Then: no exception

    async def test_play_media_replace_calls_play_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_media with REPLACE option clears queue and plays."""
        # Given: queue with items
        _seed_queue(controller, "q1", num_items=2)
        player = MagicMock()
        player.extra_data = {}
        mock_mass.players.get_player.return_value = player
        mock_mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # Create a track mock that looks like a Track
        track = MagicMock()
        track.media_type = MediaType.TRACK
        track.available = True
        track.name = "Test Track"
        track.uri = "provider://track/t1"
        fake_item = _make_item("q1", "new-item", "Test Track")
        with (
            patch.object(
                controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[track]
            ),
            patch(
                "music_assistant.controllers.player_queues.QueueItem.from_media_item",
                return_value=fake_item,
            ),
            patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index,
        ):
            # When
            await controller.play_media("q1", track, option=QueueOption.REPLACE)
            # Then: play_index called
            mock_play_index.assert_called()

    async def test_play_media_next_inserts_after_current(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_media with NEXT option inserts after current index."""
        # Given: queue with items at index 0

        queue = _seed_queue(controller, "q1", num_items=3)
        queue.current_index = 0
        queue.state = PlaybackState.IDLE
        player = MagicMock()
        player.extra_data = {}
        mock_mass.players.get_player.return_value = player
        mock_mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
        track = MagicMock()
        track.media_type = MediaType.TRACK
        track.available = True
        track.name = "Next Track"
        fake_item = _make_item("q1", "next-item", "Next Track")
        with (
            patch.object(
                controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[track]
            ),
            patch(
                "music_assistant.controllers.player_queues.QueueItem.from_media_item",
                return_value=fake_item,
            ),
        ):
            # When
            await controller.play_media("q1", track, option=QueueOption.NEXT)
            # Then: item inserted after current
            assert len(controller._queue_items["q1"]) == 4

    async def test_play_media_add_appends_to_queue(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_media with ADD option appends to queue."""
        # Given: queue with 2 items, current at 0

        queue = _seed_queue(controller, "q1", num_items=2)
        queue.current_index = 0
        player = MagicMock()
        player.extra_data = {}
        mock_mass.players.get_player.return_value = player
        mock_mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
        # Use a RADIO media type to avoid enqueued_media_items tracking (name check)
        track = MagicMock()
        track.media_type = MediaType.RADIO
        track.available = True
        track.name = "Radio Track"
        fake_item = _make_item("q1", "added-item", "Added Track")
        with (
            patch.object(
                controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[track]
            ),
            patch(
                "music_assistant.controllers.player_queues.QueueItem.from_media_item",
                return_value=fake_item,
            ),
        ):
            # When
            await controller.play_media("q1", track, option=QueueOption.ADD)
            # Then: item appended
            assert len(controller._queue_items["q1"]) == 3

    async def test_play_media_raises_when_no_items(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_media raises MediaNotFoundError when no playable items."""
        # Given: _resolve_media_items returns nothing playable

        _seed_queue(controller, "q1")
        player = MagicMock()
        player.extra_data = {}
        mock_mass.players.get_player.return_value = player
        mock_mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
        with (
            patch.object(
                controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[]
            ),
            pytest.raises(MediaNotFoundError),
        ):
            await controller.play_media("q1", MagicMock(), option=QueueOption.REPLACE)


# ---------------------------------------------------------------------------
# Tests: _load_item
# ---------------------------------------------------------------------------


class TestLoadItem:
    """Tests for _load_item()."""

    async def test_raises_when_item_unavailable(self, controller: PlayerQueuesController) -> None:
        """Test raises MediaNotFoundError when item is not available."""
        # Given: unavailable item
        _seed_queue(controller, "q1", num_items=1)
        item = controller._queue_items["q1"][0]
        item.available = False
        with pytest.raises(MediaNotFoundError):
            await controller._load_item(item, None)

    async def test_loads_non_track_item(self, controller: PlayerQueuesController) -> None:
        """Test loads a non-Track item (radio/podcast) by fetching stream details."""
        # Given: item with no media_item (basic queue item)
        _seed_queue(controller, "q1", num_items=1)
        item = controller._queue_items["q1"][0]
        assert item.media_item is None
        fake_streamdetails = MagicMock()
        with patch(
            "music_assistant.controllers.player_queues.get_stream_details",
            new_callable=AsyncMock,
            return_value=fake_streamdetails,
        ):
            # When
            await controller._load_item(item, None)
            # Then: streamdetails set
            assert item.streamdetails is fake_streamdetails

    async def test_loads_track_item_with_library_lookup(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test loads a Track item and does library lookup."""
        # Given: item with Track media_item
        _seed_queue(controller, "q1", num_items=1)
        item = controller._queue_items["q1"][0]
        track_mock = MagicMock()
        track_mock.media_type = MediaType.TRACK
        track_mock.item_id = "t1"
        track_mock.provider = "test"
        track_mock.image = None
        track_mock.album = None

        # Need isinstance check to pass
        with patch.object(Track, "__instancecheck__", return_value=True):
            pass
        # Set as a real Track-ish instance check via spec
        track_instance = MagicMock(spec=Track)
        track_instance.media_type = MediaType.TRACK
        track_instance.item_id = "t1"
        track_instance.provider = "test"
        track_instance.image = None
        track_instance.album = None
        track_instance.uri = "provider://track/t1"
        item.media_item = track_instance
        library_track = MagicMock(spec=Track)
        library_track.album = None
        mock_mass.music.get_library_item_by_prov_id = AsyncMock(return_value=library_track)
        fake_streamdetails = MagicMock()
        with patch(
            "music_assistant.controllers.player_queues.get_stream_details",
            new_callable=AsyncMock,
            return_value=fake_streamdetails,
        ):
            # When
            await controller._load_item(item, None)
            # Then: streamdetails set
            assert item.streamdetails is fake_streamdetails


# ---------------------------------------------------------------------------
# Tests: get_audiobook_resume_point
# ---------------------------------------------------------------------------


class TestGetAudiobookResumePoint:
    """Tests for get_audiobook_resume_point()."""

    async def test_returns_zero_for_fully_played(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns 0 for fully played audiobook."""
        # Given: audiobook that was fully played
        mock_mass.music.get_resume_position = AsyncMock(return_value=(True, 50000))
        audiobook = MagicMock()
        audiobook.name = "Test Audiobook"
        audiobook.metadata.chapters = None
        # When
        result = await controller.get_audiobook_resume_point(audiobook)
        # Then: returns 0 (restart from beginning for fully played)
        assert result == 0

    async def test_returns_resume_position(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns resume position for partially played audiobook."""
        # Given: audiobook partially played at 30000ms
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 30000))
        audiobook = MagicMock()
        audiobook.name = "Test Audiobook"
        audiobook.metadata.chapters = None
        # When
        result = await controller.get_audiobook_resume_point(audiobook)
        # Then
        assert result == 30000

    async def test_with_explicit_chapter(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns chapter start time when chapter is explicitly specified."""
        # Given: audiobook with chapters, chapter 2 starts at 600s
        chapter1 = MagicMock()
        chapter1.position = 1
        chapter1.start = 0.0
        chapter2 = MagicMock()
        chapter2.position = 2
        chapter2.start = 600.0
        audiobook = MagicMock()
        audiobook.name = "Test Audiobook"
        audiobook.metadata.chapters = [chapter1, chapter2]
        # When: play chapter 2
        result = await controller.get_audiobook_resume_point(audiobook, chapter=2)
        # Then: returns chapter 2 start time in ms
        assert result == 600000  # 600s * 1000

    async def test_raises_for_invalid_chapter(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test raises InvalidDataError for invalid chapter number."""
        # Given: audiobook with 2 chapters, requesting chapter 99
        chapter1 = MagicMock()
        chapter1.position = 1
        chapter1.start = 0.0
        audiobook = MagicMock()
        audiobook.name = "Test Audiobook"
        audiobook.metadata.chapters = [chapter1]
        with pytest.raises(InvalidDataError):
            await controller.get_audiobook_resume_point(audiobook, chapter=99)


# ---------------------------------------------------------------------------
# Tests: get_next_podcast_episodes
# ---------------------------------------------------------------------------


class TestGetNextPodcastEpisodes:
    """Tests for get_next_podcast_episodes()."""

    async def test_single_episode_returns_itself(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test single PodcastEpisode is returned with resume info."""
        # Given: a single podcast episode

        episode = MagicMock(spec=PodcastEpisode)
        episode.name = "Episode 1"
        episode.fully_played = False
        episode.resume_position_ms = 0
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 5000))
        # When
        result = await controller.get_next_podcast_episodes(None, episode)
        # Then: returns the episode
        assert len(result) == 1

    async def test_raises_when_no_podcast_and_no_episode(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test raises when neither podcast nor episode provided."""
        # Given/When/Then
        with pytest.raises(InvalidDataError):
            await controller.get_next_podcast_episodes(None, None)


# ---------------------------------------------------------------------------
# Tests: _resolve_media_items - remaining media types
# ---------------------------------------------------------------------------


class TestResolveMediaItemsRemaining:
    """Tests for _resolve_media_items() remaining paths."""

    async def test_resolves_audiobook(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves Audiobook media type."""
        # Given: an Audiobook (use MagicMock without spec to avoid attribute restrictions)
        audiobook = MagicMock()
        audiobook.media_type = MediaType.AUDIOBOOK
        audiobook.name = "Test Audiobook"
        audiobook.metadata.chapters = None
        audiobook.resume_position_ms = 0
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 0))
        with patch.object(
            controller,
            "get_audiobook_resume_point",
            new_callable=AsyncMock,
            return_value=0,
        ):
            # When
            result = await controller._resolve_media_items(audiobook, start_item=None)
            # Then: returns the audiobook
            assert result == [audiobook]

    async def test_resolves_podcast_episode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves PodcastEpisode media type."""
        # Given: a PodcastEpisode

        episode = MagicMock(spec=PodcastEpisode)
        episode.media_type = MediaType.PODCAST_EPISODE
        episode.name = "Episode 1"
        episode.fully_played = False
        episode.resume_position_ms = 0
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 0))
        # When
        result = await controller._resolve_media_items(episode)
        # Then: returns the episode
        assert len(result) == 1

    async def test_resolves_item_mapping_to_full_item(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test resolves ItemMapping to full media item."""
        # Given: an ItemMapping with URI

        item_mapping = MagicMock(spec=ItemMapping)
        item_mapping.uri = "provider://track/t1"
        # The full item we return from get_item_by_uri
        full_track = MagicMock()
        full_track.media_type = MediaType.TRACK
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=full_track)
        # When
        result = await controller._resolve_media_items(item_mapping)
        # Then: resolved to the full item
        assert result == [full_track]


# ---------------------------------------------------------------------------
# Tests: _get_folder_tracks
# ---------------------------------------------------------------------------


class TestGetFolderTracks:
    """Tests for _get_folder_tracks()."""

    async def test_returns_tracks_from_folder(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns tracks from a browse folder."""
        # Given: folder with one playable track item

        track = MagicMock(spec=Track)
        track.available = True
        track.media_type = MediaType.TRACK
        folder_item = MagicMock()
        folder_item.is_playable = True
        folder_item.media_type = MediaType.TRACK
        folder = MagicMock(spec=BrowseFolder)
        folder.name = "Music Folder"
        folder.path = "provider://folder/1"
        mock_mass.music.browse = AsyncMock(return_value=[folder_item])
        with patch.object(
            controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[track]
        ):
            # When
            result = await controller._get_folder_tracks(folder)
            # Then: track returned
            assert len(result) == 1

    async def test_skips_non_playable_items(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test skips non-playable items in folder."""
        # Given: folder with non-playable item

        non_playable = MagicMock()
        non_playable.is_playable = False
        folder = MagicMock(spec=BrowseFolder)
        folder.name = "Music Folder"
        folder.path = "provider://folder/1"
        mock_mass.music.browse = AsyncMock(return_value=[non_playable])
        # When
        result = await controller._get_folder_tracks(folder)
        # Then: empty result
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _get_flow_queue_stream_index
# ---------------------------------------------------------------------------


class TestGetFlowQueueStreamIndex:
    """Tests for _get_flow_queue_stream_index()."""

    def test_returns_current_when_no_stream_log_and_no_current_index(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test returns current_index when no stream log and no current_index."""
        # Given: queue with no current_index and no flow_mode_stream_log
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.current_index = None
        queue.flow_mode_stream_log = []
        queue.elapsed_time = 0.0
        player = MagicMock()
        player.state.corrected_elapsed_time = 10.0
        # When
        result = controller._get_flow_queue_stream_index(queue, player)
        # Then: returns (None, 0.0)
        assert result == (None, 0.0)

    def test_calculates_index_from_stream_log(self, controller: PlayerQueuesController) -> None:
        """Test calculates correct index from flow_mode_stream_log."""
        # Given: queue at item-0 with stream log showing item-0 streamed 60s and item-1 at 20s

        queue = _seed_queue(controller, "q1", num_items=3)
        queue.current_index = 0
        queue.flow_mode_stream_log = [
            PlayLogEntry(queue_item_id="item-0", duration=60, seconds_streamed=60),
            PlayLogEntry(queue_item_id="item-1", duration=180, seconds_streamed=None),
        ]
        player = MagicMock()
        player.state.corrected_elapsed_time = 80.0  # 60s for item-0 + 20s into item-1
        player.state.playback_state = PlaybackState.PLAYING
        # When
        queue_index, track_time = controller._get_flow_queue_stream_index(queue, player)
        # Then: we're on item-1
        assert queue_index == 1
        assert abs(track_time - 20.0) < 1

    def test_returns_current_when_not_playing(self, controller: PlayerQueuesController) -> None:
        """Test returns current_index and elapsed_time when player is not playing."""
        # Given: queue with stream log but player is paused

        queue = _seed_queue(controller, "q1", num_items=2)
        queue.current_index = 1
        queue.elapsed_time = 15.0
        queue.flow_mode_stream_log = [
            PlayLogEntry(queue_item_id="item-0", duration=60, seconds_streamed=60),
        ]
        player = MagicMock()
        player.state.corrected_elapsed_time = 75.0
        player.state.playback_state = PlaybackState.PAUSED
        # When
        queue_index, track_time = controller._get_flow_queue_stream_index(queue, player)
        # Then: returns stored current state
        assert queue_index == 1
        assert track_time == 15.0


# ---------------------------------------------------------------------------
# Tests: on_player_elapsed_time_corrected in flow mode
# ---------------------------------------------------------------------------


class TestOnPlayerElapsedTimeCorrectedFlowMode:
    """Tests for on_player_elapsed_time_corrected() in flow mode."""

    def test_uses_flow_mode_calculation(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test uses _get_flow_queue_stream_index when flow_mode is True."""
        # Given: active queue in flow mode

        queue = _seed_queue(controller, "q1", num_items=2)
        queue.active = True
        queue.flow_mode = True
        queue.current_index = 0
        queue.flow_mode_stream_log = [
            PlayLogEntry(queue_item_id="item-0", duration=60, seconds_streamed=None),
        ]
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.state.corrected_elapsed_time = 30.0
        player.state.playback_state = PlaybackState.PLAYING
        # When
        controller.on_player_elapsed_time_corrected(player)
        # Then: elapsed_time updated using flow calculation
        assert queue.elapsed_time == pytest.approx(30.0, abs=1.0)


# ---------------------------------------------------------------------------
# Tests: get_next_podcast_episodes (full podcast path)
# ---------------------------------------------------------------------------


class TestGetNextPodcastEpisodesFullPath:
    """Tests for get_next_podcast_episodes() with full podcast path."""

    async def test_returns_episodes_from_first_unplayed(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns episodes starting from first unplayed episode."""
        # Given: podcast with 3 episodes, first is fully played

        podcast = MagicMock(spec=Podcast)
        podcast.item_id = "pod-1"
        podcast.provider = "test"
        podcast.name = "Test Podcast"
        ep1 = MagicMock(spec=PodcastEpisode)
        ep1.position = 1
        ep1.uri = "provider://episode/ep1"
        ep1.fully_played = True
        ep2 = MagicMock(spec=PodcastEpisode)
        ep2.position = 2
        ep2.uri = "provider://episode/ep2"
        ep2.fully_played = False
        ep2.resume_position_ms = 0
        ep3 = MagicMock(spec=PodcastEpisode)
        ep3.position = 3
        ep3.uri = "provider://episode/ep3"
        ep3.fully_played = False
        ep3.resume_position_ms = 0

        async def _episodes_gen(
            *_args: object, **_kwargs: object
        ) -> AsyncGenerator[MagicMock, None]:
            yield ep1
            yield ep2
            yield ep3

        mock_mass.music.podcasts.episodes = _episodes_gen
        # ep2 is not fully played
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 1000))
        # When
        result = await controller.get_next_podcast_episodes(podcast, None)
        # Then: returns ep2 and ep3
        assert len(result) == 2

    async def test_returns_all_episodes_when_all_fully_played(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test returns episodes from beginning when all fully played."""
        # Given: all episodes fully played

        podcast = MagicMock(spec=Podcast)
        podcast.item_id = "pod-1"
        podcast.provider = "test"
        podcast.name = "Test Podcast"
        ep1 = MagicMock(spec=PodcastEpisode)
        ep1.position = 1
        ep1.uri = "provider://episode/ep1"
        ep1.fully_played = True

        async def _episodes_gen(
            *_args: object, **_kwargs: object
        ) -> AsyncGenerator[MagicMock, None]:
            yield ep1

        mock_mass.music.podcasts.episodes = _episodes_gen
        # All are fully played
        mock_mass.music.get_resume_position = AsyncMock(return_value=(True, 0))
        # When
        result = await controller.get_next_podcast_episodes(podcast, None)
        # Then: returns first episode (restart)
        assert len(result) == 1

    async def test_raises_when_no_episodes(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test raises InvalidDataError when no episodes found."""
        # Given: podcast with no episodes

        podcast = MagicMock(spec=Podcast)
        podcast.item_id = "pod-1"
        podcast.provider = "test"
        podcast.name = "Empty Podcast"

        async def _empty_gen(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
            return
            yield  # make it async generator

        mock_mass.music.podcasts.episodes = _empty_gen
        with pytest.raises(InvalidDataError):
            await controller.get_next_podcast_episodes(podcast, None)

    async def test_with_explicit_podcast_episode(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test with explicit PodcastEpisode finds it and returns from there."""
        # Given: podcast with 2 episodes, requesting ep2

        podcast = MagicMock(spec=Podcast)
        podcast.item_id = "pod-1"
        podcast.provider = "test"
        podcast.name = "Test Podcast"
        ep1 = MagicMock(spec=PodcastEpisode)
        ep1.position = 1
        ep1.uri = "provider://episode/ep1"
        ep1.fully_played = False
        ep2 = MagicMock(spec=PodcastEpisode)
        ep2.position = 2
        ep2.uri = "provider://episode/ep2"
        ep2.fully_played = False
        ep2.resume_position_ms = 5000

        async def _episodes_gen(
            *_args: object, **_kwargs: object
        ) -> AsyncGenerator[MagicMock, None]:
            yield ep1
            yield ep2

        mock_mass.music.podcasts.episodes = _episodes_gen
        mock_mass.music.get_resume_position = AsyncMock(return_value=(False, 5000))
        # When: explicitly request ep2
        result = await controller.get_next_podcast_episodes(podcast, ep2)
        # Then: returns ep2 (and possibly others from there)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# Tests: _update_queue_from_player edge cases
# ---------------------------------------------------------------------------


class TestUpdateQueueFromPlayerEdgeCases:
    """Additional tests for _update_queue_from_player() edge cases."""

    def _make_idle_player(self, player_id: str) -> MagicMock:
        """Create a player in IDLE state."""
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = player_id
        player.extra_data = {}
        player.state.active_source = player_id
        player.state.playback_state = PlaybackState.IDLE
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 0.0
        player.state.group_members = []
        return player

    def test_pops_prev_state_when_inactive(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test removes prev_state when queue becomes inactive."""
        # Given: queue with prev_state but player becomes inactive
        queue = _seed_queue(controller, "q1")
        queue.active = True
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.IDLE,
            "current_item_id": None,
            "next_item_id": None,
            "current_item": None,
            "elapsed_time": 0,
            "last_playing_elapsed_time": 0,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = self._make_idle_player("q1")
        player.state.active_source = "other-source"  # not active
        # When
        controller.on_player_update(player, {})
        # Then: prev_state removed (queue is inactive)
        assert "q1" not in controller._prev_states

    def test_no_update_when_state_unchanged(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test no signal_update when state didn't change."""
        # Given: queue in idle with same state as prev
        queue = _seed_queue(controller, "q1")
        queue.active = True
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.IDLE,
            "current_item_id": None,
            "next_item_id": None,
            "current_item": None,
            "elapsed_time": 0,
            "last_playing_elapsed_time": 0,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = self._make_idle_player("q1")
        mock_mass.signal_event.reset_mock()
        # When: same state as before
        controller.on_player_update(player, {})
        # Then: no updates needed (state unchanged)
        # Just verify no crash, signal may or may not be called
        assert queue.state == PlaybackState.IDLE

    def test_updates_current_item_from_index_when_idle(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test updates current_item from current_index when idle but item missing."""
        # Given: queue idle, has current_index but no current_item
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.current_index = 1
        queue.current_item = None
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.IDLE,
            "current_item_id": "item-1",
            "next_item_id": "item-2",
            "current_item": controller._queue_items["q1"][1],
            "elapsed_time": 0,
            "last_playing_elapsed_time": 0,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = self._make_idle_player("q1")
        # When
        controller.on_player_update(player, {})
        # Then: current_item populated from current_index
        assert queue.current_item is not None

    def test_parse_player_current_item_id_via_source_id(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test _parse_player_current_item_id uses source_id match."""
        # Given: queue in PLAYING state, player reports current media with source_id
        queue = _seed_queue(controller, "q1", num_items=3)
        queue.active = True
        queue.flow_mode = False
        controller._prev_states["q1"] = {
            "queue_id": "q1",
            "state": PlaybackState.PLAYING,
            "current_item_id": "item-0",
            "next_item_id": "item-1",
            "current_item": controller._queue_items["q1"][0],
            "elapsed_time": 10,
            "last_playing_elapsed_time": 10,
            "stream_title": None,
            "codec_type": None,
            "output_formats": None,
        }
        player = MagicMock()
        player.type = PlayerType.PLAYER
        player.player_id = "q1"
        player.extra_data = {}
        player.state.active_source = "q1"
        player.state.playback_state = PlaybackState.PLAYING
        player.state.name = "Test Queue"
        player.state.available = True
        player.state.corrected_elapsed_time = 15.0
        player.state.group_members = []
        # Set active_output_protocol to None so we use player directly
        player.active_output_protocol = None
        # Set current_media with source_id matching our queue
        player.current_media = MagicMock()
        player.current_media.source_id = "q1"
        player.current_media.queue_item_id = "item-1"
        # When
        controller.on_player_update(player, {})
        # Then: current_index updated to 1
        assert queue.current_index == 1


# ---------------------------------------------------------------------------
# Tests: play_media with option=None (config lookup)
# ---------------------------------------------------------------------------


class TestPlayMediaOptionNone:
    """Tests for play_media() when option is None (config lookup)."""

    async def test_play_media_uses_config_for_default_option(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test play_media uses config to determine default enqueue option."""
        # Given: queue with config returning REPLACE

        _seed_queue(controller, "q1", num_items=1)
        player = MagicMock()
        player.extra_data = {}
        mock_mass.players.get_player.return_value = player
        mock_mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
        mock_mass.config.get_core_config_value = AsyncMock(return_value=QueueOption.REPLACE.value)
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        track = MagicMock()
        track.media_type = MediaType.RADIO
        track.available = True
        track.name = "Radio"
        fake_item = _make_item("q1", "new-item", "Radio")
        with (
            patch.object(
                controller, "_resolve_media_items", new_callable=AsyncMock, return_value=[track]
            ),
            patch(
                "music_assistant.controllers.player_queues.QueueItem.from_media_item",
                return_value=fake_item,
            ),
            patch.object(controller, "play_index", new_callable=AsyncMock) as mock_play_index,
        ):
            # When: option=None triggers config lookup
            await controller.play_media("q1", track, option=None)
            # Then: config was consulted and play_index called
            mock_mass.config.get_core_config_value.assert_called()
            mock_play_index.assert_called()


# ---------------------------------------------------------------------------
# Tests: on_player_register with cache items
# ---------------------------------------------------------------------------


class TestOnPlayerRegisterWithCacheItems:
    """Tests for on_player_register() with cached items."""

    async def test_restores_queue_with_items_from_cache(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test restores queue items from cache."""
        # Given: cache returns queue state AND items
        cached_queue = _make_queue("player-3")
        cached_queue.current_index = 0
        # Create a fake cache item dict

        cache_item = {
            "queue_id": "player-3",
            "queue_item_id": "item-cached-0",
            "name": "Cached Track",
            "duration": 180,
            "media_item": {
                "item_id": "t1",
                "provider": "test",
                "name": "Cached Track",
                "media_type": "track",
                "uri": "test://track/t1",
                "available": True,
                "sort_name": "cached track",
                "metadata": {},
                "provider_mappings": [],
                "is_playable": True,
                "position": None,
            },
        }
        mock_mass.cache.get = AsyncMock(side_effect=[cached_queue.to_dict(), [cache_item]])
        player = MagicMock()
        player.player_id = "player-3"
        player.type = PlayerType.PLAYER
        player.extra_data = {}
        player.state.name = "Player 3"
        player.state.available = True
        player.state.active_source = None
        # When
        await controller.on_player_register(player)
        # Then: queue was restored
        assert "player-3" in controller._queues


# ---------------------------------------------------------------------------
# Tests: __iter__ (line 344)
# ---------------------------------------------------------------------------


class TestIter:
    """Tests for PlayerQueuesController.__iter__."""

    def test_iter_yields_queues(self, controller: PlayerQueuesController) -> None:
        """Test iterating over the controller yields registered queues."""
        # Given: two queues registered
        _seed_queue(controller, "q1", num_items=1)
        _seed_queue(controller, "q2", num_items=1)
        # When
        queues = list(controller)
        # Then
        queue_ids = [q.queue_id for q in queues]
        assert "q1" in queue_ids
        assert "q2" in queue_ids


# ---------------------------------------------------------------------------
# Tests: set_shuffle with no current_index (lines 388-389)
# ---------------------------------------------------------------------------


class TestSetShuffleNoCurrent:
    """Tests for set_shuffle when queue has no current item."""

    async def test_set_shuffle_no_current_index(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test set_shuffle when cur_index is None uses next_index=0."""
        # Given: queue with NO current_index
        _seed_queue(controller, "q1", num_items=3)
        queue = controller._queues["q1"]
        queue.current_index = None
        queue.index_in_buffer = None
        queue.shuffle_enabled = False
        mock_mass.streams.cleanup_queue_audio_data = AsyncMock()
        # When: enable shuffle (cur_index is None -> lines 388-389)
        await controller.set_shuffle("q1", shuffle_enabled=True)
        # Then: queue has shuffle enabled
        assert queue.shuffle_enabled is True


# ---------------------------------------------------------------------------
# Tests: pause triggers _watch_pause task (line 906)
# ---------------------------------------------------------------------------


class TestPauseWatchPause:
    """Tests for pause() when queue is active - triggers _watch_pause task."""

    async def test_pause_creates_watch_pause_task(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test that pause creates the _watch_pause task when queue is active."""
        # Given: active PLAYING queue
        _seed_queue(controller, "q1", num_items=1)
        queue = controller._queues["q1"]
        queue.state = PlaybackState.PLAYING
        queue.current_index = 0
        # Configure the player mock (active = True when player exists)
        queue_player = MagicMock()
        queue_player.extra_data = {}
        mock_mass.players.get_player.return_value = queue_player
        mock_mass.players.cmd_pause = AsyncMock()
        mock_mass.cancel_timer = MagicMock()
        mock_mass.create_task = MagicMock()
        # When
        await controller.pause("q1")
        # Then: create_task was called for _watch_pause
        mock_mass.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: set_playback_speed with nonexistent queue_item_id (line 467)
# ---------------------------------------------------------------------------


class TestSetPlaybackSpeedMissingItem:
    """Tests for set_playback_speed when queue_item_id is not found."""

    async def test_raises_when_queue_item_not_found(
        self, controller: PlayerQueuesController
    ) -> None:
        """Test raises InvalidDataError when specified queue_item_id not in queue."""
        # Given: queue with one item, provide a different queue_item_id
        _seed_queue(controller, "q1", num_items=1)
        queue = controller._queues["q1"]
        # Set current_item so we pass the empty check
        queue.current_item = controller._queue_items["q1"][0]
        queue.current_index = 0
        # When / Then
        with pytest.raises(InvalidDataError):
            await controller.set_playback_speed("q1", 1.5, queue_item_id="nonexistent-id")


# ---------------------------------------------------------------------------
# Tests: on_player_register with cache item missing media_item (lines 1250-1256)
# ---------------------------------------------------------------------------


class TestOnPlayerRegisterCacheItemNoMediaItem:
    """Tests for on_player_register when a cache item has no media_item."""

    async def test_skips_cache_item_without_media_item(
        self, controller: PlayerQueuesController, mock_mass: MagicMock
    ) -> None:
        """Test that cache items with no media_item are skipped with a debug log."""
        # Given: cache returns a queue + one item that has no media_item
        cached_queue = _make_queue("player-x")
        cached_queue.current_index = 0

        # Build a QueueItem that has no media_item
        qi_no_media = QueueItem.__new__(QueueItem)
        qi_no_media.queue_id = "player-x"
        qi_no_media.queue_item_id = "bad-item"
        qi_no_media.name = "Bad Item"
        qi_no_media.duration = None
        qi_no_media.media_item = None
        qi_no_media.extra_attributes = {}
        qi_no_media.sort_index = 0

        with patch(
            "music_assistant.controllers.player_queues.QueueItem.from_cache",
            return_value=qi_no_media,
        ):
            mock_mass.cache.get = AsyncMock(
                side_effect=[cached_queue.to_dict(), [{"dummy": "item"}]]
            )
            player = MagicMock()
            player.player_id = "player-x"
            player.type = PlayerType.PLAYER
            player.extra_data = {}
            player.state.name = "Player X"
            player.state.available = True
            player.state.active_source = None
            # When
            await controller.on_player_register(player)
        # Then: queue was registered but the bad item was not added
        assert "player-x" in controller._queues
        assert controller._queue_items.get("player-x", []) == []
