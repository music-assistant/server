"""
Tests for a play request on a grouped player whose first item cannot be loaded.

Player "a" is synced to player "b", or a dynamic or static member of group player "g", and its
play_media_overrides_group setting is on: a play request on a's own queue loads the first item
on that queue and only then takes a out of its group. While the owner of the group plays a source
that allows one stream, a request for that source on a fails to load while a still plays the
group's stream. The failed start must not stop a's device, which would silence the group.

The real queue and players controllers, the real audio buffer and the real source-slot code run;
only the source audio, the stream details lookup and the devices are fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderType,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

import music_assistant.controllers.player_queues.stream_feeder as feeder_mod
import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.constants import CONF_PLAY_MEDIA_OVERRIDES_GROUP
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import (
    MusicProvider,
    ProviderStreamHeldError,
    ProviderStreamLimitError,
)
from music_assistant.models.player import PlayerMedia
from tests.common import MockPlayer, MockProvider, grouping_player_controller

INSTANCE = "svc--one"
PCM = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)
KINDS = ["synced", "dynamic", "static"]
OWNER = {"synced": "b", "dynamic": "g", "static": "g"}
# the source of an item with this id prefix is gone
GONE = "gone"


class _OneStream(MusicProvider):
    """A music provider that allows one concurrent stream."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return the one stream the provider allows."""
        return 1


def _provider() -> MusicProvider:
    """Build the single-stream provider."""
    manifest = MagicMock(domain="svc", type=ProviderType.MUSIC)
    manifest.name = "Svc"
    config = MagicMock(instance_id=INSTANCE)
    config.name = "Svc"
    config.get_value.return_value = "GLOBAL"
    return _OneStream(MagicMock(), manifest, config)


def _track(item_id: str) -> Track:
    """Build a track on the single-stream provider."""
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="x", provider=INSTANCE, name="X", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="svc", provider_instance=INSTANCE)
        },
    )


class _Setup:
    """Players a, b, c and group g, their queues, the provider and the streams side."""

    def __init__(self, kind: str | None, holder_realtime: bool) -> None:
        """
        Build the players with the owner of a's group playing an item of the provider.

        :param kind: How a is captured: synced (to b), dynamic or static (member of g), or None
            for an ungrouped a that plays on its own, as b does.
        :param holder_realtime: Whether the owner's item is a realtime source.
        """
        self.kind = kind
        self.owner = OWNER[kind] if kind else "b"
        self.holder_realtime = holder_realtime
        self.device_stops: list[str] = []
        self.device_plays: list[str] = []
        self.tasks: list[asyncio.Future[Any]] = []
        self.provider = _provider()
        players = grouping_player_controller("a", "b", "c")
        self.players = players
        mass = cast("MagicMock", players.mass)
        self.mass = mass
        mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda _pid, key, default=None: (
                True if key == CONF_PLAY_MEDIA_OVERRIDES_GROUP else default
            )
        )
        mass.config.get_raw_core_config_value = MagicMock(
            side_effect=lambda _conf, _key, default=None: default
        )
        mass.get_provider = Mock(
            side_effect=lambda inst, **_kwargs: self.provider if inst == INSTANCE else None
        )
        mass.create_task = Mock(side_effect=self._create_task)
        mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
        mass.music.get_item_by_uri = AsyncMock(
            side_effect=lambda uri: _track(uri.rsplit("/", 1)[-1])
        )
        mass.music.mark_item_played = AsyncMock()
        mass.music.check_item_playable_for_user = Mock()
        mass.music.get_playback_speed = AsyncMock(return_value=1.0)
        mass.streams.audio_analysis.start_analysis = AsyncMock()
        group = MockPlayer(
            MockProvider("group", instance_id="group", mass=mass),
            "g",
            "Group G",
            player_type=PlayerType.GROUP,
        )
        group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        players._players["g"] = group
        group.set_initialized()
        for player in players._players.values():
            player._attr_playback_state = PlaybackState.IDLE
        owner = players._players[self.owner]
        owner._attr_active_source = self.owner
        owner._attr_playback_state = PlaybackState.PLAYING
        if kind == "synced":
            owner._attr_group_members = ["b", "a", "c"]
        elif kind is not None:
            group._attr_powered = True
            group._attr_group_members = ["a", "b", "c"]
            group._attr_static_group_members = ["a", "b", "c"] if kind == "static" else []
        if kind is not None:
            for player_id in "abc":
                players._players[player_id]._attr_playback_state = PlaybackState.PLAYING
        else:
            players._players["a"]._attr_active_source = "a"
            players._players["a"]._attr_playback_state = PlaybackState.PLAYING
        self._stub_devices()
        self.queues = self._queue_controller()
        mass.player_queues = self.queues
        self.audio = self._streams_audio()
        mass.streams.audio = self.audio
        self.refresh()
        if kind is not None:
            assert self.owner_of("a") == self.owner

    def owner_of(self, player_id: str) -> str:
        """Return the id of the player that owns the given player's playback."""
        player = self.players._players[player_id]
        return self.players._resolve_playback_owner(player).player_id

    def refresh(self) -> None:
        """Derive every player's state again, the owner first."""
        for _ in range(2):
            for player in sorted(self.players._players.values(), key=lambda p: p.player_id != "g"):
                player._cache.clear()
                player.update_state(force_update=True, signal_event=False)

    async def start_owner(self) -> None:
        """Let the owner's queue play an item whose buffer takes the provider's one stream."""
        item = QueueItem(
            queue_id=self.owner,
            queue_item_id="held",
            name="Held",
            duration=180,
            media_item=_track("held"),
        )
        data = self.queues._queue_data[self.owner]
        data.session_id = f"session-{self.owner}"
        item.streamdetails = StreamDetails(
            provider=INSTANCE,
            item_id="held",
            audio_format=PCM,
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            duration=180,
            queue_id=self.owner,
            is_realtime=self.holder_realtime,
            queue_session_id=data.session_id,
        )
        data.items = [item]
        data.queue.state = PlaybackState.PLAYING
        data.queue.current_index = 0
        data.queue.current_item = item
        self.holder_buffer = await AudioBuffer.get_buffer(
            self.mass, item.streamdetails, wait_ready=True, source_wait_timeout=0
        )
        assert not self.provider.has_available_stream_slot

    async def cleanup(self) -> None:
        """Clear every buffer and cancel the tasks the test started."""
        for data in self.queues._queue_data.values():
            for item in data.items:
                if item.streamdetails and item.streamdetails.buffer:
                    await item.streamdetails.buffer.clear()
        await self.holder_buffer.clear()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _create_task(self, target: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        """Run a coroutine as a task; anything else is done at once."""
        if asyncio.iscoroutine(target):
            task = asyncio.ensure_future(target)
            self.tasks.append(task)
            return task
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    def _stub_devices(self) -> None:
        """Record what the devices are told to do instead of reaching any of them."""
        players = self.players

        async def _played(player_id: str, _media: PlayerMedia) -> None:
            self.device_plays.append(player_id)
            player = players._players[player_id]
            player._attr_playback_state = PlaybackState.PLAYING
            player._attr_active_source = player_id
            self.refresh()

        async def _set_members(
            parent: Any, _to_add: Any = None, player_ids_to_remove: Any = None
        ) -> None:
            await parent.set_members(None, player_ids_to_remove)
            for player_id in player_ids_to_remove or []:
                players._players[player_id]._attr_playback_state = PlaybackState.IDLE
            self.refresh()

        players._handle_play_media = AsyncMock(side_effect=_played)  # type: ignore[method-assign]
        players._handle_set_members = AsyncMock(side_effect=_set_members)  # type: ignore[method-assign]
        players._handle_cmd_power = AsyncMock()  # type: ignore[method-assign]

        for player_id, player in players._players.items():

            async def _stop(player_id: str = player_id) -> None:
                self.device_stops.append(player_id)
                stopped = players._players[player_id]
                stopped._attr_playback_state = PlaybackState.IDLE
                if player_id == "g":
                    # a static group that stops lets its members go
                    if self.kind == "static":
                        stopped._attr_group_members = []
                    stopped._attr_active_source = None
                self.refresh()

            player.stop = _stop  # type: ignore[method-assign]

        @contextlib.asynccontextmanager
        async def _no_wait(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
            yield

        players.wait_for_player_update = _no_wait  # type: ignore[method-assign]

    def _streams_audio(self) -> StreamsAudio:
        """Build the real streams audio side, over a fake source of the provider."""
        audio = StreamsAudio(self.mass)
        audio.get_stream_details = AsyncMock(side_effect=self._stream_details)  # type: ignore[method-assign]
        audio._get_media_stream = self._source  # type: ignore[method-assign]
        audio._get_mapping_providers = Mock(return_value=[self.provider])  # type: ignore[method-assign]
        audio._has_alternative_match_providers = Mock(return_value=False)  # type: ignore[method-assign]
        audio.get_stream_providers = Mock(return_value=[self.provider])  # type: ignore[method-assign]
        audio.has_alternative_match_providers = Mock(return_value=False)  # type: ignore[method-assign]
        return audio

    def _queue_controller(self) -> Any:
        """Build the queue controller over the players, with an empty queue for each."""
        ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
        ctrl.logger = Mock()
        ctrl.mass = self.mass
        ctrl._queue_data = {
            queue_id: PlayerQueueData(
                queue=PlayerQueue(
                    queue_id=queue_id,
                    active=True,
                    display_name=queue_id.upper(),
                    available=True,
                    items=0,
                )
            )
            for queue_id in ("a", "b", "c", "g")
        }
        ctrl.signal_update = Mock()
        ctrl.on_player_update = Mock()
        ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)
        ctrl._managed_pool = Mock()
        ctrl._smart_shuffle = Mock()
        ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
        ctrl._media_resolver = Mock()
        ctrl._media_resolver._resolve_media_items = AsyncMock(
            side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
        )
        return ctrl

    async def _stream_details(
        self,
        queue_item: QueueItem,
        *_args: Any,
        excluded_provider_instances: Any = None,
        **_kwargs: Any,
    ) -> StreamDetails:
        """Return the details of an item on the provider, the same kind of source as held."""
        item_id = queue_item.media_item.item_id if queue_item.media_item else ""
        if item_id.startswith(GONE) or (
            excluded_provider_instances and INSTANCE in excluded_provider_instances
        ):
            raise MediaNotFoundError(f"{item_id} is gone")
        if queue_item.streamdetails is not None:
            return queue_item.streamdetails
        return StreamDetails(
            provider=INSTANCE,
            item_id=item_id,
            audio_format=PCM,
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            duration=180,
            queue_id=queue_item.queue_id,
            is_realtime=self.holder_realtime,
        )

    async def _source(self, *_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        """Deliver a little audio, then keep the source (and its stream) open."""
        for _ in range(3):
            yield b"\x00" * PCM.pcm_sample_size
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def _no_playback_sources() -> Any:
    """Leave out the active playback sources a real server would report."""
    with (
        patch.object(audio_mod, "playback_sources", AsyncMock(return_value=(None, []))),
        patch.object(feeder_mod, "visible_playback_sources", Mock(return_value=None)),
    ):
        yield


@contextlib.asynccontextmanager
async def _setup(kind: str | None, holder_realtime: bool) -> AsyncIterator[_Setup]:
    """Yield a setup whose owner is playing, cleaned up afterwards."""
    setup = _Setup(kind, holder_realtime)
    # a start waits this long for the held stream before it gives up
    setup.audio.get_audio_buffer = functools.partial(  # type: ignore[method-assign]
        setup.audio.get_audio_buffer, capacity_wait_timeout=0.2
    )
    await setup.start_owner()
    try:
        yield setup
    finally:
        await setup.cleanup()


def _shuffled_batch() -> list[Track]:
    return [_track("t1"), _track("t2"), _track("t3")]


# the request on a, the error its refused start raises, and whether the held item is realtime
REFUSED = {
    "shuffled-batch-realtime-holder": (_shuffled_batch, True, ProviderStreamHeldError, True),
    "timed-out-wait-non-realtime-holder": (
        lambda: _track("t1"),
        None,
        ProviderStreamLimitError,
        False,
    ),
}


def _assert_group_untouched(setup: _Setup) -> None:
    """Assert that a is still in its group and the group plays on as before."""
    assert setup.device_stops == []
    assert setup.owner_of("a") == setup.owner
    owner_data = setup.queues._queue_data[setup.owner]
    assert owner_data.session_id == f"session-{setup.owner}"
    assert owner_data.queue.state == PlaybackState.PLAYING
    assert setup.holder_buffer.is_buffering
    assert not setup.provider.has_available_stream_slot
    assert setup.queues._queue_data["a"].session_id is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("refusal", list(REFUSED))
async def test_a_refused_start_keeps_the_group_playing(kind: str, refusal: str) -> None:
    """A start that cannot get the held stream raises as before and stops no device."""
    media, shuffle, error, holder_realtime = REFUSED[refusal]
    async with _setup(kind, holder_realtime) as setup:
        with pytest.raises(error):
            await setup.queues.play_media("a", media(), QueueOption.REPLACE, shuffle=shuffle)
        await asyncio.sleep(0)

        _assert_group_untouched(setup)
        assert setup.device_plays == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("count", "message"),
    [(3, "no more tracks available"), (6, "No playable item found to start playback")],
    ids=["last-item-fails", "load-attempts-run-out"],
)
async def test_an_unplayable_start_keeps_the_group_playing(
    kind: str, count: int, message: str
) -> None:
    """A start whose items are all gone raises as before and stops no device."""
    async with _setup(kind, holder_realtime=False) as setup:
        with pytest.raises(MediaNotFoundError, match=message):
            await setup.queues.play_media(
                "a", [_track(f"{GONE}{i}") for i in range(count)], QueueOption.REPLACE
            )
        await asyncio.sleep(0)

        _assert_group_untouched(setup)


@pytest.mark.parametrize(
    ("media", "shuffle", "error", "holder_realtime"),
    [
        (_shuffled_batch, True, ProviderStreamHeldError, True),
        (lambda: [_track(f"{GONE}{i}") for i in range(3)], None, MediaNotFoundError, False),
    ],
    ids=["refused", "unplayable"],
)
async def test_a_failed_start_on_an_ungrouped_player_stops_it(
    media: Callable[[], list[Track]],
    shuffle: bool | None,
    error: type[Exception],
    holder_realtime: bool,
) -> None:
    """A player that plays on its own is stopped when its start fails."""
    async with _setup(None, holder_realtime) as setup:
        with pytest.raises(error):
            await setup.queues.play_media("a", media(), QueueOption.REPLACE, shuffle=shuffle)
        await asyncio.sleep(0)

        assert setup.device_stops == ["a"]
        assert setup.queues._queue_data["a"].session_id is None
        assert setup.queues._queue_data["b"].session_id == "session-b"


@pytest.mark.parametrize("kind", KINDS)
async def test_the_request_plays_once_the_holder_is_stopped(kind: str) -> None:
    """After the refusal the holder is stopped, and the same request takes a out and plays."""
    async with _setup(kind, holder_realtime=True) as setup:
        with pytest.raises(ProviderStreamHeldError) as exc_info:
            await setup.queues.play_media("a", _shuffled_batch(), QueueOption.REPLACE, shuffle=True)
        assert exc_info.value.data is not None
        holder = exc_info.value.data["queue_id"]
        assert holder == setup.owner

        await setup.queues.stop(holder)
        async with asyncio.timeout(1):
            while not setup.provider.has_available_stream_slot:
                await asyncio.sleep(0.01)
        await setup.queues.play_media("a", _shuffled_batch(), QueueOption.REPLACE, shuffle=True)

        assert setup.device_plays == ["a"]
        assert setup.owner_of("a") == "a"
        assert setup.queues._queue_data["a"].session_id is not None
