"""Tests for a Sonos speaker that leaves the sync group it leads while the group plays on."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.exceptions import CommandTimeout, ConnectionClosed
from aiosonos.group import SonosGroup as AiosonosGroup
from aiosonos.player import SonosPlayer as AiosonosPlayer
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia
from music_assistant_models.queue_item import QueueItem

from music_assistant.models.player import Player
from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.cloud_queue import SonosCloudQueue
from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider
from tests.providers.sonos.test_cloud_queue import (
    QUEUE_ID,
    _FakeQueues,
    _loads_into_a_session,
    _make_cloud_queue,
    _make_media_queue_item,
    _make_player,
    _set_playback_quality,
)
from tests.providers.sonos.test_player import _connected_player

HOUSEHOLD = "Sonos_household"
# Kitchen leads; Bathroom joined it first and Study last, which Sonos lists the other way round
KITCHEN = "RINCON_F0F6C100000101400"
STUDY = "RINCON_38420B00000201400"
BATHROOM = "RINCON_48A6B800000301400"
KITCHEN_GROUP = f"{KITCHEN}:400"
BATHROOM_GROUP = f"{BATHROOM}:401"
KITCHEN_ALONE = f"{KITCHEN}:402"
STUDY_GROUP = f"{STUDY}:403"
BATHROOM_ALONE = f"{BATHROOM}:404"
# the queue of the player of Kitchen itself, which is what a group made by hand plays
KITCHEN_QUEUE = f"pl_{KITCHEN}"
GENERATION = 7

PLAYING = {
    "_objectType": "playbackStatus",
    "playbackState": "PLAYBACK_STATE_PLAYING",
    "positionMillis": 25000,
    "availablePlaybackActions": {"canPause": True, "canStop": True},
    "playModes": {},
}
IDLE = {**PLAYING, "playbackState": "PLAYBACK_STATE_IDLE", "positionMillis": 0}
NOTHING_LOADED = {"_objectType": "metadataStatus"}
# what a speaker is sent to stop what it plays without a session of its own
STOP = ["playback_session.create_session", "playback_session.leave_session"]


def _metadata(queue_id: str, item_id: str, generation: int = GENERATION) -> dict[str, Any]:
    """Build the metadata a speaker reports while it plays an item of a cloud queue of ours."""
    return {
        "_objectType": "metadataStatus",
        "container": {
            "name": "Music Assistant",
            "type": "trackList",
            "id": {"serviceId": "mass", "objectId": f"mass:{queue_id}"},
            "service": {"name": "Music Assistant", "id": "mass"},
        },
        "currentItem": {
            "id": f"{item_id}@{generation}",
            "track": {"name": item_id, "durationMillis": 180000},
        },
    }


def _groups(*groups: tuple[str, str, list[str]]) -> dict[str, Any]:
    """Build a groups event (or getGroups reply) as the speakers send it."""
    return {
        "_objectType": "groups",
        "groups": [
            {
                "_objectType": "group",
                "id": group_id,
                "coordinatorId": coordinator,
                "playerIds": members,
                "name": coordinator,
            }
            for group_id, coordinator, members in groups
        ],
        "players": [
            {"_objectType": "player", "id": player_id, "name": player_id}
            for player_id in (KITCHEN, STUDY, BATHROOM)
        ],
    }


SYNCED = _groups((KITCHEN_GROUP, KITCHEN, [KITCHEN, STUDY, BATHROOM]))
# Sonos hands the group to the member that joined first, not to the first one it lists
MOVED = _groups((BATHROOM_GROUP, BATHROOM, [BATHROOM, STUDY]), (KITCHEN_ALONE, KITCHEN, [KITCHEN]))
MOVED_AGAIN = _groups(
    (STUDY_GROUP, STUDY, [STUDY]),
    (BATHROOM_ALONE, BATHROOM, [BATHROOM]),
    (KITCHEN_ALONE, KITCHEN, [KITCHEN]),
)
# the group a leader that leaves had, the household after it left and the member that takes over
MOVES = {
    KITCHEN: (KITCHEN_GROUP, MOVED, BATHROOM),
    BATHROOM: (BATHROOM_GROUP, MOVED_AGAIN, STUDY),
}


def _own_item(item_id: str) -> QueueItem:
    """Build a track of the queue of the player of Kitchen itself."""
    item = QueueItem(queue_id=KITCHEN_QUEUE, queue_item_id=item_id, name=item_id, duration=180)
    item.media_item = MagicMock(media_type=MediaType.TRACK, uri=f"library://track/{item_id}")
    return item


class _Queues:
    """The queues the speakers read, by queue id."""

    def __init__(self, queues: dict[str, _FakeQueues]) -> None:
        self._queues = queues
        self.transfer_queue = AsyncMock()

    def get(self, queue_id: str) -> MagicMock | None:
        """Return the queue with the given id."""
        return self._queues[queue_id].queue if queue_id in self._queues else None

    def index_by_id(self, queue_id: str, item_id: str) -> int | None:
        """Return the index of the item with the given id."""
        return self._queues[queue_id].index_by_id(queue_id, item_id)

    def get_item(self, queue_id: str, index_or_id: int | str) -> QueueItem | None:
        """Return the item at the given index, or with the given id."""
        return self._queues[queue_id].get_item(queue_id, index_or_id)

    def get_next_item(self, queue_id: str, index_or_id: int | str) -> QueueItem | None:
        """Return the item that plays after the given index or item id."""
        return self._queues[queue_id].get_next_item(queue_id, index_or_id)

    async def player_media_from_queue_item(self, queue_item: QueueItem) -> PlayerMedia:
        """Return the media for the given queue item."""
        return PlayerMedia(
            uri=queue_item.uri,
            media_type=MediaType.TRACK,
            title=queue_item.name,
            queue_item_id=queue_item.queue_item_id,
            source_id=queue_item.queue_id,
        )


class _Household:
    """Three Sonos speakers around real aiosonos clients: Kitchen leads a sync group's queue."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, playback_quality: str = "lossless_uncompressed"
    ) -> None:
        """
        Create the household, with the group playing the first track of the group's queue.

        :param monkeypatch: The fixture that restores what the household patches.
        :param playback_quality: The playback quality the speakers get their streams in.
        """
        own_queue = _FakeQueues([_own_item(f"own{i}") for i in range(2)])
        # the speakers stop it when Kitchen leaves its group
        own_queue.queue.state = PlaybackState.IDLE
        self.queues = _Queues(
            {
                QUEUE_ID: _FakeQueues(
                    [_make_media_queue_item(f"track{i}", MediaType.TRACK) for i in range(3)]
                ),
                KITCHEN_QUEUE: own_queue,
            }
        )
        self.mass = MagicMock()
        self.mass.create_task.side_effect = self._create_task
        # the sync group whose queue plays, which every speaker is a member of
        self.sync_group = MagicMock()
        self.sync_group.on_session_moved = AsyncMock(return_value=True)
        self.sync_group_members = {f"pl_{player_id}" for player_id in (KITCHEN, STUDY, BATHROOM)}
        self.sync_group.counts_in.side_effect = lambda player_id: (
            player_id in self.sync_group_members
        )
        group_player = MagicMock(player_id=QUEUE_ID, own_protocol=self.sync_group)
        self.mass.players.get_player.side_effect = {QUEUE_ID: group_player}.get
        self.mass.players.get_player_lock.side_effect = self._lock
        # the players whose playback lock was taken, in order
        self.locked: list[str] = []
        # what Music Assistant started in the background
        self.tasks: list[asyncio.Task[None]] = []
        # what every speaker answers getGroups with, and answered before the last move
        self.grouping = self.grouping_before = SYNCED
        # how often a speaker still answers with the grouping before the last move
        self.asked_too_early: dict[str, int] = {}
        # the speakers that answer nothing
        self.silent: set[str] = set()
        # set to let a leader that leaves answer for it late, or not at all
        self.leave_answer: asyncio.Event | None = None
        # whether a leader that leaves tells its own connection which member leads its group
        self.tells_of_move = True
        # the playback state and the queue item each speaker reported, in order
        self.reports: dict[str, list[tuple[PlaybackState | None, str | None]]] = {}
        self.apis = {player_id: self._api(player_id) for player_id in (KITCHEN, STUDY, BATHROOM)}
        self.speakers = {
            player_id: self._speaker(player_id, playback_quality) for player_id in self.apis
        }
        self.kitchen._loaded_session_id = "session"
        self.kitchen.cloud_queue_version = 12.5
        self.kitchen.cloud_queue_item_generation = GENERATION
        group = self.kitchen.group_controller
        group._playback_status_data = cast("Any", dict(PLAYING))
        group._unsubscribe_callbacks = [MagicMock()]
        speakers = list(self.speakers.values())
        monkeypatch.setattr(SonosPlayerProvider, "protocols", property(lambda _self: speakers))
        monkeypatch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        self.cloud_queue: SonosCloudQueue = _make_cloud_queue()
        self.play(QUEUE_ID, "track0", group_queue=True)
        for reports in self.reports.values():
            reports.clear()

    @property
    def kitchen(self) -> SonosPlayer:
        """The speaker that leads the group."""
        return self.speakers[KITCHEN]

    @property
    def study(self) -> SonosPlayer:
        """The member Sonos lists first."""
        return self.speakers[STUDY]

    @property
    def bathroom(self) -> SonosPlayer:
        """The member that takes the group over."""
        return self.speakers[BATHROOM]

    def play(self, queue_id: str, item_id: str, *, group_queue: bool) -> None:
        """
        Let the group of Kitchen play an item of a cloud queue that Kitchen loaded.

        :param queue_id: The queue.
        :param item_id: The item of it that plays.
        :param group_queue: Whether the queue is that of a sync group Kitchen leads.
        """
        self.kitchen.cloud_queue_id = queue_id
        self.kitchen.group_queue_id = queue_id if group_queue else None
        group = self.kitchen.group_controller
        group._playback_metadata_data = cast("Any", _metadata(queue_id, item_id))
        for speaker in self.speakers.values():
            speaker.on_player_event(None)

    def commands(self, player_id: str) -> list[str]:
        """Return the commands a speaker was sent that play, stop or (re)group something."""
        reads = ("groups.get_groups", "get_volume", "get_playback_status", "get_metadata_status")
        return [
            name
            for name, _args, _kwargs in self.apis[player_id].mock_calls
            if name and "(" not in name and not name.endswith(("subscribe", *reads))
        ]

    async def window(self, path_id: str, item_id: str | None = None) -> dict[str, Any]:
        """Return the window the cloud queue answers a speaker with under the given path."""
        request = MagicMock()
        request.path = f"/sonos_queue/{path_id}/v2.3/itemWindow"
        request.query = {"itemId": item_id} if item_id else {}
        response = await self.cloud_queue.handle_request(request)
        assert response.status == 200
        return cast("dict[str, Any]", json.loads(response.text or "{}"))

    async def report_position(self, path_id: str, item_id: str, seconds: int) -> None:
        """Let a speaker report its position in an item under the given path."""
        request = MagicMock()
        request.path = f"/sonos_queue/{path_id}/v2.3/timePlayed"
        request.json = AsyncMock(
            return_value={
                "items": [{"type": "update", "id": item_id, "positionMillis": seconds * 1000}]
            }
        )
        assert (await self.cloud_queue.handle_request(request)).status == 204

    def serving(self, path_id: str) -> SonosPlayer | None:
        """Return the speaker the cloud queue answers the given path for."""
        return self.cloud_queue._speaker_for(path_id)

    async def leave(self, leader: str = KITCHEN) -> None:
        """
        Let a leader leave its group by itself, as one that is taken out in the Sonos app.

        :param leader: The speaker that leads the group.
        """
        await self._change_group(leader, MOVES[leader][0], [], [leader])
        # it tells of the move well before it reports its new grouping
        await asyncio.sleep(0)

    async def settle(self) -> None:
        """Deliver the groups event each speaker sends by itself, and let everything finish."""
        for speaker in self.speakers.values():
            speaker.client._handle_groups_event(cast("Any", self.grouping))
        for _ in range(50):
            await asyncio.sleep(0)
        await asyncio.gather(*self.tasks)

    async def close(self) -> None:
        """End what still runs in the background."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _create_task(
        self, coro: Coroutine[Any, Any, None], **_kwargs: object
    ) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    @asynccontextmanager
    async def _lock(self, player_id: str) -> AsyncIterator[None]:
        """Hold the playback lock of a player."""
        self.locked.append(player_id)
        yield

    async def _groups_of(self, player_id: str) -> dict[str, Any]:
        """Answer getGroups for a speaker."""
        if player_id in self.silent:
            await asyncio.Event().wait()
        if self.asked_too_early.get(player_id, 0) > 0:
            self.asked_too_early[player_id] -= 1
            return self.grouping_before
        return self.grouping

    async def _change_group(
        self, player_id: str, group_id: str, *_args: object, **kwargs: Any
    ) -> dict[str, Any]:
        """Answer a group command of a speaker: a leader that takes itself out moves its group."""
        leader = self.speakers[player_id]
        if kwargs.get("player_ids_to_remove", [*_args, None, None][1]) != [player_id]:
            return {"_objectType": "groupInfo"}
        old_group_id, grouping, new_coordinator = MOVES[player_id]
        assert group_id == old_group_id
        self.grouping_before, self.grouping = self.grouping, grouping
        # the others play on, led by one of them, and the leader that left plays nothing
        for speaker_id, status, metadata in (
            (new_coordinator, PLAYING, leader.group_controller.playback_metadata),
            (player_id, IDLE, NOTHING_LOADED),
        ):
            api = self.apis[speaker_id]
            api.playback.get_playback_status.return_value = dict(status)
            api.playback_metadata.get_metadata_status.return_value = dict(metadata)
        if self.tells_of_move:
            # the former coordinator hears of the move at once
            leader.client._handle_group_coordinator_changed(
                old_group_id,
                cast(
                    "Any",
                    {
                        "_objectType": "groupCoordinatorChanged",
                        "groupStatus": "GROUP_STATUS_MOVED",
                        "playerId": new_coordinator,
                    },
                ),
            )
        if self.leave_answer is not None:
            await self.leave_answer.wait()
        return {"_objectType": "groupInfo"}

    def _api(self, player_id: str) -> AsyncMock:
        """Create the api of a speaker, which is idle in every group it is asked about."""
        api = AsyncMock()

        async def _get_groups(*_args: object, **_kwargs: object) -> dict[str, Any]:
            return await self._groups_of(player_id)

        async def _modify_group_members(*args: object, **kwargs: Any) -> dict[str, Any]:
            return await self._change_group(player_id, cast("str", args[0]), *args[1:], **kwargs)

        api.groups.get_groups.side_effect = _get_groups
        api.groups.modify_group_members.side_effect = _modify_group_members
        api.playback.get_playback_status.return_value = dict(IDLE)
        api.playback_metadata.get_metadata_status.return_value = dict(NOTHING_LOADED)
        api.group_volume.get_volume.return_value = {"volume": 20, "muted": False, "fixed": False}
        return api

    def _speaker(self, player_id: str, playback_quality: str) -> SonosPlayer:
        """Create a connected speaker that reports its state, around a real aiosonos client."""
        speaker, _ = _make_player([])
        _set_playback_quality(speaker, playback_quality)
        streams = cast("MagicMock", speaker.mass).streams
        streams.resolve_stream_url = AsyncMock(
            side_effect=lambda url_id, _media, queue_index=None: f"{url_id}/{queue_index}"
        )
        self.mass.streams = streams
        self.mass.player_queues = streams.mass.player_queues = self.queues
        speaker.mass = self.mass
        speaker._protocol_id = player_id
        speaker._attr_name = player_id
        speaker._player = MagicMock(
            spec=Player, player_id=f"pl_{player_id}", stop_called=False, active_output_protocol=None
        )
        speaker._provider = MagicMock(
            instance_id="sonos", get_protocol=lambda protocol_id: self.speakers.get(protocol_id)
        )
        speaker.cloud_queue_id = None
        speaker._loaded_session_id = None
        speaker._hand_over_tasks = set()
        speaker._airplay_rejoin_members = set()
        speaker._own_airplay_session = False
        speaker._reported_bonded_ids = set()
        speaker._sleeps = False
        speaker._attr_group_members = []
        speaker._attr_can_group_with = set()
        speaker._attr_source_list = []
        speaker._attr_active_source = None
        speaker._attr_current_media = None
        speaker._attr_playback_state = PlaybackState.IDLE
        speaker.discovery_info = cast(
            "Any", {"device": {"modelDisplayName": "Era 100", "capabilities": ["PLAYBACK"]}}
        )
        speaker.client = client = self._client(player_id)
        reports = self.reports.setdefault(player_id, [])

        def _report(*_args: object, **_kwargs: object) -> None:
            media = speaker.current_media
            reports.append((speaker.playback_state, media.queue_item_id if media else None))

        speaker.update_state = MagicMock(side_effect=_report)  # type: ignore[method-assign, misc]
        # what on_attach subscribes to
        client.subscribe(
            speaker.on_player_event,
            (
                SonosEventType.GROUP_ADDED,
                SonosEventType.GROUP_UPDATED,
                SonosEventType.PLAYER_UPDATED,
            ),
        )
        client.subscribe(
            speaker._on_group_coordinator_changed, SonosEventType.GROUP_COORDINATOR_CHANGED
        )
        return speaker

    def _client(self, player_id: str) -> SonosLocalApiClient:
        """Create an aiosonos client of one speaker, set up from the synced household."""
        client = SonosLocalApiClient("192.168.1.10", MagicMock())
        client._loop = cast("Any", asyncio.get_running_loop())
        client._player_id = player_id
        client._household_id = HOUSEHOLD
        client.api = self.apis[player_id]
        for group_data in SYNCED["groups"]:
            client._groups[group_data["id"]] = AiosonosGroup(client, cast("Any", dict(group_data)))
        player_data = next(x for x in SYNCED["players"] if x["id"] == player_id)
        client._player = AiosonosPlayer(client, cast("Any", dict(player_data)))
        return client


@pytest.fixture
async def make_home(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[Callable[..., _Household]]:
    """Return a factory of households, whose background work ends with the test."""
    homes: list[_Household] = []

    def _make(playback_quality: str = "lossless_uncompressed") -> _Household:
        homes.append(_Household(monkeypatch, playback_quality))
        return homes[-1]

    yield _make
    for home in homes:
        await home.close()


@pytest.fixture
async def home(make_home: Callable[..., _Household]) -> _Household:
    """Return a household whose sync group plays a track that is served as a file."""
    return make_home()


def _kept(speaker: SonosPlayer) -> dict[str, object]:
    """Return what a speaker keeps for the cloud queue it has loaded."""
    return {
        "cloud_queue_id": speaker.cloud_queue_id,
        "group_queue_id": speaker.group_queue_id,
        "loaded_session_id": speaker._loaded_session_id,
        "version": speaker.cloud_queue_version,
        "generation": speaker.cloud_queue_item_generation,
        "window_item_ids": speaker._window_item_ids,
        "served_bindings": speaker._served_bindings,
    }


CLEAN: dict[str, object] = {
    "cloud_queue_id": None,
    "group_queue_id": None,
    "loaded_session_id": None,
    "version": 12.5,
    "generation": GENERATION + 1,
    "window_item_ids": {},
    "served_bindings": {},
}


async def _step_out(home: _Household, leader: str = KITCHEN) -> None:
    """Let the sync group take its leader out, as its change of leader does."""
    await home.speakers[leader].set_members(player_ids_to_remove=[leader])


async def _until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until a condition holds."""
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    ("state", "hands_over"),
    [
        ("plays a file of the group's queue", True),
        ("paused in it", True),
        ("gets its tracks as flac", False),
        ("plays a radio stream of the group's queue", False),
        ("plays its own queue", False),
        ("plays the queue as one stream", False),
        ("follows another speaker", False),
        ("disconnected", False),
        ("plays nothing", False),
    ],
)
async def test_a_leader_hands_its_session_over_only_while_it_plays_a_file_of_a_group(
    make_home: Callable[..., _Household], state: str, hands_over: bool
) -> None:
    """Test the session only survives a leader that plays a file out of a group's cloud queue."""
    home = make_home(
        "lossless_compressed" if state == "gets its tracks as flac" else "lossless_uncompressed"
    )
    kitchen, group = home.kitchen, home.kitchen.group_controller
    if state == "paused in it":
        group._playback_status_data = cast(
            "Any", {**PLAYING, "playbackState": "PLAYBACK_STATE_PAUSED"}
        )
    elif state == "plays a radio stream of the group's queue":
        home.queues.get_item(QUEUE_ID, 0).media_item.media_type = MediaType.RADIO  # type: ignore[union-attr]
    elif state == "plays its own queue":
        # what a group made by hand plays
        home.play(KITCHEN_QUEUE, "own0", group_queue=False)
    elif state == "plays the queue as one stream":
        kitchen.cloud_queue_id = kitchen.group_queue_id = None
        kitchen._flow_queue_id = QUEUE_ID
    elif state == "follows another speaker":
        kitchen.client._handle_groups_event(
            cast("Any", _groups((BATHROOM_GROUP, BATHROOM, [BATHROOM, KITCHEN, STUDY])))
        )
        # the group it follows plays the same track
        kitchen.group_controller._playback_metadata_data = group.playback_metadata
    elif state == "disconnected":
        kitchen.connected = False
    elif state == "plays nothing":
        group._playback_metadata_data = cast("Any", dict(NOTHING_LOADED))

    assert kitchen.can_hand_over_session is hands_over


async def test_a_leader_that_steps_out_hands_its_cloud_queue_to_the_new_coordinator(
    home: _Household,
) -> None:
    """Test what is kept for the group's cloud queue moves, and the old leader is left clean."""
    await home.window(QUEUE_ID, f"track0@{GENERATION}")
    home.kitchen.on_queue_position_served(0, "track0")
    kept = _kept(home.kitchen)

    await _step_out(home)

    assert _kept(home.bathroom) == kept
    assert kept["window_item_ids"]
    assert kept["served_bindings"]
    assert _kept(home.kitchen) == CLEAN
    assert home.study.cloud_queue_id is None


async def test_the_new_coordinator_forgets_a_flow_stream_it_played_itself(
    home: _Household,
) -> None:
    """Test a speaker that takes a group over answers with the group's queue, not its own stream."""
    home.bathroom._flow_media = PlayerMedia(uri="http://ma/flow/own.wav", queue_item_id="flow")

    await _step_out(home)

    assert home.bathroom.plays_flow_item is False
    window = await home.bathroom.build_cloud_queue_window(f"track0@{GENERATION}")
    assert window.items[0].queue_item_id == "track0"


async def test_the_speakers_report_what_they_play_once_the_leader_stepped_out(
    home: _Household,
) -> None:
    """Test the new coordinator reports the track of the group's queue, the old leader nothing."""
    await _step_out(home)
    await home.settle()

    assert home.bathroom.playback_state == PlaybackState.PLAYING
    assert home.bathroom.active_source is None
    assert home.bathroom.current_media is not None
    assert home.bathroom.current_media.queue_item_id == "track0"
    assert home.bathroom.elapsed_time == pytest.approx(25, abs=1)
    assert home.bathroom.group_members == [BATHROOM, STUDY]
    assert home.study.synced_to == BATHROOM
    assert home.kitchen.playback_state == PlaybackState.IDLE
    assert home.kitchen.active_source is None
    assert home.kitchen.current_media is None
    assert home.kitchen.group_members == []


async def test_the_new_coordinator_never_reports_idle_while_it_takes_the_group_over(
    home: _Household,
) -> None:
    """Test the speakers report what plays all the way: the group reads what its leader does."""
    home.kitchen.update_elapsed_time(20)
    home.reports[KITCHEN].clear()

    await _step_out(home)
    await home.settle()

    # as its former leader did, until its own connection has read the group it leads
    assert home.reports[BATHROOM]
    assert set(home.reports[BATHROOM]) == {(PlaybackState.PLAYING, "track0")}
    # the members follow the old leader until then: it reports nothing before it has left
    assert set(home.reports[KITCHEN]) == {(PlaybackState.IDLE, None)}


async def test_the_new_coordinator_starts_at_the_position_of_its_former_leader(
    home: _Household,
) -> None:
    """Test the position does not jump while the new coordinator has not read its group yet."""
    home.kitchen.update_elapsed_time(20)
    reported_at = home.kitchen.elapsed_time_last_updated
    # its own connection does not get to read the group it leads
    home.apis[BATHROOM].playback.get_playback_status.side_effect = asyncio.CancelledError

    await _step_out(home)

    assert home.bathroom.playback_state == PlaybackState.PLAYING
    assert home.bathroom.elapsed_time == 20
    assert home.bathroom.elapsed_time_last_updated == reported_at
    assert home.bathroom.current_media is not None
    assert home.bathroom.current_media.queue_item_id == "track0"


async def test_the_group_path_answers_the_same_from_the_new_coordinator(home: _Household) -> None:
    """Test the speakers read the same window from the speaker that leads them now."""
    item_id = f"track0@{GENERATION}"
    before = await home.window(QUEUE_ID, item_id)
    home.kitchen.on_queue_position_served(0, "track0")
    assert home.serving(QUEUE_ID) is home.kitchen

    await _step_out(home)

    assert home.serving(QUEUE_ID) is home.bathroom
    assert await home.window(QUEUE_ID, item_id) == before
    assert [item["id"] for item in before["items"]] == [f"track{i}@{GENERATION}" for i in range(3)]
    assert [item["track"]["mediaUrl"] for item in before["items"]] == [
        f"{QUEUE_ID}/{i}" for i in range(3)
    ]
    assert before["queueVersion"] == "12.5"


async def test_a_position_reported_for_the_item_that_was_taken_over_is_not_its_position(
    home: _Household,
) -> None:
    """Test the new coordinator counts from where it took over: its own status has the position."""
    item_id = f"track0@{GENERATION}"
    await home.report_position(QUEUE_ID, item_id, 20)
    assert home.kitchen.elapsed_time == 20
    await _step_out(home)
    await home.settle()

    # it took over at 25 seconds and reports the 7 seconds it played since
    await home.report_position(QUEUE_ID, item_id, 7)

    assert home.bathroom.elapsed_time == pytest.approx(25, abs=1)
    assert home.kitchen.elapsed_time != 7


async def test_a_hand_over_right_after_a_change_of_track_takes_the_new_item_over(
    home: _Household,
) -> None:
    """Test the item that plays is the one the status names, ahead of the metadata."""
    # the leader moved on to the next track, which its metadata does not name yet
    leader_group = home.kitchen.group_controller
    leader_group._playback_status_data = cast("Any", {**PLAYING, "itemId": f"track1@{GENERATION}"})
    assert home.kitchen.can_hand_over_session

    await _step_out(home)
    await home.settle()
    group = home.bathroom.group_controller
    group._playback_metadata_data = cast("Any", _metadata(QUEUE_ID, "track1"))
    home.bathroom.on_player_event(None)
    assert home.bathroom.current_media is not None
    assert home.bathroom.current_media.queue_item_id == "track1"

    # it took over in the new track, and counts its reports for it from there
    await home.report_position(QUEUE_ID, f"track1@{GENERATION}", 7)

    assert home.bathroom.elapsed_time == pytest.approx(25, abs=1)


async def test_a_position_reported_for_the_next_item_is_taken(home: _Household) -> None:
    """Test the new coordinator's reports count again from the track that follows."""
    await _step_out(home)
    await home.settle()
    group = home.bathroom.group_controller
    group._playback_metadata_data = cast("Any", _metadata(QUEUE_ID, "track1"))
    home.bathroom.on_player_event(None)

    await home.report_position(QUEUE_ID, f"track1@{GENERATION}", 7)

    assert home.bathroom.elapsed_time == 7


async def test_a_position_reported_after_a_new_load_is_taken(home: _Household) -> None:
    """Test a seek on the new coordinator loads the track anew, whose reports count as ever."""
    await _step_out(home)
    await home.settle()
    group = home.bathroom.group_controller
    _loads_into_a_session(cast("MagicMock", group))

    await home.bathroom.play_media(
        PlayerMedia(
            uri="library://track/1",
            media_type=MediaType.TRACK,
            source_id=QUEUE_ID,
            queue_item_id="track0",
            queue_session_id="session2",
        )
    )
    group._playback_metadata_data = cast("Any", _metadata(QUEUE_ID, "track0", GENERATION + 1))
    home.bathroom.on_player_event(None)
    await home.report_position(home.bathroom.protocol_id, f"track0@{GENERATION + 1}", 7)

    assert home.bathroom.elapsed_time == 7


def _report(speaker: SonosPlayer, *, named: str, position_in: str, seconds: int) -> None:
    """
    Let the group a speaker leads report a position and the item it names.

    :param speaker: The speaker that leads the group.
    :param named: The id of the item its metadata names, as the speaker reports it.
    :param position_in: The id of the item its playback status has the position of.
    :param seconds: The position its playback status reports.
    """
    group = speaker.group_controller
    group._playback_status_data = cast(
        "Any", {**PLAYING, "itemId": position_in, "positionMillis": seconds * 1000}
    )
    group._playback_status_last_updated = time.time()
    item_id, generation = named.split("@")
    group._playback_metadata_data = cast("Any", _metadata(QUEUE_ID, item_id, int(generation)))
    speaker.on_player_event(None)


@pytest.mark.parametrize("handed_over", [False, True])
async def test_a_position_in_an_item_the_speaker_does_not_name_yet_is_not_in_the_one_it_names(
    home: _Household, handed_over: bool
) -> None:
    """Test the position of the next track is not read as one in the track that ends."""
    if handed_over:
        await _step_out(home)
        await home.settle()
    leader = home.bathroom if handed_over else home.kitchen
    ending, following = f"track0@{GENERATION}", f"track1@{GENERATION}"
    _report(leader, named=ending, position_in=ending, seconds=118)
    reported_at = leader.elapsed_time_last_updated

    # the speaker moved on: the position comes first, the item it is in after it
    _report(leader, named=ending, position_in=following, seconds=3)

    assert leader.current_media is not None
    assert leader.current_media.queue_item_id == "track0"
    assert leader.elapsed_time == pytest.approx(118, abs=1)
    assert leader.elapsed_time_last_updated == reported_at

    _report(leader, named=following, position_in=following, seconds=4)

    assert leader.current_media.queue_item_id == "track1"
    assert leader.elapsed_time == pytest.approx(4, abs=1)
    assert leader.elapsed_time_last_updated != reported_at


async def test_a_position_in_a_new_load_is_taken_before_the_speaker_names_an_item_of_it(
    home: _Household,
) -> None:
    """Test a seek shows at once, also on a speaker that names the new load seconds later."""
    leader = home.kitchen
    replaced, loaded = f"track0@{GENERATION}", f"track0@{GENERATION + 1}"
    _report(leader, named=replaced, position_in=replaced, seconds=20)
    leader.cloud_queue_item_generation = GENERATION + 1

    _report(leader, named=replaced, position_in=loaded, seconds=90)

    assert leader.elapsed_time == pytest.approx(90, abs=1)


async def test_a_step_out_stops_nothing_and_loads_nothing(home: _Household) -> None:
    """Test the leader only leaves its group, also once the speakers report the move themselves."""
    await _step_out(home)
    await home.settle()

    assert home.commands(KITCHEN) == ["groups.modify_group_members"]
    assert home.commands(STUDY) == []
    assert home.commands(BATHROOM) == []
    # only the report of the leader that left
    assert len(home.tasks) == 1
    assert home.kitchen._stepping_out is False
    assert home.bathroom.group_queue_id == QUEUE_ID


async def test_a_group_whose_leader_left_outside_music_assistant_plays_on(
    home: _Household,
) -> None:
    """Test the sync group follows the member the speakers moved it to, and nothing is stopped."""
    await home.window(QUEUE_ID, f"track0@{GENERATION}")
    home.kitchen.on_queue_position_served(0, "track0")
    kept = _kept(home.kitchen)

    await home.leave()
    await home.settle()

    assert all(home.commands(player_id) == [] for player_id in home.speakers)
    assert _kept(home.bathroom) == kept
    assert _kept(home.kitchen) == CLEAN
    home.sync_group.on_session_moved.assert_awaited_once_with(home.kitchen, home.bathroom)
    assert home.locked == [QUEUE_ID]
    assert home.bathroom.playback_state == PlaybackState.PLAYING
    assert home.bathroom.current_media is not None
    assert home.bathroom.current_media.queue_item_id == "track0"
    assert home.bathroom.group_members == [BATHROOM, STUDY]
    assert home.kitchen.playback_state == PlaybackState.IDLE
    assert home.kitchen.current_media is None
    assert home.kitchen.group_members == []
    assert home.kitchen._stepping_out is False
    # only the hand-over of this move
    assert len(home.tasks) == 1


async def test_the_member_the_speakers_chose_answers_for_the_queue_at_once(
    home: _Household,
) -> None:
    """Test the member is served its part of the track before any speaker was asked a thing."""
    item_id = f"track0@{GENERATION}"
    window = await home.window(QUEUE_ID, item_id)
    kept = _kept(home.kitchen)

    await home.leave()

    assert not home.tasks[0].done()
    assert home.serving(QUEUE_ID) is home.bathroom
    assert _kept(home.bathroom) == kept
    assert await home.window(QUEUE_ID, item_id) == window
    for api in home.apis.values():
        api.groups.get_groups.assert_not_awaited()


async def test_the_leader_that_left_reports_nothing_until_the_group_follows_the_member(
    home: _Household,
) -> None:
    """Test the sync group is told of its new leader before the old one reports to be idle."""
    told, go_on = asyncio.Event(), asyncio.Event()

    async def _follow(*_args: object) -> bool:
        told.set()
        await go_on.wait()
        return True

    home.sync_group.on_session_moved.side_effect = _follow

    await home.leave()
    for speaker in home.speakers.values():
        speaker.client._handle_groups_event(cast("Any", home.grouping))
    async with asyncio.timeout(2):
        await told.wait()

    # its own connection has it alone and idle by now
    assert home.kitchen.client.player.group_members == [KITCHEN]
    assert home.reports[KITCHEN] == []
    assert home.reports[BATHROOM]
    assert set(home.reports[BATHROOM]) == {(PlaybackState.PLAYING, "track0")}
    go_on.set()
    await home.settle()
    assert set(home.reports[KITCHEN]) == {(PlaybackState.IDLE, None)}
    assert set(home.reports[BATHROOM]) == {(PlaybackState.PLAYING, "track0")}


async def test_a_member_that_does_not_report_the_role_in_time_leads_the_group_all_the_same(
    home: _Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the sync group is told of the member that holds its queue once the wait is over."""
    monkeypatch.setattr(player_module, "GROUPS_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 0.1)
    home.asked_too_early[BATHROOM] = 1000

    await home.leave()
    await asyncio.gather(*home.tasks)

    assert not home.bathroom.client.player.is_coordinator
    home.sync_group.on_session_moved.assert_awaited_once_with(home.kitchen, home.bathroom)
    assert home.serving(QUEUE_ID) is home.bathroom
    assert home.commands(BATHROOM) == []
    assert home.kitchen._stepping_out is False
    assert home.kitchen.playback_state == PlaybackState.IDLE


async def test_the_member_that_took_the_group_over_is_followed_in_its_turn(
    home: _Household,
) -> None:
    """Test a second leader that leaves by itself hands the group over the same way."""
    kept = _kept(home.kitchen)
    await home.leave()
    await home.settle()

    await home.leave(BATHROOM)
    await home.settle()

    assert _kept(home.study) == kept
    assert [call.args for call in home.sync_group.on_session_moved.await_args_list] == [
        (home.kitchen, home.bathroom),
        (home.bathroom, home.study),
    ]
    assert home.study.playback_state == PlaybackState.PLAYING
    assert home.bathroom.playback_state == PlaybackState.IDLE
    assert all(home.commands(player_id) == [] for player_id in home.speakers)


@pytest.mark.parametrize(
    "state",
    [
        "gets its tracks as flac",
        "paused",
        "moved to a speaker that is no member",
        "the sync group does not follow",
    ],
)
async def test_a_move_made_outside_music_assistant_still_stops_the_group(
    make_home: Callable[..., _Household], state: str
) -> None:
    """Test only a sync group that plays a file on is followed; any other one stops as before."""
    home = make_home(
        "lossless_compressed" if state == "gets its tracks as flac" else "lossless_uncompressed"
    )
    if state == "paused":
        home.kitchen.group_controller._playback_status_data = cast(
            "Any", {**PLAYING, "playbackState": "PLAYBACK_STATE_PAUSED"}
        )
    elif state == "moved to a speaker that is no member":
        home.sync_group_members.discard(f"pl_{BATHROOM}")
    elif state == "the sync group does not follow":
        home.sync_group.on_session_moved.return_value = False

    await home.leave()
    await home.settle()

    assert home.commands(BATHROOM) == STOP
    assert home.kitchen.group_queue_id is None
    assert home.bathroom.group_queue_id is None
    assert home.kitchen._stepping_out is False
    if state != "the sync group does not follow":
        home.sync_group.on_session_moved.assert_not_awaited()
        assert len(home.tasks) == 1


async def test_the_new_coordinator_hands_the_group_over_in_its_turn(home: _Household) -> None:
    """Test a speaker that took the group over steps out of it the same way."""
    kept = _kept(home.kitchen)
    await _step_out(home)
    await home.settle()
    assert home.bathroom.can_hand_over_session
    # its connection does not get to read the group it is in once it has left
    home.apis[BATHROOM].playback.get_playback_status.side_effect = asyncio.CancelledError

    await _step_out(home, BATHROOM)
    await home.settle()

    assert _kept(home.study) == kept
    assert _kept(home.bathroom) == {**CLEAN, "generation": GENERATION + 1}
    assert home.study.playback_state == PlaybackState.PLAYING
    assert home.study.current_media is not None
    assert home.study.current_media.queue_item_id == "track0"
    assert home.bathroom.playback_state == PlaybackState.IDLE
    assert home.commands(BATHROOM) == ["groups.modify_group_members"]
    assert home.commands(STUDY) == []


@pytest.mark.parametrize(
    "asked",
    [(KITCHEN, STUDY, BATHROOM), (BATHROOM,)],
    ids=["every speaker", "the new coordinator"],
)
async def test_a_speaker_that_was_asked_too_early_is_asked_again(
    home: _Household, monkeypatch: pytest.MonkeyPatch, asked: tuple[str, ...]
) -> None:
    """Test a speaker that still answers with the group it was in does not end the hand-over."""
    monkeypatch.setattr(player_module, "GROUPS_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 1)
    home.tells_of_move = False
    for player_id in asked:
        home.asked_too_early[player_id] = 2

    await _step_out(home)
    await asyncio.gather(*home.tasks)

    assert home.bathroom.group_queue_id == QUEUE_ID
    assert _kept(home.kitchen) == CLEAN
    assert home.bathroom.client.player.is_coordinator
    assert home.bathroom.group_members == [BATHROOM, STUDY]
    assert home.kitchen.client.player.group_members == [KITCHEN]


async def test_the_old_leader_is_read_back_once_it_has_left(home: _Household) -> None:
    """Test the leader that left reports to be alone, also when a member told of the move first."""
    home.asked_too_early[KITCHEN] = 1

    await _step_out(home)
    await home.settle()

    assert home.bathroom.group_queue_id == QUEUE_ID
    assert home.kitchen.client.player.group_members == [KITCHEN]
    assert home.kitchen.group_members == []
    assert home.kitchen.playback_state == PlaybackState.IDLE


async def test_what_is_left_of_a_group_a_member_was_in_before_is_not_the_moved_group(
    home: _Household,
) -> None:
    """Test a group without players that a connection still lists names no new coordinator."""
    group_id = f"{STUDY}:399"
    leftover = {"id": group_id, "coordinatorId": STUDY, "playerIds": [], "name": "Study"}
    client = home.kitchen.client
    client._groups[group_id] = AiosonosGroup(client, cast("Any", leftover))
    home.tells_of_move = False

    await _step_out(home)

    assert home.serving(QUEUE_ID) is home.bathroom
    assert home.study.cloud_queue_id is None


async def test_a_speaker_reports_again_once_a_group_it_leads_is_read() -> None:
    """Test the event that ends the reading of a group makes the speaker report what it plays."""
    speaker, _, client = _connected_player()

    await speaker.on_attach(MagicMock())

    subscribed = {
        event
        for call in client.subscribe.call_args_list
        if call.args[0] == speaker.on_player_event
        for event in call.args[1]
    }
    assert SonosEventType.GROUP_ADDED in subscribed


async def test_a_step_out_does_not_wait_for_the_answer_of_the_leader(home: _Household) -> None:
    """Test the step-out ends once a member leads, while the leader has not answered for it."""
    home.leave_answer = asyncio.Event()

    await _step_out(home)

    assert home.serving(QUEUE_ID) is home.bathroom
    assert home.bathroom.playback_state == PlaybackState.PLAYING
    assert home.bathroom.group_members == [BATHROOM, STUDY]
    # the leader that left reports for itself as soon as its own connection has it alone
    await _until(lambda: not home.kitchen._stepping_out)
    assert home.kitchen.playback_state == PlaybackState.IDLE
    assert home.kitchen.group_members == []
    assert not home.leave_answer.is_set()


async def test_the_leader_that_left_reports_nothing_of_the_group_until_it_is_alone(
    home: _Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the old leader is quiet while its connection still names the group it led."""
    monkeypatch.setattr(player_module, "GROUPS_POLL_INTERVAL", 0.01, raising=False)
    home.leave_answer = asyncio.Event()
    home.asked_too_early[KITCHEN] = 1000

    await _step_out(home)
    for _ in range(20):
        await asyncio.sleep(0.005)

    assert home.kitchen._stepping_out
    assert home.reports[KITCHEN] == []
    # its answer ends the wait as well
    home.asked_too_early[KITCHEN] = 0
    home.leave_answer.set()
    await _until(lambda: not home.kitchen._stepping_out)
    assert home.kitchen.playback_state == PlaybackState.IDLE
    assert home.kitchen.group_members == []


@pytest.mark.parametrize(
    "failure",
    [CommandTimeout("groups.modifyGroupMembers", 10), ConnectionClosed("Connection closed")],
    ids=["no answer", "connection closed"],
)
async def test_a_leader_whose_leave_fails_did_leave_once_a_member_leads(
    home: _Household, failure: Exception
) -> None:
    """Test a leave without an answer is no failure once a member leads the group."""
    leave = cast("AsyncMock", home.apis[KITCHEN].groups.modify_group_members)
    moves = leave.side_effect

    async def _no_answer(*args: object, **kwargs: object) -> None:
        await moves(*args, **kwargs)
        raise failure

    leave.side_effect = _no_answer

    await _step_out(home)
    await asyncio.gather(*home.tasks)

    assert home.bathroom.group_queue_id == QUEUE_ID
    assert _kept(home.kitchen) == CLEAN
    assert home.kitchen._stepping_out is False


async def test_the_member_the_leader_names_takes_the_queue_before_any_speaker_lists_it(
    home: _Household, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test the speaker that leaves tells which member leads its group, ahead of the groups."""
    monkeypatch.setattr(player_module, "GROUPS_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 0.1)
    home.asked_too_early = dict.fromkeys(home.speakers, 1000)

    with caplog.at_level(logging.WARNING):
        await _step_out(home)

    assert "could not be handed" not in caplog.text
    assert home.serving(QUEUE_ID) is home.bathroom
    assert _kept(home.kitchen) == CLEAN
    assert home.bathroom.playback_state == PlaybackState.PLAYING


async def test_a_window_asked_for_before_the_hand_over_leaves_nothing_on_the_old_leader(
    home: _Household,
) -> None:
    """Test a window that is built while the queue changes hands is not kept by the old leader."""
    handed = home.kitchen._window_item_ids
    resolve = home.mass.streams.resolve_stream_url
    resolved = resolve.side_effect
    asked, go_on = asyncio.Event(), asyncio.Event()

    async def _slow(*args: object, **kwargs: object) -> str:
        asked.set()
        await go_on.wait()
        return cast("str", resolved(*args, **kwargs))

    resolve.side_effect = _slow
    window = asyncio.ensure_future(home.kitchen.build_cloud_queue_window(f"track0@{GENERATION}"))
    await asked.wait()
    resolve.side_effect = resolved

    await _step_out(home)
    go_on.set()
    await window

    assert _kept(home.kitchen) == CLEAN
    assert home.bathroom._window_item_ids is handed


async def test_a_new_coordinator_that_does_not_answer_gets_the_queue_all_the_same(
    home: _Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test another speaker tells which member leads the group when that member is silent."""
    monkeypatch.setattr(player_module, "GROUPS_REFRESH_TIMEOUT", 0.02)
    monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 0.2)
    home.tells_of_move = False
    home.silent.add(BATHROOM)

    await _step_out(home)

    assert home.serving(QUEUE_ID) is home.bathroom
    assert _kept(home.kitchen) == CLEAN


async def test_a_new_coordinator_does_not_add_the_members_it_took_over(home: _Household) -> None:
    """Test the members the new coordinator leads already are not sent to it again."""
    await _step_out(home)

    await home.bathroom.set_members(player_ids_to_add=[STUDY])

    assert home.commands(BATHROOM) == []


@pytest.mark.parametrize(("took_over", "added"), [(True, [KITCHEN]), (False, [STUDY, KITCHEN])])
async def test_only_a_coordinator_that_took_a_group_over_skips_the_members_it_leads(
    took_over: bool, added: list[str]
) -> None:
    """Test a member list is only gone by right after a hand-over, when it was just read."""
    speaker, _ = _make_player([])
    speaker._airplay_rejoin_members = set()
    speaker._taken_over_item_id = f"track0@{GENERATION}" if took_over else None
    speaker.client = MagicMock()
    speaker.client.player.is_coordinator = True
    speaker.client.player.group_members = [BATHROOM, STUDY]
    speaker._modify_group_members = AsyncMock()  # type: ignore[method-assign]

    await speaker.set_members(player_ids_to_add=[STUDY, KITCHEN])

    speaker._modify_group_members.assert_awaited_once_with(added, [])


async def test_a_new_load_ends_what_is_kept_of_the_take_over(home: _Household) -> None:
    """Test a new coordinator is a coordinator like any other once it loaded something itself."""
    await _step_out(home)
    await home.settle()
    _loads_into_a_session(cast("MagicMock", home.bathroom.group_controller))

    await home.bathroom.play_media(
        PlayerMedia(
            uri="library://track/1",
            media_type=MediaType.TRACK,
            source_id=QUEUE_ID,
            queue_item_id="track0",
            queue_session_id="session2",
        )
    )
    home.apis[BATHROOM].reset_mock()
    await home.bathroom.set_members(player_ids_to_add=[STUDY])

    assert home.commands(BATHROOM) == ["groups.modify_group_members"]


async def test_the_old_leader_plays_something_else_next_to_the_group(home: _Household) -> None:
    """Test a new play on the old leader is served under its own path, the group's untouched."""
    item_id = f"track0@{GENERATION}"
    group_window = await home.window(QUEUE_ID, item_id)
    await _step_out(home)
    await home.settle()
    _loads_into_a_session(cast("MagicMock", home.kitchen.group_controller))

    await home.kitchen.play_media(
        PlayerMedia(
            uri="library://track/9",
            media_type=MediaType.TRACK,
            source_id=KITCHEN_QUEUE,
            queue_item_id="own0",
            queue_session_id="own_session",
        )
    )

    assert home.serving(QUEUE_ID) is home.bathroom
    assert await home.window(QUEUE_ID, item_id) == group_window
    assert home.serving(KITCHEN) is home.kitchen
    own_window = await home.window(KITCHEN)
    assert [item["track"]["mediaUrl"] for item in own_window["items"]] == [
        f"{KITCHEN}/{i}" for i in range(2)
    ]
    # under ids of its own: a report on an item of the group is not about this speaker
    assert [item["id"] for item in own_window["items"]] == [
        f"own{i}@{GENERATION + 2}" for i in range(2)
    ]


@pytest.mark.parametrize("state", ["gets its tracks as flac", "leads a group made by hand"])
async def test_a_leader_that_can_not_hand_over_leaves_its_group_as_before(
    make_home: Callable[..., _Household], state: str
) -> None:
    """Test such a leader only leaves, and the move is handled as one the speakers made."""
    home = make_home(
        "lossless_compressed" if state == "gets its tracks as flac" else "lossless_uncompressed"
    )
    if state == "leads a group made by hand":
        home.play(KITCHEN_QUEUE, "own0", group_queue=False)

    await _step_out(home)

    home.apis[KITCHEN].groups.modify_group_members.assert_awaited_once_with(
        KITCHEN_GROUP, [], [KITCHEN]
    )
    for api in home.apis.values():
        api.groups.get_groups.assert_not_awaited()
    assert home.bathroom.cloud_queue_id is None
    await home.settle()
    assert home.kitchen.cloud_queue_id is None
    if state == "gets its tracks as flac":
        # the queue of a group player stops with the group
        assert home.commands(BATHROOM) == STOP
        home.queues.transfer_queue.assert_not_awaited()
    else:
        # the queue of the leader goes to the player that leads the group now
        home.queues.transfer_queue.assert_awaited_once_with(
            KITCHEN_QUEUE, f"pl_{BATHROOM}", auto_play=False
        )
        assert home.commands(BATHROOM) == []


@pytest.mark.parametrize("failure", ["no speaker reports the move", "a speaker that is not ours"])
async def test_a_queue_stays_where_it_is_when_no_member_is_known_to_lead_the_group(
    home: _Household,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """Test the hand-over is given up on in time, and the queue is left with the old leader."""
    monkeypatch.setattr(player_module, "GROUPS_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 0.1)
    if failure == "no speaker reports the move":
        home.tells_of_move = False
        home.asked_too_early = dict.fromkeys(home.speakers, 1000)
    else:
        del home.speakers[BATHROOM]
    kept = _kept(home.kitchen)
    started = asyncio.get_running_loop().time()

    with caplog.at_level(logging.WARNING):
        await _step_out(home)

    assert asyncio.get_running_loop().time() - started < 1
    assert "could not be handed" in caplog.text
    assert _kept(home.kitchen) == kept
    assert home.kitchen._stepping_out is False
