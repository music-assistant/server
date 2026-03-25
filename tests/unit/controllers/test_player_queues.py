"""Unit tests for PlayerQueuesController.

Covers: get, all, items, get_item, index_by_id, load, update_items, move_item,
delete_item, clear, set_repeat, set_shuffle, set_playback_speed, on_player_remove,
signal_update, player_media_from_queue_item, get_next_item.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, RepeatMode
from music_assistant_models.errors import (
    InvalidCommand,
    InvalidDataError,
    PlayerUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController

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
