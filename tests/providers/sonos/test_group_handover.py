"""Tests for a Sonos speaker that leaves the sync group it leads while the group plays on."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
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
    _set_output_codec,
)

HOUSEHOLD = "Sonos_household"
# Kitchen leads; Bathroom joined it first and Study last, which Sonos lists the other way round
KITCHEN = "RINCON_F0F6C100000101400"
STUDY = "RINCON_38420B00000201400"
BATHROOM = "RINCON_48A6B800000301400"
KITCHEN_GROUP = f"{KITCHEN}:400"
BATHROOM_GROUP = f"{BATHROOM}:401"
KITCHEN_ALONE = f"{KITCHEN}:402"
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


def _metadata(queue_id: str, item_id: str) -> dict[str, Any]:
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
            "id": f"{item_id}@{GENERATION}",
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

    def __init__(self, monkeypatch: pytest.MonkeyPatch, codec: str = "wav") -> None:
        """
        Create the household, with the group playing the first track of the group's queue.

        :param monkeypatch: The fixture that restores what the household patches.
        :param codec: The codec the speakers get their streams in.
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
        # what Music Assistant started in the background
        self.tasks: list[asyncio.Task[None]] = []
        # what every speaker answers getGroups with
        self.grouping = SYNCED
        self.apis = {player_id: self._api() for player_id in (KITCHEN, STUDY, BATHROOM)}
        self.speakers = {player_id: self._speaker(player_id, codec) for player_id in self.apis}
        self.apis[KITCHEN].groups.modify_group_members.side_effect = self._kitchen_leaves
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

    async def _kitchen_leaves(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        """Take Kitchen out of its group, as the speakers do: the others play on."""
        self.grouping = MOVED
        self.apis[BATHROOM].playback.get_playback_status.return_value = dict(PLAYING)
        self.apis[BATHROOM].playback_metadata.get_metadata_status.return_value = dict(
            self.kitchen.group_controller.playback_metadata
        )
        # the former coordinator hears of the move at once
        self.kitchen.client._handle_group_coordinator_changed(
            KITCHEN_GROUP,
            cast(
                "Any",
                {
                    "_objectType": "groupCoordinatorChanged",
                    "groupStatus": "GROUP_STATUS_MOVED",
                    "playerId": BATHROOM,
                },
            ),
        )
        return {"_objectType": "groupInfo"}

    def _api(self) -> AsyncMock:
        """Create the api of a speaker, which is idle in every group it is asked about."""
        api = AsyncMock()
        api.groups.get_groups.side_effect = lambda *_args, **_kwargs: self.grouping
        api.groups.modify_group_members.return_value = {"_objectType": "groupInfo"}
        api.playback.get_playback_status.return_value = dict(IDLE)
        api.playback_metadata.get_metadata_status.return_value = dict(NOTHING_LOADED)
        api.group_volume.get_volume.return_value = {"volume": 20, "muted": False, "fixed": False}
        return api

    def _speaker(self, player_id: str, codec: str) -> SonosPlayer:
        """Create a connected speaker that reports its state, around a real aiosonos client."""
        speaker, _ = _make_player([])
        _set_output_codec(speaker, codec)
        streams = cast("MagicMock", speaker.mass).streams
        streams.resolve_stream_url = AsyncMock(
            side_effect=lambda url_id, _media, queue_index: f"{url_id}/{queue_index}"
        )
        self.mass.streams = streams
        self.mass.player_queues = streams.mass.player_queues = self.queues
        # no queue is a sync group's whose stream the speaker renders, unless it is told so
        self.mass.players.get_player.return_value = None
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
        speaker._stepping_out = False
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
        speaker.update_state = MagicMock()  # type: ignore[method-assign, misc]
        # what on_attach subscribes to
        client.subscribe(
            speaker.on_player_event, (SonosEventType.GROUP_UPDATED, SonosEventType.PLAYER_UPDATED)
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

    def _make(codec: str = "wav") -> _Household:
        homes.append(_Household(monkeypatch, codec))
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


async def _step_out(home: _Household) -> None:
    """Let the sync group take its leader out, as its change of leader does."""
    await home.kitchen.set_members(player_ids_to_remove=[KITCHEN])


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
    home = make_home("flac" if state == "gets its tracks as flac" else "wav")
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
    assert _kept(home.kitchen) == {
        "cloud_queue_id": None,
        "group_queue_id": None,
        "loaded_session_id": None,
        "version": 12.5,
        "generation": GENERATION + 1,
        "window_item_ids": {},
        "served_bindings": {},
    }
    assert home.study.cloud_queue_id is None


async def test_the_speakers_report_what_they_play_once_the_leader_stepped_out(
    home: _Household,
) -> None:
    """Test the new coordinator reports the track of the group's queue, the old leader nothing."""
    await _step_out(home)

    for speaker in (home.kitchen, home.study, home.bathroom):
        cast("MagicMock", speaker.update_state).assert_called()
    assert home.bathroom.playback_state == PlaybackState.PLAYING
    assert home.bathroom.active_source is None
    assert home.bathroom.current_media is not None
    assert home.bathroom.current_media.queue_item_id == "track0"
    assert home.bathroom.group_members == [BATHROOM, STUDY]
    assert home.study.synced_to == BATHROOM
    assert home.kitchen.playback_state == PlaybackState.IDLE
    assert home.kitchen.active_source is None
    assert home.kitchen.current_media is None
    assert home.kitchen.group_members == []


async def test_the_group_path_answers_the_same_from_the_new_coordinator(home: _Household) -> None:
    """Test the speakers read the same window, and are heard on, before and after the move."""
    item_id = f"track0@{GENERATION}"
    before = await home.window(QUEUE_ID, item_id)
    home.kitchen.on_queue_position_served(0, "track0")
    await home.report_position(QUEUE_ID, item_id, 20)
    assert home.serving(QUEUE_ID) is home.kitchen
    assert home.kitchen.elapsed_time == 20

    await _step_out(home)

    assert home.serving(QUEUE_ID) is home.bathroom
    assert await home.window(QUEUE_ID, item_id) == before
    assert [item["id"] for item in before["items"]] == [f"track{i}@{GENERATION}" for i in range(3)]
    assert [item["track"]["mediaUrl"] for item in before["items"]] == [
        f"{QUEUE_ID}/{i}" for i in range(3)
    ]
    assert before["queueVersion"] == "12.5"
    await home.report_position(QUEUE_ID, item_id, 30)
    assert home.bathroom.elapsed_time == 30
    assert home.kitchen.elapsed_time != 30


async def test_a_step_out_stops_nothing_and_loads_nothing(home: _Household) -> None:
    """Test the leader only leaves its group, also once the speakers report the move themselves."""
    await _step_out(home)
    await home.settle()

    assert home.commands(KITCHEN) == ["groups.modify_group_members"]
    assert home.commands(STUDY) == []
    assert home.commands(BATHROOM) == []
    assert home.tasks == []
    assert home.kitchen._stepping_out is False
    assert home.bathroom.group_queue_id == QUEUE_ID


async def test_a_move_made_outside_music_assistant_still_stops_the_group(home: _Household) -> None:
    """Test a group that the speakers moved themselves is not played on by Music Assistant."""
    await home.apis[KITCHEN].groups.modify_group_members()
    await home.settle()

    assert len(home.tasks) == 1
    assert home.commands(BATHROOM) == STOP
    assert home.kitchen.group_queue_id is None
    assert home.bathroom.group_queue_id is None


async def test_a_new_coordinator_does_not_add_the_members_it_took_over(home: _Household) -> None:
    """Test the members the new coordinator leads already are not sent to it again."""
    await _step_out(home)

    await home.bathroom.set_members(player_ids_to_add=[STUDY])

    assert home.commands(BATHROOM) == []


async def test_a_coordinator_adds_only_the_members_it_does_not_lead_yet() -> None:
    """Test a member that is new to the group is added, next to one that is in it already."""
    speaker, _ = _make_player([])
    speaker._airplay_rejoin_members = set()
    speaker.client = MagicMock()
    speaker.client.player.is_coordinator = True
    speaker.client.player.group_members = [BATHROOM, STUDY]
    speaker._modify_group_members = AsyncMock()  # type: ignore[method-assign]

    await speaker.set_members(player_ids_to_add=[STUDY, KITCHEN])

    speaker._modify_group_members.assert_awaited_once_with([KITCHEN], [])


async def test_the_old_leader_plays_something_else_next_to_the_group(home: _Household) -> None:
    """Test a new play on the old leader is served under its own path, the group's untouched."""
    item_id = f"track0@{GENERATION}"
    group_window = await home.window(QUEUE_ID, item_id)
    await _step_out(home)
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
    home = make_home("flac" if state == "gets its tracks as flac" else "wav")
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


@pytest.mark.parametrize("failure", ["no role reported", "unknown coordinator"])
async def test_a_queue_stays_where_it_is_when_the_new_coordinator_can_not_take_it(
    home: _Household,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """Test nothing moves to a speaker that is not ours, or does not report to lead the group."""
    if failure == "no role reported":
        # its own connection keeps reporting the group it was in
        home.apis[BATHROOM].groups.get_groups.side_effect = lambda *_args: SYNCED
        monkeypatch.setattr(player_module, "MOVED_GROUP_TIMEOUT", 0.05)
    else:
        del home.speakers[BATHROOM]
    kept = _kept(home.kitchen)

    with caplog.at_level(logging.WARNING):
        await _step_out(home)

    assert "could not be handed" in caplog.text
    assert _kept(home.kitchen) == kept
    assert home.kitchen._stepping_out is False
