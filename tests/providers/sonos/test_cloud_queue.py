"""Tests for the Sonos cloud queue window served to the speakers."""

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from types import MethodType
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.exceptions import FailedCommand
from music_assistant_models.enums import CrossfadeMode, MediaType, PlaybackState, RepeatMode
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams import StreamsController
from music_assistant.models.player import Player
from music_assistant.providers.sonos.cloud_queue import SonosCloudQueue, _requested_max
from music_assistant.providers.sonos.const import PLAYBACK_STATE_MAP
from music_assistant.providers.sonos.player import SonosPlayer, SonosQueueWindow
from music_assistant.providers.sonos.provider import SonosPlayerProvider, _refresh_task_id

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

    def get_item(self, queue_id: str, index_or_id: int | str) -> QueueItem | None:
        """Return the item at the given index, or with the given id."""
        if isinstance(index_or_id, str):
            return next((x for x in self.items if x.queue_item_id == index_or_id), None)
        return self.items[index_or_id] if 0 <= index_or_id < len(self.items) else None

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
    mass.streams.resolve_stream_url = AsyncMock(
        side_effect=lambda _player_id, media, **_kwargs: media.uri
    )
    # the streams controller's own decision on what a player gets as a file
    mass.streams.mass = mass
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.streams.audio.fade_into_pending.return_value = False
    mass.streams.serves_as_file = MethodType(StreamsController.serves_as_file, mass.streams)
    player = SonosPlayer.__new__(SonosPlayer)
    # a speaker as it comes: every setting at its default
    player._config = MagicMock()
    player._config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.cloud_queue")
    player._protocol_id = "sonos_player"
    player._player = None
    player.connected = True
    player.cloud_queue_id = QUEUE_ID
    player._loaded_session_id = "session"
    player.cloud_queue_version = 1.0
    player.cloud_queue_item_generation = 0
    player._window_item_ids = {}
    player._served_bindings = {}
    player._announcement_media = None
    player._woken_from_sleep = False
    player._flow_queue_id = None
    player._queue_session_id = None
    player._evicted_cloud_queue = None
    return player, queues


def _set_output_codec(player: SonosPlayer, codec: str) -> None:
    """Set the codec the speaker gets its streams in."""
    cast("MagicMock", player.config).get_value = MagicMock(
        side_effect=lambda key, default=None: codec if key == CONF_OUTPUT_CODEC else default
    )


def _make_media_queue_item(item_id: str, media_type: MediaType) -> QueueItem:
    """Build a queue item that carries media of the given type."""
    item = _make_queue_item(item_id)
    item.media_item = MagicMock(media_type=media_type, uri=f"library://{media_type}/{item_id}")
    return item


def _loads_into_a_session(
    group: MagicMock,
    on_load: Callable[..., Awaitable[None]] | None = None,
    on_setup: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Let a mocked group load a cloud queue into a session of its own, as the speaker does."""

    async def _play_cloud_queue(
        *args: object, on_session: Callable[[str], None] | None = None, **kwargs: object
    ) -> None:
        if on_setup is not None:
            await on_setup()
        if on_session is not None:
            on_session("loaded-session")
        if on_load is not None:
            await on_load(*args, **kwargs)
        group.active_session_id = "loaded-session"

    group.play_cloud_queue = AsyncMock(side_effect=_play_cloud_queue)


def _make_player_on_real_queues(count: int, repeat_mode: RepeatMode) -> SonosPlayer:
    """Create a SonosPlayer whose queue of `count` tracks is held by the real controller."""
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Party", available=True, items=count
    )
    queue.repeat_mode = repeat_mode
    queue.current_index = 0
    queue.index_in_buffer = 0
    data = PlayerQueueData(queue=queue)
    data.session_id = "session"
    data.items = [_make_queue_item(f"track{i}") for i in range(count)]
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.logger = logging.getLogger("test.sonos.player_queues")
    queues._queue_data = {QUEUE_ID: data}
    player, _ = _make_player([])
    player.mass.player_queues = queues
    return player


async def test_window_is_the_requested_item_and_the_tracks_after_it() -> None:
    """Test the window starts one before the item asked about and runs ahead from there."""
    items = [_make_queue_item(f"track{i}") for i in range(20)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track5")

    # the speaker only asks for a new window once it runs out, so it is handed a deep one
    assert [x.queue_item_id for x in window.items] == [f"track{i}" for i in range(4, 16)]
    assert window.includes_beginning is False
    assert window.includes_end is False


@pytest.mark.parametrize("item_id", [None, ""], ids=["omitted", "empty"])
async def test_window_without_an_item_id_starts_at_the_queue_head(item_id: str | None) -> None:
    """Test an omitted or empty item id asks for the start of the queue, as Sonos specifies."""
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, _ = _make_player(items, current_index=3)

    window = await player.build_cloud_queue_window(item_id)

    assert [x.queue_item_id for x in window.items] == [f"track{i}" for i in range(5)]
    assert window.includes_beginning is True


async def test_window_for_an_unknown_item_falls_back_to_the_playing_one() -> None:
    """Test an item id the queue no longer holds is answered around the playing item."""
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, queues = _make_player(items, current_index=1)
    # with crossfade the buffered index runs an item ahead of what is playing
    queues.queue.index_in_buffer = 2

    window = await player.build_cloud_queue_window("gone")

    # centred on the playing track1 rather than the buffered track2, so it opens on track0
    assert [x.queue_item_id for x in window.items] == [f"track{i}" for i in range(5)]


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
        "track2",
        "track3",
    ]

    # a party guest adds a track behind the one the speaker already buffered
    queues.items.insert(2, _make_queue_item("guest"))

    # the window is built from the live queue, so the next time the speaker asks it is
    # handed the guest right after the buffered track
    window = await player.build_cloud_queue_window("track1")

    assert [x.queue_item_id for x in window.items] == [
        "track0",
        "track1",
        "guest",
        "track2",
        "track3",
    ]


async def test_window_items_are_addressed_by_queue_position() -> None:
    """Test the speaker gets position URLs, so a cached slot plays whatever the queue holds there."""
    items = [_make_queue_item(f"track{i}") for i in range(20)]
    player, _ = _make_player(items)
    resolve = cast("AsyncMock", player.mass.streams.resolve_stream_url)

    window = await player.build_cloud_queue_window("track5@0")

    assert [call.kwargs["queue_index"] for call in resolve.await_args_list] == list(range(4, 16))
    # the ids stay those of the items, which is what the speaker reports back
    assert [x.queue_item_id for x in window.items] == [f"track{i}" for i in range(4, 16)]
    assert player._window_item_ids == {i: f"track{i}@0" for i in range(4, 16)}
    assert window.wire_ids == [f"track{i}@0" for i in range(4, 16)]


async def test_a_reported_id_resolves_to_the_item_served_at_its_position() -> None:
    """
    Test an id the speaker reports is read as the track whose audio it fetched for that slot.

    A track added right behind the playing one takes the position the speaker cached the
    next track at: the speaker plays the added track there, under the id it cached.
    """
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    await player.build_cloud_queue_window("track0")
    queue_data.items.insert(1, _make_queue_item("guest"))
    # the stream server served the guest when the speaker fetched position 1
    player.on_queue_position_served(1, "guest")

    assert player.served_item_id("track1@0") == "guest"
    # a position the speaker has not fetched yet names the item it cached there
    assert player.served_item_id("track2@0") == "track2"
    assert player.served_item_id("unknown@0") == "unknown"


async def test_a_binding_survives_the_window_being_rebuilt() -> None:
    """
    Test a report on fetched audio keeps resolving to it after the speaker re-reads the queue.

    The re-read moves the cached id to the position the item now holds, while the audio
    fetched under that id keeps playing.
    """
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    await player.build_cloud_queue_window("track0")
    queue_data.items.insert(1, _make_queue_item("guest"))
    player.on_queue_position_served(1, "guest")

    window = await player.build_cloud_queue_window("track0@0")

    assert [x.queue_item_id for x in window.items][:3] == ["track0", "guest", "track1"]
    # the moved item gets an id of its own, as the speaker holds its old one for the guest
    assert window.wire_ids[:3] == ["track0@0", "guest@0", "track1@0~2"]
    assert player.served_item_id("track1@0") == "guest"
    # fetching the moved item under its own id leaves the guest's binding alone
    player.on_queue_position_served(2, "track1")
    assert player.served_item_id("track1@0") == "guest"
    assert player.served_item_id("track1@0~2") == "track1"


async def test_a_window_is_centred_on_the_position_the_speaker_plays() -> None:
    """
    Test a window asked around a cached id centres on the position fetched under that id.

    The speaker names what it cached for the position it plays; tracks added before that
    position move the item it names, but not the speaker.
    """
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    await player.build_cloud_queue_window("track0")
    for guest in ("guest1", "guest2"):
        queue_data.items.insert(1, _make_queue_item(guest))
    # the speaker fetched position 1 under the id it cached there, track1
    player.on_queue_position_served(1, "guest2")

    window = await player.build_cloud_queue_window("track1@0")

    assert [x.queue_item_id for x in window.items] == [
        "track0",
        "guest2",
        "guest1",
        "track1",
        "track2",
        "track3",
    ]


async def test_a_window_asked_around_a_lost_id_holds_it_at_the_playing_position() -> None:
    """
    Test a window asked around an id the queue lost serves the playing track under that id.

    The speaker discards a window without the id it asked about, and stops at the end of
    the window it holds.
    """
    items = [_make_queue_item(f"track{i}") for i in range(5)]
    player, _ = _make_player(items, current_index=1)

    window = await player.build_cloud_queue_window("gone@0")

    assert window.wire_ids == ["track0@0", "gone@0", "track2@0", "track3@0", "track4@0"]
    assert window.items[1].title == "track1"
    assert player._window_item_ids[1] == "gone@0"


async def test_a_window_asked_around_a_bound_id_holds_it_at_its_position() -> None:
    """
    Test a window asked around a fetched id keeps it at its position, whatever sits there now.

    The tracks after the playing one were replaced before the speaker fetched position 1
    under the id it cached there.
    """
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    await player.build_cloud_queue_window("track0@0")
    queue_data.items[1:] = [_make_queue_item(f"new{i}") for i in range(1, 4)]
    player.on_queue_position_served(1, "new1")

    window = await player.build_cloud_queue_window("track1@0")

    assert [x.queue_item_id for x in window.items] == ["track0", "new1", "new2", "new3"]
    assert window.wire_ids == ["track0@0", "track1@0", "new2@0", "new3@0"]
    assert player._window_item_ids[1] == "track1@0"
    assert player.served_item_id("track1@0") == "new1"


async def test_a_report_on_the_asked_id_resolves_to_the_item_served_there() -> None:
    """Test the speaker fetching the position it asked about binds the id it asked about."""
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    player.mass.player_queues._queue_data[QUEUE_ID].queue.current_index = 1
    await player.build_cloud_queue_window("gone@0")

    player.on_queue_position_served(1, "track1")

    assert player.served_item_id("gone@0") == "track1"


async def test_a_repeated_position_keeps_the_asked_id_for_its_own_round() -> None:
    """Test the position asked about keeps the asked id, and its repeat gets an id of its own."""
    player = _make_player_on_real_queues(3, RepeatMode.ONE)

    window = await player.build_cloud_queue_window("gone@0")

    assert window.wire_ids == ["gone@0", "track0@0~r1"]
    assert player._window_item_ids == {0: "track0@0~r1"}


async def test_each_round_of_a_repeating_track_gets_an_id_of_its_own() -> None:
    """
    Test every round of a track on repeat is served under an id of its own.

    Sonos wants a distinct id for every instance of a track in its queue: a round under the
    id of the round before is not fetched cleanly, and the speaker cuts out.
    """
    player = _make_player_on_real_queues(1, RepeatMode.ONE)

    first = await player.build_cloud_queue_window("track0@0")
    # the speaker moved on to the repeat and asks around it
    second = await player.build_cloud_queue_window("track0@0~r1")

    assert first.wire_ids == ["track0@0", "track0@0~r1"]
    assert second.wire_ids == ["track0@0~r1", "track0@0~r2"]


async def test_the_rounds_of_repeat_all_agree_from_window_to_window() -> None:
    """Test a window asked around a later round names the rounds as the window before did."""
    player = _make_player_on_real_queues(3, RepeatMode.ALL)

    before = await player.build_cloud_queue_window("track2@0")
    # the speaker moved on to the first track of the next round
    after = await player.build_cloud_queue_window("track0@0~r1")

    assert before.wire_ids[:5] == [
        "track1@0",
        "track2@0",
        "track0@0~r1",
        "track1@0~r1",
        "track2@0~r1",
    ]
    assert after.wire_ids[:4] == ["track0@0~r1", "track1@0~r1", "track2@0~r1", "track0@0~r2"]


async def test_a_fetch_on_repeat_all_binds_the_round_the_speaker_reaches_first() -> None:
    """
    Test a fetch at a position a short queue on repeat lists twice binds its nearest round.

    A track added behind the playing one takes the position the speaker cached the next
    track at, and goes out under the id of that track's first round.
    """
    player = _make_player_on_real_queues(4, RepeatMode.ALL)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    queue_data.queue.current_index = 1
    await player.build_cloud_queue_window("track1@0")
    queue_data.items.insert(2, _make_queue_item("guest"))
    player.on_queue_position_served(2, "guest")

    assert player.served_item_id("track2@0") == "guest"
    # the speaker has not fetched the round after it yet
    assert player.served_item_id("track2@0~r1") == "track2"


async def test_a_fetch_on_repeat_one_binds_the_repeat() -> None:
    """
    Test a fetch at the position of a track on repeat binds the round after the playing one.

    The speaker fetched the round it plays already: a track added before it takes its
    position, and goes out under the id of the repeat.
    """
    player = _make_player_on_real_queues(3, RepeatMode.ONE)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    queue_data.queue.current_index = 1
    await player.build_cloud_queue_window("track1@0")
    queue_data.items.insert(1, _make_queue_item("guest"))
    player.on_queue_position_served(1, "guest")

    assert player.served_item_id("track1@0~r1") == "guest"
    assert player.served_item_id("track1@0") == "track1"


def test_an_id_of_a_later_round_names_its_queue_item() -> None:
    """Test a report on a repeat round is read as the queue item of the current load."""
    player, _ = _make_player([_make_queue_item("track0")])

    assert player.from_current_load("track0@0~r2")
    assert player.served_item_id("track0@0~r2") == "track0"


async def test_a_window_asked_around_an_id_of_an_earlier_load_keeps_the_ids_of_this_load() -> None:
    """Test an id from before the current load is not echoed: the speaker moves to the new load."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    player, _ = _make_player(items, current_index=1)
    player.cloud_queue_item_generation = 1

    moved = await player.build_cloud_queue_window("track2@0")
    lost = await player.build_cloud_queue_window("gone@0")

    assert moved.wire_ids == ["track1@1", "track2@1"]
    assert lost.wire_ids == ["track0@1", "track1@1", "track2@1"]


@pytest.mark.parametrize("replaced", [True, False], ids=["replaced queue", "same queue"])
async def test_a_window_asked_ahead_of_a_load_addresses_the_loaded_session(replaced: bool) -> None:
    """
    Test the items of a window belong to the session the speaker loaded, until its next load.

    A started queue is in a new session before the speaker loads it. An item fetched from that
    session ahead of the load can wait for audio, and the speaker acts on no load while it does.

    :param replaced: Whether the started queue replaced the one the speaker plays.
    """
    player = _make_player_on_real_queues(3, RepeatMode.OFF)
    cast("AsyncMock", player.mass.streams.resolve_stream_url).side_effect = (
        lambda _player_id, media, queue_index: f"{media.queue_session_id}/{queue_index}"
    )
    player.cloud_queue_item_generation = 3
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    queue_data.queue.current_index = 1
    if replaced:
        queue_data.items = [_make_queue_item(f"new{i}") for i in range(3)]
    queue_data.session_id = "next-session"

    window = await player.build_cloud_queue_window("track1@3")

    assert window.wire_ids[1] == "track1@3"
    assert [x.uri for x in window.items] == ["session/0", "session/1", "session/2"]


async def test_a_load_addresses_the_session_it_loads() -> None:
    """Test a window holds the items of the new session from the load on, not while it is set up."""
    player = _make_player_on_real_queues(3, RepeatMode.OFF)
    cast("AsyncMock", player.mass.streams.resolve_stream_url).side_effect = (
        lambda _player_id, media, queue_index: f"{media.queue_session_id}/{queue_index}"
    )
    player.mass.player_queues._queue_data[QUEUE_ID].session_id = "next-session"
    client = MagicMock()
    client.player.is_passive = False
    served: list[SonosQueueWindow] = []

    async def _read_window_for_load(*_args: object, item_id: str, **_kwargs: object) -> None:
        served.append(await player.build_cloud_queue_window(item_id))

    async def _read_window_during_setup() -> None:
        served.append(await player.build_cloud_queue_window("track0@0"))

    _loads_into_a_session(client.player.group, _read_window_for_load, _read_window_during_setup)
    player.client = client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await player.play_media(
            PlayerMedia(
                uri="library://track/1",
                media_type=MediaType.TRACK,
                source_id=QUEUE_ID,
                queue_item_id="track0",
                queue_session_id="next-session",
            )
        )

    during_setup, for_load = served
    assert [x.uri for x in during_setup.items] == [f"session/{i}" for i in range(3)]
    assert [x.uri for x in for_load.items] == [f"next-session/{i}" for i in range(3)]


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
    assert window.wire_ids == ["announcement@0"]
    assert window.includes_beginning is True
    assert window.includes_end is True


async def test_an_announcement_window_is_served_through_the_endpoint() -> None:
    """Test the itemWindow endpoint describes a playing announcement as its one item."""
    player, _ = _make_player([_make_queue_item("track0")])
    player._announcement_media = PlayerMedia(
        uri="http://announcement", media_type=MediaType.ANNOUNCEMENT, queue_item_id="announcement"
    )
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {"itemId": "track0@0", "previousWindowSize": "9", "upcomingWindowSize": "10"}

    response = await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert [item["id"] for item in body["items"]] == ["announcement@0"]
    assert body["includesEndOfQueue"] is True


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
    attached_to = player._player = MagicMock(spec=Player)
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    player._announcement_media = PlayerMedia(
        uri="http://announcement", media_type=MediaType.ANNOUNCEMENT
    )

    await player.stop()

    attached_to.mark_stop_called.assert_called_once_with()
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


def _make_cloud_queue() -> SonosCloudQueue:
    """Create a cloud queue backed by a bare provider for the request handlers."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.sonos.cloud_queue")
    provider._pending_refresh_tasks = set()
    return SonosCloudQueue(provider)


async def test_itemwindow_passes_the_speakers_request_through() -> None:
    """Test the sizes and centre the speaker asks for reach the window builder."""
    player = MagicMock(spec=SonosPlayer)
    player.cloud_queue_version = 12.5
    player.cloud_queue_item_generation = 4
    player.bare_item_id = SonosPlayer.bare_item_id
    player.build_cloud_queue_window = AsyncMock(
        return_value=SonosQueueWindow(includes_beginning=True, includes_end=False)
    )
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {
        "itemId": "track7@4",
        "previousWindowSize": "9",
        "upcomingWindowSize": "10",
        "contextVersion": "3",
    }

    response = await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    player.build_cloud_queue_window.assert_awaited_once_with(
        "track7@4", max_previous=9, max_upcoming=10, generation=4
    )
    body = json.loads(response.text or "{}")
    assert body["queueVersion"] == "12.5"
    assert body["contextVersion"] == "3"
    assert body["includesBeginningOfQueue"] is True


@pytest.mark.parametrize(
    ("codec", "policies"),
    [
        ("wav", [{"canSeek": True}, None]),
        ("mp3", [{"canSeek": True}, None]),
        ("flac", [None, None]),
    ],
)
async def test_only_a_track_served_as_a_file_allows_a_seek(
    codec: str, policies: list[dict[str, bool] | None]
) -> None:
    """Test the speaker may seek in a track it gets as a file, and in nothing else it plays."""
    items = [
        _make_media_queue_item("track0", MediaType.TRACK),
        _make_media_queue_item("radio0", MediaType.RADIO),
    ]
    player, _ = _make_player(items)
    _set_output_codec(player, codec)
    request = MagicMock()
    request.query = {"itemId": "track0@0"}

    response = await _make_cloud_queue()._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert [item["id"] for item in body["items"]] == ["track0@0", "radio0@0"]
    assert [item.get("policies") for item in body["items"]] == policies


@pytest.mark.parametrize(
    ("repeat_mode", "policies"),
    [(RepeatMode.OFF, [{"canSeek": True}]), (RepeatMode.ONE, [None])],
)
async def test_a_track_in_a_queue_with_crossfade_allows_a_seek_unless_it_repeats(
    repeat_mode: RepeatMode, policies: list[dict[str, bool] | None]
) -> None:
    """Test a track whose stream carries a mix is a file too, but not one that comes right back."""
    player, queues = _make_player([_make_media_queue_item("track0", MediaType.TRACK)])
    _set_output_codec(player, "wav")
    queues.queue.repeat_mode = repeat_mode
    streams = cast("MagicMock", player.mass.streams)
    streams.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE
    request = MagicMock()
    request.query = {"itemId": "track0@0"}

    response = await _make_cloud_queue()._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert [item.get("policies") for item in body["items"]] == policies


async def test_the_queue_itself_allows_no_seek() -> None:
    """Test the queue keeps a seek off: only an item served as a file allows it for itself."""
    player, _ = _make_player([_make_queue_item("track0")])

    response = await _make_cloud_queue()._handle_sonos_queue_context(player, MagicMock())

    body = json.loads(response.text or "{}")
    assert body["playbackPolicies"]["canSeek"] is False


async def test_itemwindow_reports_end_of_queue_when_it_cannot_be_described() -> None:
    """Test a queue that went away answers with an empty window instead of an error."""
    player = MagicMock(spec=SonosPlayer)
    player.display_name = "Kantoor"
    player.cloud_queue_version = 1.0
    player.cloud_queue_item_generation = 1
    player.build_cloud_queue_window = AsyncMock(side_effect=InvalidDataError("no session"))
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {}

    response = await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert body["items"] == []
    assert body["includesEndOfQueue"] is True


async def test_itemwindow_logs_the_speakers_request_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a support case can read what the speaker asked for from one debug line."""
    player = MagicMock(spec=SonosPlayer)
    player.protocol_id = "RINCON_TEST"
    player.cloud_queue_version = 12.5
    player.cloud_queue_item_generation = 4
    player.bare_item_id = SonosPlayer.bare_item_id
    player.wire_item_id = MagicMock(return_value=None)
    player.build_cloud_queue_window = AsyncMock(
        return_value=SonosQueueWindow(
            items=[PlayerMedia(uri="track1.mp3"), PlayerMedia(uri="track2.mp3")],
            wire_ids=["track1@4", "track2@4"],
            includes_beginning=True,
            includes_end=False,
        )
    )
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {
        "itemId": "track7@4",
        "reason": "queueCompleted",
        "previousWindowSize": "9",
        "upcomingWindowSize": "10",
        "queueVersion": "11.0",
    }

    with caplog.at_level(logging.DEBUG, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    message = caplog.records[0].getMessage()
    assert "RINCON_TEST" in message
    for expected in ("reason=queueCompleted", "itemId=track7@4", "previous=9", "upcoming=10"):
        assert expected in message
    assert "queueVersion=11.0 -> 12.5" in message
    assert "begin=True end=False items=2" in message


async def test_itemwindow_logs_the_end_of_queue_fallback_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the served window is logged even when the queue can no longer be described."""
    player = MagicMock(spec=SonosPlayer)
    player.protocol_id = "RINCON_TEST"
    player.cloud_queue_version = 1.0
    player.cloud_queue_item_generation = 1
    player.build_cloud_queue_window = AsyncMock(side_effect=InvalidDataError("no session"))
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {}

    with caplog.at_level(logging.DEBUG, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    message = caplog.records[0].getMessage()
    assert "begin=True end=True items=0" in message
    assert "no session" in message


async def test_version_logs_the_speakers_poll_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the version poll reports which version the speaker holds and which we serve."""
    player = MagicMock(spec=SonosPlayer)
    player.protocol_id = "RINCON_TEST"
    player.cloud_queue_version = 12.5
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {"contextVersion": "1", "queueVersion": "11.0"}

    with caplog.at_level(logging.DEBUG, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_version(player, request)

    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    message = caplog.records[0].getMessage()
    assert "RINCON_TEST" in message
    assert "queueVersion=11.0 -> 12.5" in message


@pytest.mark.parametrize(
    ("requested", "expected_upcoming"),
    [
        ("10", ["track1", "track2", "track3"]),
        ("1", ["track1"]),
        ("0", []),
        ("", ["track1", "track2", "track3"]),
        (None, ["track1", "track2", "track3"]),
    ],
    ids=["ten", "one", "zero", "unreadable", "absent"],
)
async def test_upcoming_is_capped_by_what_the_speaker_allows(
    requested: str | None, expected_upcoming: list[str]
) -> None:
    """Test we never serve more than the speaker's maximum."""
    items = [_make_queue_item(f"track{i}") for i in range(4)]
    player, _ = _make_player(items)

    window = await player.build_cloud_queue_window("track0", max_upcoming=_requested_max(requested))

    assert [x.queue_item_id for x in window.items] == ["track0", *expected_upcoming]


async def test_a_speaker_gets_as_far_ahead_as_it_asks() -> None:
    """
    A speaker is handed as many upcoming tracks as it asks for, up to our ceiling.

    It only asks for a new window once it runs out, so a window that stops one track
    ahead leaves it reloading at the next boundary.
    """
    items = [_make_queue_item(f"track{i}") for i in range(15)]
    player, _ = _make_player(items)

    asked_for_ten = await player.build_cloud_queue_window("track0", max_upcoming=10)
    asked_for_more = await player.build_cloud_queue_window("track0", max_upcoming=50)

    assert len(asked_for_ten.items) == 11
    assert len(asked_for_more.items) == 11


@pytest.mark.parametrize(
    ("repeat_mode", "count"),
    [(RepeatMode.ONE, 3), (RepeatMode.ALL, 1)],
    ids=["repeat_one", "single_track_on_repeat_all"],
)
async def test_a_track_repeating_itself_is_listed_once_more(
    repeat_mode: RepeatMode, count: int
) -> None:
    """
    A track that repeats itself is listed once more, not for a whole window.

    The speaker is never refused a copy of the track it plays, so a window full of them
    would keep it repeating long after repeat was switched off.
    """
    player = _make_player_on_real_queues(count, repeat_mode)

    window = await player.build_cloud_queue_window("track0@0")

    assert [x.queue_item_id for x in window.items] == ["track0", "track0"]
    assert window.wire_ids == ["track0@0", "track0@0~r1"]
    assert window.includes_end is False


async def test_a_pair_repeating_itself_is_listed_once_round() -> None:
    """
    Test two tracks on repeat all go round once, not for a whole window.

    The wrapped track is also the one before the playing track, which the stale-request
    check lets through for skip-back, so it could not refuse the rest of such a window.
    """
    player = _make_player_on_real_queues(2, RepeatMode.ALL)

    window = await player.build_cloud_queue_window("track0")

    assert [x.queue_item_id for x in window.items] == ["track0", "track1", "track0"]


async def test_a_short_queue_on_repeat_all_still_fills_the_window() -> None:
    """Test repeat all wraps round a short queue, so the speaker does not run dry early."""
    player = _make_player_on_real_queues(3, RepeatMode.ALL)

    window = await player.build_cloud_queue_window("track0@0")

    assert [x.queue_item_id for x in window.items] == [f"track{i % 3}" for i in range(11)]
    # every pass over the queue is a round of its own
    assert window.wire_ids == [
        f"track{i % 3}@0~r{i // 3}" if i >= 3 else f"track{i}@0" for i in range(11)
    ]


async def test_a_successful_load_releases_the_replaced_sessions_streams() -> None:
    """The old session's open responses die once the speaker accepted the new load."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    _loads_into_a_session(client.player.group)
    player.client = client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await player.play_media(
            PlayerMedia(
                uri="library://track/1",
                media_type=MediaType.TRACK,
                source_id=QUEUE_ID,
                queue_item_id="track0",
                queue_session_id="session-2",
            )
        )

    close_stale = cast("MagicMock", player.mass.streams.close_superseded_item_streams)
    close_stale.assert_called_once_with(QUEUE_ID, "session-2")


async def test_a_forced_flow_load_also_releases_the_replaced_sessions_streams() -> None:
    """A flow-mode queue (overlay) streams via play_stream_url; the sweep still runs."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    client.player.group.play_stream_url = AsyncMock()
    player.client = client
    player._provider = MagicMock()
    player._provider.mass = player.mass

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: True))
        await player.play_media(
            PlayerMedia(
                uri="http://ma/flow/abc.flac",
                media_type=MediaType.TRACK,
                source_id=QUEUE_ID,
                queue_item_id="track0",
                queue_session_id="session-2",
            )
        )

    client.player.group.play_stream_url.assert_awaited_once()
    close_stale = cast("MagicMock", player.mass.streams.close_superseded_item_streams)
    close_stale.assert_called_once_with(QUEUE_ID, "session-2")


async def test_a_failed_forced_flow_load_keeps_the_old_sessions_streams_alive() -> None:
    """A flow load the speaker refused must not cut the audio that is still playing."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    client.player.group.play_stream_url = AsyncMock(side_effect=FailedCommand("no can do"))
    player.client = client
    player._provider = MagicMock()
    player._provider.mass = player.mass

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: True))
        with pytest.raises(FailedCommand):
            await player.play_media(
                PlayerMedia(
                    uri="http://ma/flow/abc.flac",
                    media_type=MediaType.TRACK,
                    source_id=QUEUE_ID,
                    queue_item_id="track0",
                    queue_session_id="session-2",
                )
            )

    cast("MagicMock", player.mass.streams.close_superseded_item_streams).assert_not_called()


async def test_a_failed_load_keeps_the_old_sessions_streams_alive() -> None:
    """A load the speaker refused must not cut the audio that is still playing."""
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
                    source_id=QUEUE_ID,
                    queue_item_id="track0",
                    queue_session_id="session-2",
                )
            )

    cast("MagicMock", player.mass.streams.close_superseded_item_streams).assert_not_called()


async def test_a_reload_of_the_same_item_gets_a_fresh_wire_id() -> None:
    """
    A seek reloads the item the speaker is already playing.

    Under its old id the speaker keeps waiting out its stale stream; a fresh id
    per load makes the reload cut over like a track change.
    """
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    loaded_ids: list[str] = []

    async def _play_cloud_queue(*_args: object, item_id: str = "", **_kwargs: object) -> None:
        loaded_ids.append(item_id)

    _loads_into_a_session(client.player.group, _play_cloud_queue)
    player.client = client
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id=QUEUE_ID,
        queue_item_id="track0",
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await player.play_media(media)
        await player.play_media(media)

    assert len(loaded_ids) == 2
    assert loaded_ids[0] != loaded_ids[1]
    assert [SonosPlayer.bare_item_id(x) for x in loaded_ids] == ["track0", "track0"]


async def test_window_items_are_served_under_the_loads_wire_id() -> None:
    """The window describes items under the same ids the load command named."""
    player, _ = _make_player([_make_queue_item("track0"), _make_queue_item("track1")])
    player.cloud_queue_item_generation = 7
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {"itemId": player.wire_item_id("track0")}

    response = await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    served = [x["id"] for x in body["items"]]
    assert served == ["track0@7", "track1@7"]


async def test_window_ids_stay_on_the_generation_the_request_started_with() -> None:
    """A load landing mid-request must not relabel the old window's items."""
    player, _ = _make_player([_make_queue_item("track0")])
    player.cloud_queue_item_generation = 7
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.query = {"itemId": player.wire_item_id("track0")}

    async def _bump_mid_build(_player_id: str, media: PlayerMedia, **_kwargs: object) -> str:
        player.cloud_queue_item_generation = 8
        return media.uri

    player.mass.streams.resolve_stream_url = AsyncMock(  # type: ignore[method-assign]
        side_effect=_bump_mid_build
    )

    response = await cloud_queue._handle_sonos_queue_itemwindow(player, request)

    body = json.loads(response.text or "{}")
    assert [x["id"] for x in body["items"]] == ["track0@7"]


async def test_time_played_matches_the_playing_item_through_its_wire_id() -> None:
    """Only the current load's wire id (or a legacy bare id) may update the position."""
    player, _ = _make_player([_make_queue_item("track0")])
    player.cloud_queue_item_generation = 3
    player._attr_current_media = PlayerMedia(uri="track0", queue_item_id="track0")
    player.update_elapsed_time = MagicMock()  # type: ignore[method-assign]
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.json = AsyncMock(
        return_value={"items": [{"type": "update", "id": "track0@3", "positionMillis": 5000}]}
    )

    await cloud_queue._handle_sonos_queue_time_played(player, request)

    player.update_elapsed_time.assert_called_once_with(5.0)

    # a report that raced a same-track reload carries the old generation and its
    # pre-seek position: it must not win from the position the reload seeked to
    player.update_elapsed_time.reset_mock()
    request.json = AsyncMock(
        return_value={"items": [{"type": "update", "id": "track0@2", "positionMillis": 5000}]}
    )

    await cloud_queue._handle_sonos_queue_time_played(player, request)

    player.update_elapsed_time.assert_not_called()


async def test_time_played_follows_the_audio_served_for_the_reported_position() -> None:
    """Test a position report under a stale cached id lands on the track that really plays."""
    player = _make_player_on_real_queues(4, RepeatMode.OFF)
    queue_data = player.mass.player_queues._queue_data[QUEUE_ID]
    await player.build_cloud_queue_window("track0")
    # a track added right behind the playing one takes the position the speaker cached
    # track1 at, so that is what the speaker plays there
    queue_data.items.insert(1, _make_queue_item("guest"))
    player.on_queue_position_served(1, "guest")
    player._attr_current_media = PlayerMedia(uri="guest", queue_item_id="guest")
    player.update_elapsed_time = MagicMock()  # type: ignore[method-assign]
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.json = AsyncMock(
        return_value={"items": [{"type": "update", "id": "track1@0", "positionMillis": 5000}]}
    )

    await cloud_queue._handle_sonos_queue_time_played(player, request)

    player.update_elapsed_time.assert_called_once_with(5.0)


async def test_play_media_keeps_describing_the_queue_until_the_new_one_is_loaded() -> None:
    """Test the still-playing queue is not blanked while its replacement is being loaded."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    loaded_with_queue_id: list[str | None] = []

    async def _play_cloud_queue(*_args: object, **_kwargs: object) -> None:
        # by the time the speaker is told to load, the id must already point at the new queue
        loaded_with_queue_id.append(player.cloud_queue_id)

    _loads_into_a_session(client.player.group, _play_cloud_queue)
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
    playing.protocol_id = "playing"
    playing.cloud_queue_id = QUEUE_ID
    other.protocol_id = "other"
    other.cloud_queue_id = "another_queue"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(provider), "protocols", property(lambda _self: [playing, other]))
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


def _error_report(report_type: str = "update", report_id: str = "report-1") -> dict[str, object]:
    """
    Build the report a speaker sends when it gives up on an item.

    Shaped after a real one from support#6329: it carries no positionMillis, which
    every healthy report has.
    """
    return {
        "reportId": report_id,
        "error": {"type": "playback", "status": "ERROR_LSE"},
        "contextVersion": "1",
        "id": "track0@3",
        "type": report_type,
        "positionMillisAtSegmentStart": 0,
        "durationPlayedMillis": 4848,
        "timeSincePlaybackMillis": 215,
    }


def _player_for_error_reports() -> MagicMock:
    """Create a speaker whose current item is the one the reports name."""
    player = MagicMock(spec=SonosPlayer)
    player.cloud_queue_item_generation = 3
    player.wire_item_id = lambda item_id, generation=None: SonosPlayer.wire_item_id(
        player, item_id, generation
    )
    player.current_media = MagicMock()
    player.current_media.queue_item_id = "track0"
    player.current_media.title = "A Strange Happening"
    player.display_name = "Huiskamer"
    player.reported_playback_errors = deque(maxlen=16)
    return player


@pytest.mark.parametrize("report_type", ["update", "final"], ids=["update", "final"])
async def test_a_speaker_giving_up_on_an_item_is_reported(
    caplog: pytest.LogCaptureFixture, report_type: str
) -> None:
    """
    A speaker that cannot play an item says so only here, so it must not pass silently.

    Playback stops while Music Assistant still believes the track is playing, and the
    speaker sends the failure as both an update and a final report.
    """
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [_error_report(report_type)]})

    with caplog.at_level(logging.WARNING, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert "ERROR_LSE" in caplog.text
    assert "A Strange Happening" in caplog.text
    player.update_elapsed_time.assert_not_called()


async def test_the_speakers_retry_of_a_failure_is_not_reported_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The speaker re-sends the same report until it gives up, which is one failure."""
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [_error_report()]})

    with caplog.at_level(logging.WARNING, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_time_played(player, request)
        request.json = AsyncMock(return_value={"items": [_error_report("final")]})
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert caplog.text.count("ERROR_LSE") == 1
    player.release_failed_item.assert_called_once_with("track0@3")


async def test_a_failure_reported_only_through_the_cloud_queue_releases_the_item() -> None:
    """The report endpoint is the speaker's other way of saying it gave up on an item."""
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [_error_report()]})

    await cloud_queue._handle_sonos_queue_time_played(player, request)

    player.release_failed_item.assert_called_once_with("track0@3")


async def test_a_batch_of_failures_resent_together_is_not_reported_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One report can carry several failures, and the speaker resends the whole batch."""
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    batch = {"items": [_error_report(report_id="report-a"), _error_report(report_id="report-b")]}
    request = MagicMock()
    request.json = AsyncMock(return_value=batch)

    with caplog.at_level(logging.WARNING, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_time_played(player, request)
        request.json = AsyncMock(return_value=batch)
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert caplog.text.count("ERROR_LSE") == 2


async def test_a_later_failure_on_another_item_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the repeat of one report is held back, never the next thing that fails."""
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    request = MagicMock()

    with caplog.at_level(logging.WARNING, logger="test.sonos.cloud_queue"):
        request.json = AsyncMock(return_value={"items": [_error_report()]})
        await cloud_queue._handle_sonos_queue_time_played(player, request)
        request.json = AsyncMock(return_value={"items": [_error_report(report_id="report-2")]})
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert caplog.text.count("ERROR_LSE") == 2


@pytest.mark.parametrize("status", [404, "404"], ids=["int", "str"])
async def test_a_track_our_stream_server_refused_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture, status: int | str
) -> None:
    """
    A 404 is us refusing a track the queue moved past, not the speaker failing.

    After a queue edit the speaker asks for every track it cached, and each refusal comes
    back as a report. Those must not bury the failures this log is for.
    """
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    refused = {
        **_error_report("final", report_id="refused-1"),
        "error": {"type": "http", "status": status},
        "id": "track1@3",
    }
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [refused]})

    with caplog.at_level(logging.DEBUG, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "refused track1@3" in caplog.text
    # nor may it take a place in the history that holds back repeats of real failures
    assert not player.reported_playback_errors
    # and a refused track never started a source that would need releasing
    player.release_failed_item.assert_not_called()


async def test_another_http_error_is_still_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Only a refusal is expected, any other http error the speaker hit is a real failure."""
    player = _player_for_error_reports()
    cloud_queue = _make_cloud_queue()
    failed = {**_error_report(report_id="http-500"), "error": {"type": "http", "status": 500}}
    request = MagicMock()
    request.json = AsyncMock(return_value={"items": [failed]})

    with caplog.at_level(logging.WARNING, logger="test.sonos.cloud_queue"):
        await cloud_queue._handle_sonos_queue_time_played(player, request)

    assert "reported 500 (http)" in caplog.text


@pytest.mark.parametrize(
    ("woken", "playing", "expect_nudge"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
async def test_a_load_after_a_wake_nudges_a_speaker_that_stays_paused(
    woken: bool, playing: bool, expect_nudge: bool
) -> None:
    """A woken speaker restores its session paused and needs play() when the load does not start it."""
    player, _ = _make_player([_make_queue_item("track0")])
    client = MagicMock()
    client.player.is_passive = False
    _loads_into_a_session(client.player.group)
    client.player.group.play = AsyncMock()
    reported = PlaybackState.PLAYING if playing else PlaybackState.PAUSED
    client.player.group.playback_state = next(
        state for state, mapped in PLAYBACK_STATE_MAP.items() if mapped == reported
    )
    player.client = client
    player._woken_from_sleep = woken

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        patch.setattr("music_assistant.providers.sonos.player.WAKE_PLAY_KICK_TIMEOUT", 0.05)
        await player.play_media(
            PlayerMedia(
                uri="library://track/1",
                media_type=MediaType.TRACK,
                source_id=QUEUE_ID,
                queue_item_id="track0",
                queue_session_id="session-2",
            )
        )

    assert client.player.group.play.await_count == (1 if expect_nudge else 0)
    assert player._woken_from_sleep is False


async def _read_window(player: SonosPlayer, item_id: str) -> None:
    """Let the speaker read its cloud queue window around the given id, as it does over HTTP."""
    request = MagicMock()
    request.query = {"itemId": item_id, "previousWindowSize": "9", "upcomingWindowSize": "10"}
    await _make_cloud_queue()._handle_sonos_queue_itemwindow(player, request)


async def test_the_next_item_of_the_window_the_speaker_read_is_received() -> None:
    """An item the speaker read in its window is one it holds, not one it fetches cold."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    player, _ = _make_player(items)

    assert not player.has_received_next_item(items[1])
    await _read_window(player, "track0@0")

    assert player.has_received_next_item(items[1])


async def test_an_item_added_after_the_speaker_read_its_window_is_not_received() -> None:
    """A track that extends the queue counts only once the speaker read a window holding it."""
    player, queues = _make_player([_make_queue_item("track0")])
    await _read_window(player, "track0@0")
    # autoplay extends a one-track start: the speaker is told to refresh, but holds back
    queues.items.append(_make_queue_item("track1"))

    assert not player.has_received_next_item(queues.items[1])
    await _read_window(player, "track0@0")
    assert player.has_received_next_item(queues.items[1])


async def test_an_item_that_replaced_one_the_window_holds_is_received() -> None:
    """The speaker holds its window by position, so a replaced item there is not fetched cold."""
    player, queues = _make_player([_make_queue_item(f"track{i}") for i in range(4)])
    await _read_window(player, "track0@0")
    queues.items[1:] = [_make_queue_item(f"new{i}") for i in range(1, 6)]

    assert player.has_received_next_item(queues.items[1])
    assert player.has_received_next_item(queues.items[3])
    # the new tracks are more than the ones they replace: the last position is not held
    assert not player.has_received_next_item(queues.items[4])


async def test_a_window_read_before_the_current_load_is_not_received() -> None:
    """A load starts the speaker over: only a window it read since then counts."""
    items = [_make_queue_item(f"track{i}") for i in range(3)]
    player, _ = _make_player(items)
    await _read_window(player, "track0@0")
    client = MagicMock()
    client.player.is_passive = False
    _loads_into_a_session(client.player.group)
    player.client = client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await player.play_media(
            PlayerMedia(
                uri="library://track/1",
                media_type=MediaType.TRACK,
                source_id=QUEUE_ID,
                queue_item_id="track0",
                queue_session_id="session-2",
            )
        )

    assert not player.has_received_next_item(items[1])
    await _read_window(player, player.wire_item_id("track0") or "")
    assert player.has_received_next_item(items[1])
