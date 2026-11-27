"""
Tests for a paused queue handing its provider stream slot to another queue.

A queue paused on a player that really pauses keeps its session and its source buffers, and
with them a provider's stream slot, until the pause watcher stops it. A provider that allows
one stream would make a play on any other player fail in that window.

A playing queue keeps its slot. When its source is realtime, that slot stays taken until the
item ends, so a start on another queue is refused at once instead of waiting its budget out.
A play command is refused that way before its tracks are fetched, unless one of the item's own
sources has a free slot.

The owner of an account goes first: a queue that plays it for someone else gives its realtime
slot up to the owner's playback, and goes on from another source or stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.access import ProviderAccess
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    Playlist,
    ProviderMapping,
    SoundEffect,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.player_queues.stream_feeder as feeder_mod
import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.provider_access import PlaybackSources
from music_assistant.models.music_provider import (
    MusicProvider,
    ProviderStreamHeldError,
    ProviderStreamLimitError,
)
from tests.common import set_music_source_access

INSTANCE = "spotify--one"
SECOND_INSTANCE = "spotify--two"
PAUSED_QUEUE = "living_room"
STARTING_QUEUE = "kitchen"
BORROWING_QUEUE = "office"
OWNER = "owner"
BORROWER = "housemate"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16)


class _SingleStreamProvider(MusicProvider):
    """Streaming provider with one source slot."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


class _TwoStreamProvider(MusicProvider):
    """Streaming provider with two source slots."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return two source slots."""
        return 2


def _provider(
    provider_cls: type[MusicProvider] = _SingleStreamProvider, instance: str = INSTANCE
) -> MusicProvider:
    """Construct a provider of the given class, under the shared instance id by default."""
    manifest = MagicMock(domain="spotify", type=ProviderType.MUSIC)
    manifest.name = "Spotify"
    config = MagicMock(instance_id=instance)
    config.name = "Spotify"
    config.get_value.return_value = "GLOBAL"
    return provider_cls(MagicMock(), manifest, config)


def _queue_item(queue_id: str, realtime: bool = False) -> QueueItem:
    """Build a queue item of the provider, with its stream details resolved."""
    mapping = ProviderMapping(
        item_id=f"track-{queue_id}",
        provider_domain="spotify",
        provider_instance=INSTANCE,
        audio_format=PCM_FORMAT,
    )
    item = QueueItem(
        queue_id=queue_id,
        queue_item_id=f"item-{queue_id}",
        name=f"Track on {queue_id}",
        duration=600,
        media_item=SoundEffect(
            item_id=mapping.item_id,
            provider=INSTANCE,
            name=f"Track on {queue_id}",
            provider_mappings={mapping},
        ),
    )
    item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id=mapping.item_id,
        audio_format=PCM_FORMAT,
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.CUSTOM,
        duration=600,
        queue_id=queue_id,
        is_realtime=realtime,
    )
    return item


async def _endless_source(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    """Deliver a few seconds of audio, then keep the source open like a track far from its end."""
    for _ in range(3):
        yield b"\x00" * PCM_FORMAT.pcm_sample_size
    await asyncio.Event().wait()


async def _finite_source(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    """Deliver a few seconds of audio and end, like a track whose audio has fully arrived."""
    for _ in range(3):
        yield b"\x00" * PCM_FORMAT.pcm_sample_size


class _Rig:
    """A queue controller and the streams side of it, sharing one single-slot provider."""

    def __init__(self) -> None:
        self.provider = _provider()
        self.other_providers: dict[str, MusicProvider] = {}
        self.players: dict[str, SimpleNamespace] = {}
        self.tasks: list[asyncio.Future[Any]] = []
        mass = MagicMock()
        mass.get_provider.side_effect = lambda instance, **_kwargs: (
            self.provider if instance == INSTANCE else self.other_providers.get(instance)
        )
        mass.config.get_raw_core_config_value.side_effect = lambda _core, _key, default: default
        mass.create_task.side_effect = self._create_task
        mass.players.get_player.side_effect = self.players.get
        self.stop_device = AsyncMock(side_effect=self._device_stopped)
        mass.players._handle_cmd_stop = self.stop_device
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_owners: dict[str, asyncio.Task[Any] | None] = {}
        mass.players.get_group_and_player_lock = self.playback_lock
        self.mass = mass
        self.queues = PlayerQueuesController.__new__(PlayerQueuesController)
        self.queues.mass = mass
        self.queues.logger = MagicMock()
        self.queues._queue_data = {}
        self.queues.signal_update = MagicMock()  # type: ignore[method-assign]
        self.queues.on_player_update = MagicMock()  # type: ignore[method-assign]
        mass.player_queues = self.queues
        self.audio = StreamsAudio(mass)
        self.audio._get_media_stream = _endless_source  # type: ignore[method-assign]
        mass.streams.audio = self.audio

    def add_queue(self, queue_id: str, state: PlaybackState, realtime: bool = False) -> QueueItem:
        """Register a queue and its player in the given state, holding one item."""
        item = _queue_item(queue_id, realtime)
        queue = PlayerQueue(
            queue_id=queue_id,
            active=state != PlaybackState.IDLE,
            display_name=queue_id,
            available=True,
            items=1,
            state=state,
            current_index=0,
            current_item=item,
        )
        self.queues._queue_data[queue_id] = PlayerQueueData(
            queue=queue, items=[item], session_id=f"session-{queue_id}"
        )
        self.players[queue_id] = SimpleNamespace(
            player_id=queue_id,
            type=PlayerType.PLAYER,
            playback_state=state,
            active_source=queue_id,
            extra_data={},
            # a shared speaker: its protocol is no private client player
            attached_protocols={f"{queue_id}_device": SimpleNamespace(private=False)},
            offline_by_power_control=False,
        )
        return item

    async def fill(self, item: QueueItem) -> AudioBuffer:
        """Start the item's source buffer, which takes the provider's slot."""
        assert item.streamdetails is not None
        item.streamdetails.queue_session_id = self.queues._queue_data[item.queue_id].session_id
        return await AudioBuffer.get_buffer(
            self.mass,
            item.streamdetails,
            wait_ready=True,
            source_wait_timeout=0,
            on_created=partial(self.queues.register_audio_buffer, item),
        )

    @contextlib.asynccontextmanager
    async def playback_lock(self, player_id: str) -> AsyncIterator[None]:
        """Hold a player's playback lock, re-entrant within one task like the real one."""
        task = asyncio.current_task()
        if self._lock_owners.get(player_id) is task:
            yield
            return
        async with self._locks.setdefault(player_id, asyncio.Lock()):
            self._lock_owners[player_id] = task
            try:
                yield
            finally:
                self._lock_owners[player_id] = None

    def _create_task(self, target: Awaitable[Any], **_kwargs: Any) -> asyncio.Future[Any]:
        """Run a task the controller schedules, keeping it so a test can wait for it."""
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        return task

    async def _device_stopped(self, queue_id: str) -> None:
        """Let the player report idle once it was told to stop, and its queue follow it."""
        self.players[queue_id].playback_state = PlaybackState.IDLE
        self.queues._queue_data[queue_id].queue.state = PlaybackState.IDLE


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> _Rig:
    """Build the rig with every music source open to the queues' playback."""
    for module in (audio_mod, feeder_mod):
        monkeypatch.setattr(
            module, "playback_sources", AsyncMock(return_value=PlaybackSources(None, []))
        )
    return _Rig()


async def test_a_paused_queue_hands_its_slot_to_a_start_on_another_queue(rig: _Rig) -> None:
    """The paused queue is stopped for real, and the other queue takes the only slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    assert not rig.provider.has_available_stream_slot
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )

    assert buffer is not None
    assert buffer.is_buffering
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is None
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_paused_player_that_cannot_be_reached_still_hands_over_its_slot(
    rig: _Rig,
) -> None:
    """The paused queue's session is torn down anyway, so its player's failure stays its own."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.stop_device.side_effect = PlayerUnavailableError("gone")
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )

    assert buffer is not None
    assert buffer.is_buffering
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is None
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_stop_that_ended_no_session_reports_no_freed_slot(rig: _Rig) -> None:
    """Only a paused queue whose session really ended counts as a handed over slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.queues._handle_stop = AsyncMock(side_effect=RuntimeError("broken"))  # type: ignore[method-assign]

    assert not await rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE)

    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is not None
    await paused_buffer.clear()


async def test_play_after_a_failed_stop_starts_the_queue_again(rig: _Rig) -> None:
    """A player still paused on the stream the handover ended is resumed, not unpaused."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    await rig.fill(paused_item)
    rig.stop_device.side_effect = PlayerUnavailableError("gone")
    assert await rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE)
    assert rig.players[PAUSED_QUEUE].playback_state == PlaybackState.PAUSED
    rig.queues.resume = AsyncMock()  # type: ignore[method-assign]

    await rig.queues._handle_play(PAUSED_QUEUE)

    rig.queues.resume.assert_awaited_once_with(PAUSED_QUEUE)


@pytest.mark.parametrize(
    ("session_id", "player_state", "active_source", "expected"),
    [
        (None, PlaybackState.PAUSED, PAUSED_QUEUE, True),
        ("session-living_room", PlaybackState.PAUSED, PAUSED_QUEUE, False),
        (None, PlaybackState.PAUSED, "spotify_connect", False),
        (None, PlaybackState.IDLE, PAUSED_QUEUE, False),
    ],
    ids=["stream-ended", "stream-still-there", "paused-on-another-source", "player-idle"],
)
def test_only_a_player_paused_on_an_ended_stream_has_lost_it(
    rig: _Rig,
    session_id: str | None,
    player_state: PlaybackState,
    active_source: str,
    expected: bool,
) -> None:
    """Only the queue's own stream, paused on the player after the queue ended it, is lost."""
    rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    rig.queues._queue_data[PAUSED_QUEUE].session_id = session_id
    rig.players[PAUSED_QUEUE].playback_state = player_state
    rig.players[PAUSED_QUEUE].active_source = active_source

    assert rig.queues.has_lost_paused_stream(PAUSED_QUEUE) is expected


async def test_a_playing_queue_keeps_its_slot(rig: _Rig) -> None:
    """Only a paused queue gives up its slot; a start elsewhere waits for it as before."""
    playing_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING)
    playing_buffer = await rig.fill(playing_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    with pytest.raises(ProviderStreamLimitError) as exc_info:
        await rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=0.2)

    # a source that fills ahead of playback frees its slot soon, so the wait is worth it
    assert not isinstance(exc_info.value, ProviderStreamHeldError)
    rig.stop_device.assert_not_awaited()
    assert playing_buffer.is_buffering
    await playing_buffer.clear()


async def test_a_playing_realtime_holder_refuses_a_start_on_another_queue_at_once(
    rig: _Rig,
) -> None:
    """A realtime source keeps its slot until its item ends: the start is refused, naming it."""
    playing_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    playing_buffer = await rig.fill(playing_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    started = asyncio.get_running_loop().time()
    with pytest.raises(ProviderStreamHeldError) as exc_info:
        await rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=15)

    assert asyncio.get_running_loop().time() - started < 1
    assert exc_info.value.translation_args == ["Spotify", PAUSED_QUEUE]
    assert exc_info.value.data == {
        "queue_id": PAUSED_QUEUE,
        "player_name": PAUSED_QUEUE,
        "provider_instance": INSTANCE,
    }
    rig.stop_device.assert_not_awaited()
    assert playing_buffer.is_buffering
    await playing_buffer.clear()


async def test_a_realtime_holder_whose_session_ended_is_waited_for(rig: _Rig) -> None:
    """A stop ends the session before its audio is released, and that release is worth the wait."""
    stopping_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    stopping_buffer = await rig.fill(stopping_item)
    rig.queues._queue_data[PAUSED_QUEUE].session_id = None
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    async def _release_shortly() -> None:
        await asyncio.sleep(0.1)
        await stopping_buffer.clear()

    release = asyncio.ensure_future(_release_shortly())
    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )
    await release

    assert buffer is not None
    assert buffer.is_buffering
    await buffer.clear()


async def test_a_paused_realtime_holder_is_taken_over_rather_than_refused(rig: _Rig) -> None:
    """The paused takeover comes first, so the start gets the slot instead of a refusal."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED, realtime=True)
    paused_buffer = await rig.fill(paused_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    buffer = await rig.audio.get_audio_buffer(
        starting_item, reason="prepare", capacity_wait_timeout=2
    )

    assert buffer is not None
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_paused_holder_without_audio_yet_is_taken_over_once_it_has_audio(
    rig: _Rig,
) -> None:
    """A start that finds a paused queue's source on its way in takes the slot over as well."""
    first_audio = asyncio.Event()

    async def _slow_to_deliver(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        await first_audio.wait()
        async for chunk in _endless_source():
            yield chunk

    rig.audio._get_media_stream = _slow_to_deliver  # type: ignore[method-assign]
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED, realtime=True)
    filling = asyncio.create_task(rig.fill(paused_item))
    while not rig.provider.stream_slot_holders:
        await asyncio.sleep(0)
    rig.audio._get_media_stream = _endless_source  # type: ignore[method-assign]
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    starting = asyncio.create_task(
        rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=30)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert not starting.done()
    first_audio.set()
    paused_buffer = await filling
    buffer = await asyncio.wait_for(starting, 5)

    assert buffer is not None
    assert buffer.is_buffering
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_a_paused_queue_that_starts_a_track_is_not_stopped_by_a_start_that_waits(
    rig: _Rig,
) -> None:
    """A start that waits behind a paused queue's own new track is refused, not handed the slot."""
    first_audio = asyncio.Event()

    async def _slow_to_deliver(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        await first_audio.wait()
        async for chunk in _endless_source():
            yield chunk

    rig.audio._get_media_stream = _slow_to_deliver  # type: ignore[method-assign]
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED, realtime=True)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    async def _load(item: QueueItem, **_kwargs: Any) -> None:
        await rig.fill(item)

    @contextlib.asynccontextmanager
    async def _slow_confirmation(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        # a slow speaker reports the pause until the wait for it to play has ended
        yield
        await asyncio.sleep(0.2)

    rig.queues._load_item = _load  # type: ignore[method-assign,assignment]
    rig.queues._repin_sleep_timer = MagicMock()  # type: ignore[method-assign]
    rig.queues.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    rig.mass.players.wait_for_player_update = _slow_confirmation
    rig.mass.players.play_media = AsyncMock()
    playing = asyncio.create_task(rig.queues._play_index(PAUSED_QUEUE, 0, 0, False))
    while not rig.provider.stream_slot_holders:
        await asyncio.sleep(0)
    rig.audio._get_media_stream = _endless_source  # type: ignore[method-assign]
    starting = asyncio.create_task(
        rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=30)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    first_audio.set()

    with pytest.raises(ProviderStreamHeldError):
        await asyncio.wait_for(starting, 5)
    await playing
    rig.stop_device.assert_not_awaited()
    assert paused_item.streamdetails is not None
    paused_buffer = paused_item.streamdetails.buffer
    assert paused_buffer is not None
    assert paused_buffer.is_buffering
    await paused_buffer.clear()


async def test_a_paused_queue_that_rebuilds_its_upcoming_tracks_hands_its_slot_over(
    rig: _Rig,
) -> None:
    """A paused dynamic queue that refills its upcoming tracks starts nothing, so it is paused."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED, realtime=True)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)
    paused_buffer = await rig.fill(paused_item)
    queue_data = rig.queues._queue_data[PAUSED_QUEUE]
    queue_data.queue.is_dynamic = True
    filling = asyncio.Event()
    filled = asyncio.Event()

    async def _fill_pool(*_args: Any, **_kwargs: Any) -> list[Any]:
        filling.set()
        await filled.wait()
        return [paused_item.media_item]

    rig.queues._managed_pool = SimpleNamespace(fill=_fill_pool)  # type: ignore[assignment]
    rig.queues.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    rig.queues.load = AsyncMock()  # type: ignore[method-assign]
    rig.queues.update_next_item_on_player = MagicMock()  # type: ignore[method-assign]

    async def _add() -> None:
        async with rig.playback_lock(PAUSED_QUEUE):
            await rig.queues._enter_dynamic_mode(PAUSED_QUEUE, QueueOption.ADD)

    adding = asyncio.create_task(_add())
    await filling.wait()
    starting = asyncio.create_task(
        rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=2)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    filled.set()
    await adding
    buffer = await asyncio.wait_for(starting, 5)

    assert buffer is not None
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert paused_buffer.cancelled
    await buffer.clear()


async def test_an_item_resuming_from_its_own_buffer_is_not_refused(rig: _Rig) -> None:
    """Audio that already arrived needs no slot, so a held one is no reason to refuse it."""
    rig.audio._get_media_stream = _finite_source  # type: ignore[method-assign]
    resuming_item = rig.add_queue(STARTING_QUEUE, PlaybackState.PAUSED, realtime=True)
    own_buffer = await rig.fill(resuming_item)
    while own_buffer.is_buffering:
        await asyncio.sleep(0.01)
    assert rig.provider.has_available_stream_slot
    rig.audio._get_media_stream = _endless_source  # type: ignore[method-assign]
    playing_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    playing_buffer = await rig.fill(playing_item)

    buffer = await rig.audio.get_audio_buffer(
        resuming_item, reason="prepare", capacity_wait_timeout=15
    )

    assert buffer is own_buffer
    await playing_buffer.clear()
    await own_buffer.clear()


async def test_a_provider_with_more_than_one_slot_keeps_the_wait(rig: _Rig) -> None:
    """One realtime holder says nothing about the other slots, so the start waits as before."""
    rig.provider = _provider(_TwoStreamProvider)
    first_buffer = await rig.fill(rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True))
    second_buffer = await rig.fill(rig.add_queue("office", PlaybackState.PLAYING, realtime=True))
    assert not rig.provider.has_available_stream_slot
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    with pytest.raises(ProviderStreamLimitError) as exc_info:
        await rig.audio.get_audio_buffer(starting_item, reason="prepare", capacity_wait_timeout=0.2)

    assert not isinstance(exc_info.value, ProviderStreamHeldError)
    await first_buffer.clear()
    await second_buffer.clear()


@pytest.mark.parametrize(
    ("player_filter", "named"),
    [([STARTING_QUEUE], False), ([STARTING_QUEUE, PAUSED_QUEUE], True), ([], True)],
    ids=["other-player-not-allowed", "other-player-allowed", "unrestricted"],
)
async def test_a_holder_is_named_only_to_a_user_who_may_control_it(
    rig: _Rig, player_filter: list[str], named: bool
) -> None:
    """A user limited to some players is refused all the same, but learns nothing of others."""
    playing_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    playing_buffer = await rig.fill(playing_item)
    set_current_user(
        User(user_id="kid", username="kid", role=UserRole.USER, player_filter=player_filter)
    )
    try:
        with pytest.raises(ProviderStreamLimitError) as exc_info:
            rig.queues.refuse_held_stream_slot(INSTANCE, STARTING_QUEUE)
    finally:
        set_current_user(None)

    assert isinstance(exc_info.value, ProviderStreamHeldError) is named
    assert (exc_info.value.data is not None) is named
    await playing_buffer.clear()


@pytest.mark.parametrize(
    ("asking_queue", "state", "realtime", "has_session", "refused"),
    [
        (STARTING_QUEUE, PlaybackState.PLAYING, True, True, True),
        (PAUSED_QUEUE, PlaybackState.PLAYING, True, True, False),
        (STARTING_QUEUE, PlaybackState.PLAYING, False, True, False),
        (STARTING_QUEUE, PlaybackState.PAUSED, True, True, False),
        (STARTING_QUEUE, PlaybackState.PLAYING, True, False, False),
    ],
    ids=[
        "another-queue-playing-a-realtime-source",
        "the-asking-queue-itself",
        "a-source-that-fills-ahead",
        "paused-is-handed-over-instead",
        "session-ended",
    ],
)
async def test_only_another_queue_playing_a_realtime_source_holds_its_slot_for_good(
    rig: _Rig,
    asking_queue: str,
    state: PlaybackState,
    realtime: bool,
    has_session: bool,
    refused: bool,
) -> None:
    """Only a realtime source played by another queue holds its slot for the item's length."""
    item = rig.add_queue(PAUSED_QUEUE, state, realtime=realtime)
    buffer = await rig.fill(item)
    if not has_session:
        rig.queues._queue_data[PAUSED_QUEUE].session_id = None

    with pytest.raises(ProviderStreamHeldError) if refused else contextlib.nullcontext():
        rig.queues.refuse_held_stream_slot(INSTANCE, asking_queue)

    await buffer.clear()


async def test_a_realtime_source_without_audio_yet_refuses_nobody(rig: _Rig) -> None:
    """A source that has its slot and no audio yet can still fail, so a start waits behind it."""
    first_audio = asyncio.Event()

    async def _slow_to_deliver(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        await first_audio.wait()
        async for chunk in _endless_source():
            yield chunk

    rig.audio._get_media_stream = _slow_to_deliver  # type: ignore[method-assign]
    item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    assert item.streamdetails is not None
    buffer = await AudioBuffer.get_buffer(
        rig.mass,
        item.streamdetails,
        wait_ready=False,
        source_wait_timeout=0,
        on_created=partial(rig.queues.register_audio_buffer, item),
    )
    await asyncio.sleep(0)
    assert rig.provider.stream_slot_holders == (buffer,)

    rig.queues.refuse_held_stream_slot(INSTANCE, STARTING_QUEUE)

    first_audio.set()
    await asyncio.wait_for(buffer.ready.wait(), 1)
    with pytest.raises(ProviderStreamHeldError):
        rig.queues.refuse_held_stream_slot(INSTANCE, STARTING_QUEUE)
    await buffer.clear()


async def test_a_source_whose_track_left_its_queue_still_holds_its_slot(rig: _Rig) -> None:
    """The source that holds the slot is known by its queue, also once its track is off the list."""
    item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True)
    buffer = await rig.fill(item)
    rig.queues._queue_data[PAUSED_QUEUE].items = []

    with pytest.raises(ProviderStreamHeldError):
        rig.queues.refuse_held_stream_slot(INSTANCE, STARTING_QUEUE)

    await buffer.clear()


@pytest.mark.parametrize(
    ("asking_queue", "queue_state", "player_state", "active_source", "announcing"),
    [
        (PAUSED_QUEUE, PlaybackState.PAUSED, PlaybackState.PAUSED, PAUSED_QUEUE, False),
        (STARTING_QUEUE, PlaybackState.PAUSED, PlaybackState.PLAYING, PAUSED_QUEUE, False),
        (STARTING_QUEUE, PlaybackState.IDLE, PlaybackState.PAUSED, "spotify_connect", False),
        (STARTING_QUEUE, PlaybackState.PAUSED, PlaybackState.PAUSED, "spotify_connect", False),
        (STARTING_QUEUE, PlaybackState.PAUSED, PlaybackState.PAUSED, PAUSED_QUEUE, True),
    ],
    ids=[
        "the-asking-queue-itself",
        "player-playing-again",
        "player-paused-on-another-source",
        "queue-not-updated-yet-after-a-source-switch",
        "announcement-in-progress",
    ],
)
async def test_only_another_queue_paused_on_its_player_gives_up_its_slot(
    rig: _Rig,
    asking_queue: str,
    queue_state: PlaybackState,
    player_state: PlaybackState,
    active_source: str,
    announcing: bool,
) -> None:
    """A queue that is not paused for sure, or is the one asking, keeps its slot."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.queues._queue_data[PAUSED_QUEUE].queue.state = queue_state
    player = rig.players[PAUSED_QUEUE]
    player.playback_state = player_state
    player.active_source = active_source
    player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = announcing

    assert not rig.queues.has_stream_slot_to_take_over(INSTANCE, asking_queue)
    assert not await rig.queues.take_over_stream_slot(INSTANCE, asking_queue)

    await asyncio.gather(*rig.tasks)
    rig.stop_device.assert_not_awaited()
    assert paused_buffer.is_buffering
    await paused_buffer.clear()


async def test_a_paused_queue_still_waiting_for_a_slot_is_no_holder(rig: _Rig) -> None:
    """A paused queue whose source waits behind a playing queue holds nothing to hand over."""
    playing_item = rig.add_queue("office", PlaybackState.PLAYING)
    playing_buffer = await rig.fill(playing_item)
    waiting_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    assert waiting_item.streamdetails is not None
    waiting_buffer = await AudioBuffer.get_buffer(
        rig.mass,
        waiting_item.streamdetails,
        wait_ready=False,
        source_wait_timeout=None,
        on_created=partial(rig.queues.register_audio_buffer, waiting_item),
    )
    await asyncio.sleep(0)
    assert waiting_buffer.is_buffering

    assert not rig.queues.has_stream_slot_to_take_over(INSTANCE, STARTING_QUEUE)

    await waiting_buffer.clear()
    await playing_buffer.clear()


@pytest.mark.parametrize(
    ("player_filter", "expected"),
    [([STARTING_QUEUE], False), ([STARTING_QUEUE, PAUSED_QUEUE], True), ([], True)],
    ids=["other-player-not-allowed", "other-player-allowed", "unrestricted"],
)
async def test_a_user_only_takes_the_slot_of_a_player_they_may_control(
    rig: _Rig, player_filter: list[str], expected: bool
) -> None:
    """A user limited to some players never stops a paused player outside their filter."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    set_current_user(
        User(user_id="kid", username="kid", role=UserRole.USER, player_filter=player_filter)
    )
    try:
        assert rig.queues.has_stream_slot_to_take_over(INSTANCE, STARTING_QUEUE) is expected
    finally:
        set_current_user(None)
    await paused_buffer.clear()


async def test_a_paused_queue_without_a_session_is_no_holder(rig: _Rig) -> None:
    """A stop releases only a session's audio, so a queue without one has nothing to hand over."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    rig.queues._queue_data[PAUSED_QUEUE].session_id = None

    assert not rig.queues.has_stream_slot_to_take_over(INSTANCE, STARTING_QUEUE)
    assert not await rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE)

    rig.stop_device.assert_not_awaited()
    await paused_buffer.clear()


async def test_a_queue_that_resumes_before_it_is_stopped_keeps_playing(rig: _Rig) -> None:
    """The stop waits for the queue's playback lock, and a resume holding it wins."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    lock_taken = asyncio.Event()
    resumed = asyncio.Event()

    async def _resume() -> None:
        async with rig.playback_lock(PAUSED_QUEUE):
            lock_taken.set()
            await resumed.wait()
            rig.players[PAUSED_QUEUE].playback_state = PlaybackState.PLAYING
            rig.queues._queue_data[PAUSED_QUEUE].queue.state = PlaybackState.PLAYING

    resume = asyncio.ensure_future(_resume())
    await lock_taken.wait()
    release = asyncio.ensure_future(rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE))
    await asyncio.sleep(0)
    resumed.set()

    assert not await release
    await resume
    rig.stop_device.assert_not_awaited()
    assert paused_buffer.is_buffering
    await paused_buffer.clear()


async def test_a_holder_whose_source_finished_meanwhile_is_left_alone(rig: _Rig) -> None:
    """A paused queue that gave up its slot while its lock was held elsewhere keeps its pause."""
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    lock_taken = asyncio.Event()
    source_done = asyncio.Event()

    async def _hold_the_lock() -> None:
        async with rig.playback_lock(PAUSED_QUEUE):
            lock_taken.set()
            await source_done.wait()

    holder = asyncio.ensure_future(_hold_the_lock())
    await lock_taken.wait()
    release = asyncio.ensure_future(rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE))
    await asyncio.sleep(0)
    await paused_buffer.clear()
    source_done.set()

    assert not await release
    await holder
    rig.stop_device.assert_not_awaited()
    assert rig.queues._queue_data[PAUSED_QUEUE].session_id is not None


async def test_a_member_starting_from_its_paused_group_takes_the_groups_slot(rig: _Rig) -> None:
    """
    A player that starts its own queue while its group is paused holds the group's lock.

    The group's stop runs within that start, so it gets the lock right away.
    """
    paused_item = rig.add_queue(PAUSED_QUEUE, PlaybackState.PAUSED)
    paused_buffer = await rig.fill(paused_item)
    starting_item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)

    async with rig.playback_lock(PAUSED_QUEUE):
        buffer = await rig.audio.get_audio_buffer(
            starting_item, reason="prepare", capacity_wait_timeout=2
        )

    assert buffer is not None
    assert buffer.is_buffering
    rig.stop_device.assert_awaited_once_with(PAUSED_QUEUE)
    assert paused_buffer.cancelled
    await buffer.clear()


def _mapping(instance: str) -> ProviderMapping:
    """Build the mapping of an item on the given provider instance."""
    return ProviderMapping(
        item_id=f"on-{instance}", provider_domain="spotify", provider_instance=instance
    )


def _playlist(item_id: str = "big") -> Playlist:
    """Build a playlist straight from the single-slot provider."""
    return Playlist(
        item_id=item_id, provider=INSTANCE, name=item_id, provider_mappings={_mapping(INSTANCE)}
    )


async def _hold_the_slot(rig: _Rig) -> AudioBuffer:
    """Let realtime playback on another queue take the provider's only slot."""
    rig.provider.available = True
    return await rig.fill(rig.add_queue(PAUSED_QUEUE, PlaybackState.PLAYING, realtime=True))


async def test_a_play_on_a_held_source_is_refused_before_its_tracks_are_fetched(
    rig: _Rig,
) -> None:
    """The refusal comes once the playlist itself is known, and the queue stays as it was."""
    holder_buffer = await _hold_the_slot(rig)
    queued = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)
    queue_data = rig.queues._queue_data[STARTING_QUEUE]
    seed = _playlist("earlier")
    queue_data.enqueued_media_items = [seed]
    resolve = AsyncMock()
    rig.queues._media_resolver = MagicMock(_resolve_media_items=resolve)

    with pytest.raises(ProviderStreamHeldError) as exc_info:
        await rig.queues._handle_play_media(STARTING_QUEUE, _playlist(), QueueOption.REPLACE)

    assert exc_info.value.data == {
        "queue_id": PAUSED_QUEUE,
        "player_name": PAUSED_QUEUE,
        "provider_instance": INSTANCE,
    }
    resolve.assert_not_awaited()
    assert queue_data.items == [queued]
    assert queue_data.enqueued_media_items == [seed]
    await holder_buffer.clear()


def _second_account(rig: _Rig) -> MusicProvider:
    """Add a free second account of the provider's streaming service."""
    second = _provider(instance=SECOND_INSTANCE)
    second.available = True
    rig.other_providers[SECOND_INSTANCE] = second
    rig.mass.providers = [rig.provider, second]
    return second


async def test_a_copy_on_a_free_second_account_keeps_a_play_from_being_refused_early(
    rig: _Rig,
) -> None:
    """The stream itself picks the free account, so the play goes ahead."""
    holder_buffer = await _hold_the_slot(rig)
    _second_account(rig)
    track = Track(
        item_id="7",
        provider="library",
        name="Seven",
        provider_mappings={_mapping(INSTANCE), _mapping(SECOND_INSTANCE)},
    )

    rig.queues.refuse_held_item(track, STARTING_QUEUE, None)

    await holder_buffer.clear()


async def test_a_free_second_account_streams_an_item_mapped_to_the_busy_one(rig: _Rig) -> None:
    """Another account of the same service plays the same item id, so the play goes ahead."""
    holder_buffer = await _hold_the_slot(rig)
    _second_account(rig)

    rig.queues.refuse_held_item(_playlist(), STARTING_QUEUE, None)

    await holder_buffer.clear()


async def test_a_free_account_the_user_may_not_use_does_not_keep_a_play_going(
    rig: _Rig,
) -> None:
    """Only the sources the user may play from count as free."""
    holder_buffer = await _hold_the_slot(rig)
    _second_account(rig)
    track = Track(
        item_id="7",
        provider="library",
        name="Seven",
        provider_mappings={_mapping(INSTANCE), _mapping(SECOND_INSTANCE)},
    )

    with (
        patch.object(
            feeder_mod, "playback_sources_for", return_value=PlaybackSources([INSTANCE], [])
        ),
        pytest.raises(ProviderStreamHeldError),
    ):
        rig.queues.refuse_held_item(track, STARTING_QUEUE, None)

    await holder_buffer.clear()


async def test_a_play_another_streaming_service_may_find_is_not_refused_early(
    rig: _Rig,
) -> None:
    """The stream itself may still find the item on a service it has no mapping on."""
    holder_buffer = await _hold_the_slot(rig)
    rig.mass.music.providers = [
        SimpleNamespace(
            available=True,
            is_streaming_provider=True,
            supported_features={ProviderFeature.SEARCH},
            domain="tidal",
            supported_media_types={MediaType.TRACK},
            instance_id="tidal--one",
        )
    ]

    rig.queues.refuse_held_item(_playlist(), STARTING_QUEUE, None)

    await holder_buffer.clear()


async def test_a_provider_with_more_than_one_slot_is_not_refused_early(rig: _Rig) -> None:
    """One realtime holder says nothing about the other slots, so the play goes ahead."""
    rig.provider = _provider(_TwoStreamProvider)
    first_buffer = await _hold_the_slot(rig)
    second_buffer = await rig.fill(rig.add_queue("office", PlaybackState.PLAYING, realtime=True))
    assert not rig.provider.has_available_stream_slot

    rig.queues.refuse_held_item(_playlist(), STARTING_QUEUE, None)

    await first_buffer.clear()
    await second_buffer.clear()


def _owned_by(rig: _Rig, owner: str | None = OWNER) -> None:
    """Let the given user own the provider's account, which is shared with everyone."""
    set_music_source_access(
        rig.mass, {INSTANCE: ProviderAccess(owner=owner, sharing=ProviderSharing.EVERYONE)}
    )


async def _borrow(rig: _Rig, userid: str | None = BORROWER) -> tuple[QueueItem, AudioBuffer]:
    """Let realtime playback for the given user take the account's only slot, 42s in its item."""
    rig.provider.available = True
    item = rig.add_queue(BORROWING_QUEUE, PlaybackState.PLAYING, realtime=True)
    queue_data = rig.queues._queue_data[BORROWING_QUEUE]
    queue_data.userid = userid
    queue_data.queue.elapsed_time = 42
    queue_data.queue.elapsed_time_last_updated = time.time()
    return item, await rig.fill(item)


def _owner_queue(rig: _Rig, userid: str | None = OWNER) -> QueueItem:
    """Register the queue that starts playback for the given user."""
    item = rig.add_queue(STARTING_QUEUE, PlaybackState.IDLE)
    rig.queues._queue_data[STARTING_QUEUE].userid = userid
    return item


def _follow_resumes(rig: _Rig) -> list[tuple[str, int | str, int]]:
    """
    Let a queue that resumes get the audio of its item as a start does, and record the resumes.

    Its source is the item's own account, or the second one while the first has no free slot.
    """
    resumed: list[tuple[str, int | str, int]] = []

    async def _get_stream_details(
        queue_item: QueueItem,
        excluded_provider_instances: set[str] | None = None,
        **_kwargs: Any,
    ) -> StreamDetails:
        excluded = excluded_provider_instances or set()
        return StreamDetails(
            provider=next(i for i in (INSTANCE, SECOND_INSTANCE) if i not in excluded),
            item_id=f"track-{queue_item.queue_id}",
            audio_format=PCM_FORMAT,
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.CUSTOM,
            duration=600,
            queue_id=queue_item.queue_id,
            is_realtime=True,
        )

    async def _play_index(
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,  # noqa: ARG001 - the signature of the method it stands in for
    ) -> None:
        resumed.append((queue_id, index, seek_position))
        queue_item = rig.queues.get_item(queue_id, index)
        assert queue_item is not None
        rig.queues._queue_data[queue_id].session_id = "resumed"
        await rig.audio.get_audio_buffer(
            queue_item, seek_position_ms=seek_position * 1000, reason="prepare"
        )

    rig.audio.get_stream_details = _get_stream_details  # type: ignore[method-assign,assignment]
    rig.queues.play_index = _play_index  # type: ignore[method-assign]
    return resumed


def _logged_giving_way(rig: _Rig, outcome: str) -> bool:
    """Return whether the borrowing queue was logged to give way, with the given outcome."""
    return any(
        BORROWING_QUEUE in call.args and outcome in call.args
        for call in rig.queues.logger.info.call_args_list  # type: ignore[attr-defined]
    )


async def test_a_playing_borrower_gives_way_to_the_owner_and_goes_on_from_another_account(
    rig: _Rig,
) -> None:
    """The owner's start gets the account, and the borrower resumes where it was on a free one."""
    _owned_by(rig)
    second = _second_account(rig)
    borrowed_item, borrowed_buffer = await _borrow(rig)
    owner_item = _owner_queue(rig)
    resumed = _follow_resumes(rig)

    buffer = await rig.audio.get_audio_buffer(owner_item, reason="prepare", capacity_wait_timeout=5)
    await asyncio.wait_for(asyncio.gather(*rig.tasks), 5)

    assert buffer is not None
    assert rig.provider.stream_slot_holders == (buffer,)
    rig.stop_device.assert_awaited_once_with(BORROWING_QUEUE)
    assert borrowed_buffer.cancelled
    borrower = rig.queues._queue_data[BORROWING_QUEUE].queue
    assert borrower.resume_pos >= 42
    assert resumed == [(BORROWING_QUEUE, borrowed_item.queue_item_id, borrower.resume_pos)]
    assert borrowed_item.streamdetails is not None
    assert borrowed_item.streamdetails.provider == SECOND_INSTANCE
    moved_buffer = borrowed_item.streamdetails.buffer
    assert moved_buffer is not None
    assert second.stream_slot_holders == (moved_buffer,)
    assert _logged_giving_way(rig, "it goes on from another source")
    await buffer.clear()
    await moved_buffer.clear()


@pytest.mark.parametrize(
    "other_source", ["none", "busy", "not-theirs"], ids=lambda value: f"other-account-{value}"
)
async def test_a_playing_borrower_without_another_free_source_stops_and_keeps_its_position(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, other_source: str
) -> None:
    """The owner's start gets the account, and the borrower stops where it can resume from."""
    _owned_by(rig)
    async with contextlib.AsyncExitStack() as stack:
        if other_source != "none":
            second = _second_account(rig)
            if other_source == "busy":
                await stack.enter_async_context(second.acquire_stream_slot(0))
        if other_source == "not-theirs":
            monkeypatch.setattr(
                feeder_mod,
                "playback_sources",
                AsyncMock(return_value=PlaybackSources([INSTANCE], [])),
            )
        _, borrowed_buffer = await _borrow(rig)
        owner_item = _owner_queue(rig)
        resumed = _follow_resumes(rig)

        buffer = await rig.audio.get_audio_buffer(
            owner_item, reason="prepare", capacity_wait_timeout=5
        )
        await asyncio.wait_for(asyncio.gather(*rig.tasks), 5)

    assert buffer is not None
    assert rig.provider.stream_slot_holders == (buffer,)
    rig.stop_device.assert_awaited_once_with(BORROWING_QUEUE)
    assert borrowed_buffer.cancelled
    borrower = rig.queues._queue_data[BORROWING_QUEUE]
    assert borrower.session_id is None
    assert borrower.queue.resume_pos >= 42
    assert resumed == []
    assert _logged_giving_way(rig, "it stops, as no other source has a free slot")
    await buffer.clear()


async def test_the_owners_own_queue_keeps_the_slot_from_the_owners_other_queue(
    rig: _Rig,
) -> None:
    """A queue that plays for the owner is no borrower: the owner's start elsewhere is refused."""
    _owned_by(rig)
    _, playing_buffer = await _borrow(rig, userid=OWNER)
    owner_item = _owner_queue(rig)

    with pytest.raises(ProviderStreamHeldError):
        await rig.audio.get_audio_buffer(owner_item, reason="prepare", capacity_wait_timeout=15)

    rig.stop_device.assert_not_awaited()
    assert playing_buffer.is_buffering
    await playing_buffer.clear()


async def test_a_borrower_can_not_take_the_slot_back_before_the_owners_queue_has_it(
    rig: _Rig,
) -> None:
    """The slot is kept for the owner's queue before the borrower goes on, so it moves instead."""
    _owned_by(rig)
    second = _second_account(rig)
    borrowed_item, _ = await _borrow(rig)
    _owner_queue(rig)
    resumed = _follow_resumes(rig)

    assert await rig.queues.take_over_stream_slot(INSTANCE, STARTING_QUEUE)
    # the owner's queue has not taken its slot yet when the borrower resumes
    await asyncio.wait_for(asyncio.gather(*rig.tasks), 5)

    assert len(resumed) == 1
    assert rig.provider.stream_slot_holders == ()
    assert rig.provider.stream_slot_reservations == (STARTING_QUEUE,)
    assert borrowed_item.streamdetails is not None
    assert borrowed_item.streamdetails.provider == SECOND_INSTANCE
    moved_buffer = borrowed_item.streamdetails.buffer
    assert moved_buffer is not None
    assert second.stream_slot_holders == (moved_buffer,)
    rig.provider.end_stream_slot_reservation(STARTING_QUEUE)
    await moved_buffer.clear()


@pytest.mark.parametrize(
    ("owner", "holder_user", "starting_user", "gives_way"),
    [
        (OWNER, BORROWER, OWNER, True),
        (OWNER, None, OWNER, True),
        (OWNER, OWNER, OWNER, False),
        (OWNER, BORROWER, "visitor", False),
        (OWNER, BORROWER, None, False),
        (None, BORROWER, OWNER, False),
    ],
    ids=[
        "another-user",
        "anonymous-playback",
        "the-owners-own-queue",
        "a-start-for-someone-else",
        "an-anonymous-start",
        "an-account-without-owner",
    ],
)
async def test_only_a_queue_borrowing_the_account_gives_way_to_its_owner(
    rig: _Rig,
    owner: str | None,
    holder_user: str | None,
    starting_user: str | None,
    gives_way: bool,
) -> None:
    """A realtime holder playing the account for someone else gives it up to its owner alone."""
    _owned_by(rig, owner)
    _, buffer = await _borrow(rig, holder_user)
    _owner_queue(rig, starting_user)

    with contextlib.nullcontext() if gives_way else pytest.raises(ProviderStreamHeldError):
        rig.queues.refuse_held_stream_slot(INSTANCE, STARTING_QUEUE)
    assert rig.queues.has_stream_slot_to_take_over(INSTANCE, STARTING_QUEUE) is gives_way

    await buffer.clear()


async def test_the_owner_takes_the_slot_of_a_borrower_on_a_player_they_may_not_control(
    rig: _Rig,
) -> None:
    """Owning the account is the right to its slot, where a paused player has to be the user's."""
    _owned_by(rig)
    _, buffer = await _borrow(rig)
    _owner_queue(rig)
    set_current_user(
        User(user_id=OWNER, username=OWNER, role=UserRole.USER, player_filter=[STARTING_QUEUE])
    )
    try:
        assert rig.queues.has_stream_slot_to_take_over(INSTANCE, STARTING_QUEUE)
    finally:
        set_current_user(None)
    await buffer.clear()


@pytest.mark.parametrize(
    ("userid", "refused"), [(OWNER, False), ("visitor", True)], ids=["the-owner", "someone-else"]
)
async def test_a_play_of_the_owner_is_not_refused_over_a_borrower(
    rig: _Rig, userid: str, refused: bool
) -> None:
    """The play goes ahead for the owner, whoever the queue played for before."""
    _owned_by(rig)
    _, buffer = await _borrow(rig)
    _owner_queue(rig, userid=None)
    user = User(user_id=userid, username=userid, role=UserRole.USER)

    with pytest.raises(ProviderStreamHeldError) if refused else contextlib.nullcontext():
        rig.queues.refuse_held_item(_playlist(), STARTING_QUEUE, user)

    await buffer.clear()
