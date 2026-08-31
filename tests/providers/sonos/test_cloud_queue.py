"""Tests for the Sonos cloud queue window served to the speakers."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.exceptions import FailedCommand
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.player import PlayerMedia
from music_assistant_models.queue_item import QueueItem

from music_assistant.providers.sonos.player import SonosPlayer, SonosQueueWindow
from music_assistant.providers.sonos.provider import (
    SonosPlayerProvider,
    _refresh_task_id,
    _requested_max,
)

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
        if isinstance(index_or_id, int):
            index = index_or_id
        elif (found := self.index_by_id(queue_id, index_or_id)) is None:
            return None
        else:
            index = found
        # the real controller walks past items it cannot play
        for candidate in self.items[index + 1 :]:
            if candidate.available:
                return candidate
        return None

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


async def test_window_is_the_requested_item_and_the_one_after_it() -> None:
    """Test only the item asked about and its neighbours are served, however long the queue."""
    items = [_make_queue_item(f"track{i}") for i in range(10)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track5")

    # a deeper window would let the speaker play several tracks out of a cache we cannot
    # update; this way it has to ask again for every one
    assert [x.queue_item_id for x in window.items] == ["track4", "track5", "track6"]
    assert window.includes_beginning is False
    assert window.includes_end is False


@pytest.mark.parametrize("item_id", [None, ""], ids=["omitted", "empty"])
async def test_window_without_an_item_id_starts_at_the_queue_head(item_id: str | None) -> None:
    """Test an omitted or empty item id asks for the start of the queue, as Sonos specifies."""
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, _ = _make_player(items, current_index=3)

    window = await player.build_cloud_queue_window(item_id)

    assert [x.queue_item_id for x in window.items] == ["track0", "track1"]
    assert window.includes_beginning is True


async def test_window_for_an_unknown_item_falls_back_to_the_playing_one() -> None:
    """Test an item id the queue no longer holds is answered around the playing item."""
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, queues = _make_player(items, current_index=1)
    # with crossfade the buffered index runs an item ahead of what is playing
    queues.queue.index_in_buffer = 2

    window = await player.build_cloud_queue_window("gone")

    assert [x.queue_item_id for x in window.items] == ["track0", "track1", "track2"]


async def test_window_flags_the_end_of_the_queue() -> None:
    """Test the end-of-queue flag is set once the window reaches the last item."""
    items = [_make_queue_item(f"track{i}") for i in range(2)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track0")

    assert [x.queue_item_id for x in window.items] == ["track0", "track1"]
    assert window.includes_beginning is True
    assert window.includes_end is True


async def test_window_serves_an_item_added_after_the_last_enqueue() -> None:
    """Test a track added mid-playback is served as the next one, with no enqueue in between."""
    items = [_make_queue_item(f"track{i}") for i in range(4)]
    player, queues = _make_player(items)
    # the speaker has already loaded the next track, which is what stops the queue
    # controller from announcing any further change
    queues.queue.index_in_buffer = 1

    assert [x.queue_item_id for x in (await player.build_cloud_queue_window("track0")).items] == [
        "track0",
        "track1",
    ]

    # a party guest adds a track behind the one the speaker already buffered
    queues.items.insert(2, _make_queue_item("guest"))

    # the speaker comes back when that buffered track starts, and is handed the new one
    window = await player.build_cloud_queue_window("track1")

    assert [x.queue_item_id for x in window.items] == ["track0", "track1", "guest"]


async def test_unavailable_items_are_left_out() -> None:
    """Test an item that cannot be played is not offered to the speaker."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    items[0].available = False
    player, _ = _make_player(items, current_index=1)

    window = await player.build_cloud_queue_window("track1")

    assert [x.queue_item_id for x in window.items] == ["track1", "track2"]


async def test_announcement_is_served_as_a_single_item_queue() -> None:
    """Test an announcement is the only item in the window while it plays."""
    player, _ = _make_player([_make_queue_item("track0")])
    player._announcement_media = PlayerMedia(
        uri="http://announcement", media_type=MediaType.ANNOUNCEMENT, queue_item_id="announcement"
    )

    window = await player.build_cloud_queue_window("track0")

    assert [x.queue_item_id for x in window.items] == ["announcement"]
    assert window.includes_beginning is True
    assert window.includes_end is True


async def test_window_is_empty_without_a_queue() -> None:
    """Test a speaker with no MA queue loaded is told the queue is over, not just empty."""
    player, _ = _make_player([_make_queue_item("track0")])
    player.cloud_queue_id = None

    window = await player.build_cloud_queue_window(None)

    assert window.items == []
    # both ends flagged, or the speaker keeps what it cached and polls on
    assert window.includes_beginning is True
    assert window.includes_end is True


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


async def test_stop_forgets_the_cloud_queue() -> None:
    """Test a stopped speaker is no longer signalled about that queue."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    client.player.group.stop = AsyncMock()
    player.client = client
    player.mark_stop_called = MagicMock()  # type: ignore[misc, method-assign]
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    player._announcement_media = PlayerMedia(
        uri="http://announcement", media_type=MediaType.ANNOUNCEMENT
    )

    await player.stop()

    assert player.cloud_queue_id is None
    assert player._announcement_media is None


async def test_refresh_survives_a_session_the_speaker_forgot() -> None:
    """Test a rejected refresh is not an error: the next read carries the change anyway."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.group.active_session_id = "stale_session"
    client.api.playback_session.refresh_cloud_queue = AsyncMock(
        side_effect=FailedCommand("no cloud queue loaded")
    )
    player.client = client

    await player.refresh_cloud_queue()

    client.api.playback_session.refresh_cloud_queue.assert_awaited_once()


def _make_provider() -> SonosPlayerProvider:
    """Create a bare provider for the cloud-queue request handlers."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.sonos.cloud_queue")
    provider._pending_refresh_tasks = set()
    return provider


async def test_itemwindow_passes_the_speakers_request_through() -> None:
    """Test the sizes and centre the speaker asks for reach the window builder."""
    player = MagicMock(spec=SonosPlayer)
    player.cloud_queue_version = 12.5
    player.build_cloud_queue_window = AsyncMock(
        return_value=SonosQueueWindow(includes_beginning=True, includes_end=False)
    )
    provider = _make_provider()
    request = MagicMock()
    request.query = {
        "itemId": "track7",
        "previousWindowSize": "9",
        "upcomingWindowSize": "10",
        "contextVersion": "3",
    }

    response = await provider._handle_sonos_queue_itemwindow(player, request)

    player.build_cloud_queue_window.assert_awaited_once_with(
        "track7", max_previous=9, max_upcoming=10
    )
    body = json.loads(response.text or "{}")
    assert body["queueVersion"] == "12.5"
    assert body["contextVersion"] == "3"
    assert body["includesBeginningOfQueue"] is True


async def test_itemwindow_reports_end_of_queue_when_it_cannot_be_described() -> None:
    """Test a queue that went away answers with an empty window instead of an error."""
    player = MagicMock(spec=SonosPlayer)
    player.display_name = "Kantoor"
    player.cloud_queue_version = 1.0
    player.build_cloud_queue_window = AsyncMock(side_effect=InvalidDataError("no session"))
    provider = _make_provider()
    request = MagicMock()
    request.query = {}

    response = await provider._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert body["items"] == []
    assert body["includesEndOfQueue"] is True


@pytest.mark.parametrize(
    ("requested", "expected_upcoming"),
    [("10", ["track1"]), ("1", ["track1"]), ("0", []), ("", ["track1"]), (None, ["track1"])],
    ids=["ten", "one", "zero", "unreadable", "absent"],
)
async def test_upcoming_is_capped_by_what_the_speaker_allows(
    requested: str | None, expected_upcoming: list[str]
) -> None:
    """Test we never serve more than the speaker's maximum, though we usually serve fewer."""
    items = [_make_queue_item(f"track{i}") for i in range(4)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track0", max_upcoming=_requested_max(requested))

    assert [x.queue_item_id for x in window.items] == ["track0", *expected_upcoming]


async def test_play_media_keeps_describing_the_queue_until_the_new_one_is_loaded() -> None:
    """Test the still-playing queue is not blanked while its replacement is being loaded."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    loaded_with_queue_id: list[str | None] = []

    async def _play_cloud_queue(*_args: object, **_kwargs: object) -> None:
        # by the time the speaker is told to load, the id must already point at the new queue
        loaded_with_queue_id.append(player.cloud_queue_id)

    client.player.group.play_cloud_queue = _play_cloud_queue
    player.client = client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await player.play_media(
            PlayerMedia(
                uri="library://track/1",
                media_type=MediaType.TRACK,
                source_id="new_queue",
                queue_item_id="item1",
            )
        )

    assert loaded_with_queue_id == ["new_queue"]
    assert player.cloud_queue_id == "new_queue"


async def test_a_failed_load_leaves_no_cloud_queue_described() -> None:
    """Test a load the speaker never accepted is not described as a queue afterwards."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    client.player.group.play_cloud_queue = AsyncMock(side_effect=FailedCommand("no can do"))
    player.client = client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        with pytest.raises(FailedCommand):
            await player.play_media(
                PlayerMedia(
                    uri="library://track/1",
                    media_type=MediaType.TRACK,
                    source_id="new_queue",
                    queue_item_id="item1",
                )
            )

    # the session was reset before the load, so claiming either queue would be a lie
    assert player.cloud_queue_id is None


def test_queue_change_only_reaches_the_speakers_playing_it() -> None:
    """Test a queue edit is signalled to the speakers serving that queue."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.sonos.cloud_queue")
    provider._pending_refresh_tasks = set()
    playing, other = MagicMock(spec=SonosPlayer), MagicMock(spec=SonosPlayer)
    playing.player_id = "playing"
    playing.cloud_queue_id = QUEUE_ID
    other.player_id = "other"
    other.cloud_queue_id = "another_queue"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(provider), "players", property(lambda _self: [playing, other]))
        provider._handle_queue_items_updated(MagicMock(object_id=QUEUE_ID))

    # the version must be invalidated before the (debounced) command goes out, or a window
    # served in between carries a version the speaker reads as current
    playing.bump_cloud_queue_version.assert_called_once_with()
    other.bump_cloud_queue_version.assert_not_called()
    assert provider.mass.call_later.call_count == 1
    assert provider.mass.call_later.call_args.args[1] == playing.refresh_cloud_queue
    # the id must be per speaker, or one speaker's refresh cancels another's
    assert provider.mass.call_later.call_args.kwargs["task_id"] == _refresh_task_id("playing")
    assert provider._pending_refresh_tasks == {_refresh_task_id("playing")}
