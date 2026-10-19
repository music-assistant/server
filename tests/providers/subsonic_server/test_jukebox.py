"""Tests for the Subsonic jukebox mode on a Music Assistant player queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import MultiDict, MultiDictProxy
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import ItemMapping, Radio, Track

from music_assistant.providers.subsonic_server.jukebox import JukeboxEndpoints
from music_assistant.providers.subsonic_server.request import SubsonicRequest
from music_assistant.providers.subsonic_server.response import SubsonicError

PLAYER = "RINCON_B8E937B7FDA001400"
MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)
GUEST_WITHOUT_QUEUES = User(user_id="x", username="x", role="custom-role-without-scopes")


@dataclass
class FakeQueueItem:
    """The parts of a QueueItem the jukebox reads."""

    queue_item_id: str
    media_item: Track


@dataclass
class FakeQueue:
    """A player queue that keeps played items as history, like Music Assistant."""

    items: list[FakeQueueItem] = field(default_factory=list)
    current_index: int | None = None
    state: PlaybackState = PlaybackState.IDLE
    corrected_elapsed_time: float = 0.0
    buffered_index: int | None = None
    counter: int = 0

    @property
    def current_item(self) -> FakeQueueItem | None:
        """Return the item at the current index."""
        if self.current_index is None or self.current_index >= len(self.items):
            return None
        return self.items[self.current_index]


class FakePlayerQueues:
    """Stand-in for the player queues controller, backed by one FakeQueue."""

    def __init__(self, queue: FakeQueue) -> None:
        """Wrap the given queue."""
        self.queue = queue
        self.play = AsyncMock(side_effect=self._play)
        self.pause = AsyncMock(side_effect=self._pause)

    def get(self, queue_id: str) -> FakeQueue:
        """Return the queue."""
        return self.queue

    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[FakeQueueItem]:
        """Return a page of queue items."""
        return self.queue.items[offset : offset + limit]

    def get_item(self, queue_id: str, item_id: str) -> FakeQueueItem | None:
        """Return a queue item by id."""
        return next((item for item in self.queue.items if item.queue_item_id == item_id), None)

    def index_by_id(self, queue_id: str, item_id: str) -> int | None:
        """Return the index of a queue item."""
        for index, item in enumerate(self.queue.items):
            if item.queue_item_id == item_id:
                return index
        return None

    def clear(self, queue_id: str) -> None:
        """Empty the queue and stop."""
        self.queue.items = []
        self.queue.current_index = None
        self.queue.state = PlaybackState.IDLE

    def delete_item(self, queue_id: str, item_id: str) -> None:
        """Delete an item unless the player already buffered it."""
        index = self.index_by_id(queue_id, item_id)
        assert index is not None
        if self.queue.buffered_index is not None and index <= self.queue.buffered_index:
            return
        self.queue.items.pop(index)

    async def play_media(self, queue_id: str, media: list[str], option: QueueOption) -> None:
        """Append tracks without starting playback."""
        assert option == QueueOption.ADD
        await asyncio.sleep(0)
        for uri in media:
            self.queue.counter += 1
            self.queue.items.append(FakeQueueItem(f"q{self.queue.counter}", _track(uri)))

    async def play_index(self, queue_id: str, index: str, seek_position: int = 0) -> None:
        """Play the item with the given id."""
        self.queue.current_index = self.index_by_id(queue_id, index)
        self.queue.buffered_index = self.queue.current_index
        self.queue.corrected_elapsed_time = seek_position
        self.queue.state = PlaybackState.PLAYING

    def advance(self) -> None:
        """Move to the next item, as the player does at the end of a track."""
        assert self.queue.current_index is not None
        self.queue.current_index += 1
        self.queue.buffered_index = self.queue.current_index

    async def _play(self, queue_id: str) -> None:
        self.queue.state = PlaybackState.PLAYING

    async def _pause(self, queue_id: str) -> None:
        self.queue.state = PlaybackState.PAUSED


def _track(uri: str) -> Track:
    item_id = uri.rsplit("/", 1)[-1]
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings=set(),
        duration=200,
        album=ItemMapping(media_type=MediaType.ALBUM, item_id="5", provider="library", name="A"),
    )


def _jukebox(queue: FakeQueue | None = None, player_id: str | None = PLAYER) -> JukeboxEndpoints:
    provider = MagicMock()
    provider.jukebox_player_id = player_id
    provider.mass.player_queues = FakePlayerQueues(queue or FakeQueue())
    provider.mass.players.get_player.return_value = MagicMock(volume_level=40, available=True)
    provider.mass.players.cmd_volume_set = AsyncMock()
    return JukeboxEndpoints(provider)


async def _control(jukebox: JukeboxEndpoints, user: User = MEMBER, **params: Any) -> Any:
    multi: MultiDict[str] = MultiDict()
    for key, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            multi.add(key, str(item))
    request = SubsonicRequest(params=MultiDictProxy(multi), user=user)
    return await jukebox.endpoints()["jukeboxControl"](request)


async def test_disabled_without_player() -> None:
    """Without a configured player the jukebox answers not authorized."""
    with pytest.raises(SubsonicError) as err:
        await _control(_jukebox(player_id=None), action="status")
    assert err.value.code == 50


async def test_requires_queue_control_scope() -> None:
    """A user who may not control queues cannot use the jukebox."""
    with pytest.raises(SubsonicError) as err:
        await _control(_jukebox(), user=GUEST_WITHOUT_QUEUES, action="status")
    assert err.value.code == 50


async def test_feishin_flow_set_skip_start() -> None:
    """Set two tracks, skip to the first and start, as Feishin does."""
    jukebox = _jukebox()
    status = await _control(jukebox, action="set", id=["tr-1", "tr-2"])
    assert status["jukeboxStatus"]["playing"] is False
    await _control(jukebox, action="skip", index=0, offset=0)
    await _control(jukebox, action="start")
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert playlist["currentIndex"] == 0
    assert playlist["playing"] is True
    assert playlist["gain"] == 0.4
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-1", "tr-2"]


async def test_remove_played_track_shifts_indexes() -> None:
    """After the player advanced, removing index 0 makes the current track index 0."""
    jukebox = _jukebox()
    await _control(jukebox, action="set", id=["tr-1", "tr-2"])
    await _control(jukebox, action="skip", index=0)
    queues = cast("FakePlayerQueues", jukebox.mass.player_queues)
    queues.advance()
    assert (await _control(jukebox, action="status"))["jukeboxStatus"]["currentIndex"] == 1
    await _control(jukebox, action="remove", index=0)
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert playlist["currentIndex"] == 0
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-2"]


async def test_replace_next_track() -> None:
    """Removing the upcoming track and adding another replaces it, as Feishin does."""
    jukebox = _jukebox()
    await _control(jukebox, action="set", id=["tr-1", "tr-2"])
    await _control(jukebox, action="skip", index=0)
    await _control(jukebox, action="remove", index=1)
    await _control(jukebox, action="add", id="tr-3")
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-1", "tr-3"]
    assert playlist["currentIndex"] == 0


async def test_skip_with_offset_and_stop() -> None:
    """Skip seeks within the track, stop pauses the player."""
    jukebox = _jukebox()
    await _control(jukebox, action="set", id="tr-1")
    status = (await _control(jukebox, action="skip", index=0, offset=42))["jukeboxStatus"]
    assert status["position"] == 42
    status = (await _control(jukebox, action="stop"))["jukeboxStatus"]
    assert status["playing"] is False


async def test_set_gain_is_clamped() -> None:
    """The gain maps onto the 0..100 player volume and is clamped to 0..1."""
    jukebox = _jukebox()
    await _control(jukebox, action="setGain", gain="0.25")
    await _control(jukebox, action="setGain", gain="3")
    volume_set = jukebox.mass.players.cmd_volume_set
    assert isinstance(volume_set, AsyncMock)
    assert [call.args[1] for call in volume_set.await_args_list] == [25, 100]


async def test_mirrors_queue_changed_elsewhere() -> None:
    """When the queue plays an item the jukebox does not know, the view mirrors the queue."""
    queue = FakeQueue()
    jukebox = _jukebox(queue)
    await _control(jukebox, action="set", id="tr-1")
    queue.items = [
        FakeQueueItem("other-1", _track("library://track/7")),
        FakeQueueItem("other-2", _track("library://track/8")),
    ]
    queue.current_index = 1
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-7", "tr-8"]
    assert playlist["currentIndex"] == 1


async def test_invalid_index_and_action() -> None:
    """An index outside the playlist and an unknown action are errors."""
    jukebox = _jukebox()
    await _control(jukebox, action="set", id="tr-1")
    with pytest.raises(SubsonicError):
        await _control(jukebox, action="skip", index=5)
    with pytest.raises(SubsonicError):
        await _control(jukebox, action="shuffle")


async def test_set_rejects_non_track_ids() -> None:
    """Only track ids can be played."""
    with pytest.raises(SubsonicError) as err:
        await _control(_jukebox(), action="set", id="al-5")
    assert err.value.code == 70


async def test_seek_to_the_current_position_is_ignored() -> None:
    """A skip to the playing track at about its position does not restart the stream."""
    queue = FakeQueue()
    jukebox = _jukebox(queue)
    await _control(jukebox, action="set", id="tr-1")
    await _control(jukebox, action="skip", index=0, offset=100)
    queues = cast("FakePlayerQueues", jukebox.mass.player_queues)
    queues.play_index = AsyncMock()  # type: ignore[method-assign]
    queue.corrected_elapsed_time = 114
    await _control(jukebox, action="skip", index=0, offset=115)
    queues.play_index.assert_not_awaited()
    await _control(jukebox, action="skip", index=0, offset=30)
    queues.play_index.assert_awaited_once()


async def test_playing_is_reported_while_the_stream_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right after a start the jukebox reports playing, even while the queue is not yet playing."""
    queue = FakeQueue()
    jukebox = _jukebox(queue)
    now = 1000.0
    monkeypatch.setattr(
        "music_assistant.providers.subsonic_server.jukebox.time.monotonic", lambda: now
    )
    await _control(jukebox, action="set", id="tr-1")
    await _control(jukebox, action="skip", index=0)
    queue.state = PlaybackState.IDLE
    assert (await _control(jukebox, action="status"))["jukeboxStatus"]["playing"] is True
    now += 6
    assert (await _control(jukebox, action="status"))["jukeboxStatus"]["playing"] is False


async def test_stop_ends_the_grace_period() -> None:
    """After a stop the jukebox reports the real state at once."""
    queue = FakeQueue()
    jukebox = _jukebox(queue)
    await _control(jukebox, action="set", id="tr-1")
    await _control(jukebox, action="start")
    status = (await _control(jukebox, action="stop"))["jukeboxStatus"]
    assert status["playing"] is False


@pytest.mark.parametrize("player", [None, MagicMock(available=False)])
async def test_unavailable_player_is_an_error(player: MagicMock | None) -> None:
    """Without a reachable player the jukebox reports an error instead of a made-up volume."""
    jukebox = _jukebox()
    cast("MagicMock", jukebox.mass.players.get_player).return_value = player
    with pytest.raises(SubsonicError) as err:
        await _control(jukebox, action="status")
    assert err.value.code == 0


async def test_player_outside_the_user_filter_is_refused() -> None:
    """A user limited to other players may not use the jukebox player."""
    restricted = User(
        user_id="r", username="r", role=UserRole.USER, player_filter=["another-player"]
    )
    with pytest.raises(SubsonicError) as err:
        await _control(_jukebox(), user=restricted, action="status")
    assert err.value.code == 50


async def test_items_that_are_not_tracks_stay_out_of_the_playlist() -> None:
    """A radio station in the queue is not listed and does not shift the indexes."""
    queue = FakeQueue()
    radio = Radio(item_id="7", provider="library", name="FIP", provider_mappings=set())
    queue.items = [
        FakeQueueItem("radio", cast("Track", radio)),
        FakeQueueItem("song", _track("library://track/8")),
    ]
    queue.current_index = 1
    queue.state = PlaybackState.PLAYING
    playlist = (await _control(_jukebox(queue), action="get"))["jukeboxPlaylist"]
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-8"]
    assert playlist["currentIndex"] == 0


async def test_removing_a_buffered_track_is_an_error() -> None:
    """A track the player already loaded can not be removed and stays in the playlist."""
    queue = FakeQueue()
    jukebox = _jukebox(queue)
    await _control(jukebox, action="set", id=["tr-1", "tr-2"])
    await _control(jukebox, action="skip", index=0)
    queue.buffered_index = 1
    with pytest.raises(SubsonicError):
        await _control(jukebox, action="remove", index=1)
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert [entry["id"] for entry in playlist["entry"]] == ["tr-1", "tr-2"]


async def test_long_queues_are_read_page_by_page() -> None:
    """Every track of a queue longer than one page is part of the playlist."""
    queue = FakeQueue()
    queue.items = [FakeQueueItem(f"q{n}", _track(f"library://track/{n}")) for n in range(1200)]
    queue.current_index = 1100
    queue.state = PlaybackState.PLAYING
    playlist = (await _control(_jukebox(queue), action="get"))["jukeboxPlaylist"]
    assert len(playlist["entry"]) == 1200
    assert playlist["currentIndex"] == 1100


async def test_concurrent_adds_do_not_duplicate_entries() -> None:
    """Two adds at the same time each append their own track once."""
    jukebox = _jukebox()
    await asyncio.gather(
        _control(jukebox, action="add", id="tr-1"), _control(jukebox, action="add", id="tr-2")
    )
    playlist = (await _control(jukebox, action="get"))["jukeboxPlaylist"]
    assert sorted(entry["id"] for entry in playlist["entry"]) == ["tr-1", "tr-2"]
