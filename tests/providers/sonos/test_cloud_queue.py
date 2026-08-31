"""Tests for the Sonos cloud queue window served to the speakers."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia
from music_assistant_models.queue_item import QueueItem

from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider, _window_size

QUEUE_ID = "party_queue"


def _make_queue_item(item_id: str) -> QueueItem:
    """Build a minimal playable queue item."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=180,
    )


class _FakeQueues:
    """The slice of the player-queues controller the cloud queue reads."""

    def __init__(self, items: list[QueueItem], current_index: int = 0) -> None:
        self.items = items
        self.queue = MagicMock()
        self.queue.current_index = current_index
        self.queue.index_in_buffer = current_index

    def get(self, queue_id: str) -> MagicMock | None:
        """Return the queue for the given id."""
        return self.queue if queue_id == QUEUE_ID else None

    def index_by_id(self, queue_id: str, item_id: str) -> int | None:
        """Return the index of the item with the given id."""
        return next((i for i, x in enumerate(self.items) if x.queue_item_id == item_id), None)

    def get_item(self, queue_id: str, index: int) -> QueueItem | None:
        """Return the item at the given index."""
        return self.items[index] if 0 <= index < len(self.items) else None

    def get_next_item(self, queue_id: str, index_or_id: int | str) -> QueueItem | None:
        """Return the item that plays after the given index or item id."""
        index = (
            index_or_id
            if isinstance(index_or_id, int)
            else self.index_by_id(queue_id, index_or_id) or 0
        )
        return self.get_item(queue_id, index + 1)

    async def player_media_from_queue_item(self, queue_item: QueueItem) -> PlayerMedia:
        """Return the media for the given queue item."""
        return PlayerMedia(
            uri=queue_item.uri,
            media_type=MediaType.TRACK,
            title=queue_item.name,
            queue_item_id=queue_item.queue_item_id,
            source_id=QUEUE_ID,
        )


def _make_player(items: list[QueueItem], current_index: int = 0) -> tuple[SonosPlayer, _FakeQueues]:
    """Create a SonosPlayer serving a cloud queue for the given items."""
    queues = _FakeQueues(items, current_index)
    mass = MagicMock()
    mass.player_queues = queues
    mass.streams.resolve_stream_url = AsyncMock(side_effect=lambda _player_id, media: media.uri)
    player = SonosPlayer.__new__(SonosPlayer)
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.cloud_queue")
    player._player_id = "sonos_player"
    player.connected = True
    player.cloud_queue_id = QUEUE_ID
    player.cloud_queue_version = 1.0
    player._announcement_media = None
    return player, queues


async def test_window_is_centered_on_the_requested_item() -> None:
    """Test the speaker gets the window around the item it asked about."""
    items = [_make_queue_item(f"track{i}") for i in range(10)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track5", previous_size=2, upcoming_size=2)

    assert [x.queue_item_id for x in window.items] == [
        "track3",
        "track4",
        "track5",
        "track6",
        "track7",
    ]
    assert window.includes_beginning is False
    assert window.includes_end is False


async def test_window_falls_back_to_the_playing_item() -> None:
    """Test a request without an item id is centered on what the queue is playing."""
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, _ = _make_player(items, current_index=1)

    window = await player.build_cloud_queue_window(None, previous_size=4, upcoming_size=1)

    assert [x.queue_item_id for x in window.items] == ["track0", "track1", "track2"]
    assert window.includes_beginning is True


async def test_window_flags_the_end_of_the_queue() -> None:
    """Test the end-of-queue flag is set once the window reaches the last item."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track0", previous_size=4, upcoming_size=5)

    assert [x.queue_item_id for x in window.items] == ["track0", "track1", "track2"]
    assert window.includes_beginning is True
    assert window.includes_end is True


async def test_window_serves_an_item_added_after_the_last_enqueue() -> None:
    """Test a track added mid-playback is served without waiting for the next enqueue."""
    items = [_make_queue_item(f"track{i}") for i in range(4)]
    player, queues = _make_player(items)
    # the speaker has already loaded the next track, which is what stops the queue
    # controller from announcing further changes
    queues.queue.index_in_buffer = 1

    before = await player.build_cloud_queue_window("track0", previous_size=4, upcoming_size=5)
    assert "guest" not in [x.queue_item_id for x in before.items]

    # a party guest adds a track behind the one the speaker already buffered
    queues.items.insert(2, _make_queue_item("guest"))

    after = await player.build_cloud_queue_window("track0", previous_size=4, upcoming_size=5)
    assert [x.queue_item_id for x in after.items] == [
        "track0",
        "track1",
        "guest",
        "track2",
        "track3",
    ]


async def test_unavailable_items_are_left_out() -> None:
    """Test an item that cannot be played is not offered to the speaker."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    items[0].available = False
    player, _ = _make_player(items, current_index=1)

    window = await player.build_cloud_queue_window("track1", previous_size=4, upcoming_size=1)

    assert [x.queue_item_id for x in window.items] == ["track1", "track2"]


async def test_announcement_is_served_as_a_single_item_queue() -> None:
    """Test an announcement is the only item in the window while it plays."""
    player, _ = _make_player([_make_queue_item("track0")])
    player._announcement_media = PlayerMedia(
        uri="http://announcement", media_type=MediaType.ANNOUNCEMENT, queue_item_id="announcement"
    )

    window = await player.build_cloud_queue_window("track0", previous_size=4, upcoming_size=5)

    assert [x.queue_item_id for x in window.items] == ["announcement"]
    assert window.includes_beginning is True
    assert window.includes_end is True


async def test_window_is_empty_without_a_queue() -> None:
    """Test a speaker that has no MA queue loaded serves nothing."""
    player, _ = _make_player([_make_queue_item("track0")])
    player.cloud_queue_id = None

    window = await player.build_cloud_queue_window(None, previous_size=4, upcoming_size=5)

    assert window.items == []


async def test_refresh_bumps_the_version_and_signals_the_speaker() -> None:
    """Test a refresh tells the speaker to re-read and invalidates its cached version."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.group.active_session_id = "session1"
    client.api.playback_session.refresh_cloud_queue = AsyncMock()
    player.client = client
    version_before = player.cloud_queue_version

    await player.refresh_cloud_queue()

    assert player.cloud_queue_version > version_before
    client.api.playback_session.refresh_cloud_queue.assert_awaited_once_with("session1")


async def test_refresh_without_a_session_only_bumps_the_version() -> None:
    """Test a speaker with no cloud queue loaded is not sent a refresh."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.group.active_session_id = None
    client.api.playback_session.refresh_cloud_queue = AsyncMock()
    player.client = client
    version_before = player.cloud_queue_version

    await player.refresh_cloud_queue()

    assert player.cloud_queue_version > version_before
    client.api.playback_session.refresh_cloud_queue.assert_not_awaited()


def test_queue_change_only_reaches_the_speakers_playing_it() -> None:
    """Test a queue edit is signalled to the speakers serving that queue."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.sonos.cloud_queue")
    playing, other = MagicMock(spec=SonosPlayer), MagicMock(spec=SonosPlayer)
    playing.player_id = "playing"
    playing.cloud_queue_id = QUEUE_ID
    other.player_id = "other"
    other.cloud_queue_id = "another_queue"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(provider), "players", property(lambda _self: [playing, other]))
        provider._handle_queue_items_updated(MagicMock(object_id=QUEUE_ID))

    assert provider.mass.call_later.call_count == 1
    assert provider.mass.call_later.call_args.args[1] == playing.refresh_cloud_queue


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("10", 10),
        ("0", 0),
        ("", 5),
        (None, 5),
        ("not a number", 5),
        ("-3", 0),
        ("1000", 25),
    ],
)
def test_window_sizes_are_clamped(requested: str | None, expected: int) -> None:
    """Test a size the speaker asks for is clamped to what we are willing to serve."""
    assert _window_size(requested, 5) == expected
