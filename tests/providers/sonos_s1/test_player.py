"""Unit tests for the Sonos S1 player."""

from __future__ import annotations

import asyncio
import threading
from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from soco.exceptions import SoCoException

from music_assistant.mass import MusicAssistant
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sonos_s1 import player as player_module
from music_assistant.providers.sonos_s1.constants import (
    POLL_INTERVAL,
    SUBSCRIPTION_SERVICES,
    TRANSITION_POLL_INTERVAL,
)
from music_assistant.providers.sonos_s1.player import SonosPlayer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

STREAM_URL = "http://192.168.1.2:8097/single/sessionabc/queue1/item1/player1.flac"


def _make_soco(uid: str = "RINCON_000E58AAAAAA01400", name: str = "Test Sonos") -> MagicMock:
    """Create a mocked soco device."""
    soco = MagicMock()
    soco.uid = uid
    soco.household_id = "Sonos_household"
    soco.player_name = name
    soco._player_name = name
    soco.ip_address = "127.0.0.1"
    soco.speaker_info = {"model_name": "Sonos Play:1"}
    return soco


@pytest.fixture
def sonos_player() -> SonosPlayer:
    """Create a SonosPlayer with a mocked soco device and provider."""
    provider = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value=STREAM_URL)
    return SonosPlayer(provider=provider, soco=_make_soco())


@pytest.fixture
async def timer_mass() -> AsyncGenerator[MusicAssistant]:
    """Create a bare MusicAssistant exposing the real call_later/cancel_timer machinery."""
    mass = object.__new__(MusicAssistant)
    mass.loop = asyncio.get_running_loop()
    mass.loop_thread_id = threading.get_ident()
    mass._tracked_timers = {}
    mass._tracked_tasks = {}
    mass.config = MagicMock()
    mass.players = MagicMock()
    mass.players.all_players.return_value = []
    yield mass
    for handle in mass._tracked_timers.values():
        handle.cancel()
    for task in mass._tracked_tasks.values():
        task.cancel()


def _make_player(mass: MusicAssistant, uid: str, name: str) -> SonosPlayer:
    """Create a SonosPlayer bound to the given MusicAssistant."""
    provider = MagicMock()
    provider.mass = mass
    provider.topology_condition = asyncio.Condition()
    return SonosPlayer(provider=provider, soco=_make_soco(uid, name))


def _poll_id(player: SonosPlayer) -> str:
    """Return the task id that debounces the follow-up poll of the given speaker."""
    return f"sonos_poll_{player.player_id}"


def _pending_polls(mass: MusicAssistant) -> list[str]:
    """Return the task ids of all pending speaker polls."""
    return sorted(task_id for task_id in mass._tracked_timers if task_id.startswith("sonos_poll_"))


def _make_media() -> PlayerMedia:
    """Return PlayerMedia as built by the queue controller, with an MA media uri."""
    return PlayerMedia(
        uri="library://track/123",
        media_type=MediaType.TRACK,
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration=180,
        source_id="queue1",
        queue_item_id="item1",
    )


async def test_enqueue_next_media_builds_didl_from_stream_url(
    sonos_player: SonosPlayer,
) -> None:
    """The enqueue metadata res element must contain the stream url, not the MA media uri."""
    await sonos_player.enqueue_next_media(_make_media())
    call_args = sonos_player.soco.avTransport.SetNextAVTransportURI.call_args
    args = dict(call_args.args[0])
    assert args["NextURI"] == STREAM_URL
    assert STREAM_URL in args["NextURIMetaData"]
    assert "library://track/123" not in args["NextURIMetaData"]


async def test_play_media_builds_didl_from_stream_url(sonos_player: SonosPlayer) -> None:
    """The play metadata res element must contain the stream url, not the MA media uri."""
    await sonos_player.play_media(_make_media())
    call_args = sonos_player.soco.play_uri.call_args
    assert call_args.args[0] == STREAM_URL
    assert STREAM_URL in call_args.kwargs["meta"]
    assert "library://track/123" not in call_args.kwargs["meta"]


def _set_transport_state(sonos_player: SonosPlayer, state: str) -> None:
    """Make the mocked speaker report the given transport state."""
    sonos_player.soco.get_current_transport_info.return_value = {"current_transport_state": state}


def test_transitional_state_shortens_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A transitional transport state keeps the last known state and is watched closely."""
    sonos_player._attr_playback_state = PlaybackState.IDLE
    _set_transport_state(sonos_player, "TRANSITIONING")

    sonos_player.poll_media()

    assert sonos_player._attr_playback_state == PlaybackState.IDLE
    assert sonos_player.poll_interval == TRANSITION_POLL_INTERVAL


def test_settled_state_restores_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A usable transport state returns the speaker to the regular poll interval."""
    sonos_player._attr_poll_interval = TRANSITION_POLL_INTERVAL

    _set_transport_state(sonos_player, "PLAYING")
    with (
        patch.object(sonos_player, "_set_basic_track_info"),
        patch.object(sonos_player, "update_player"),
    ):
        sonos_player.poll_media()

    assert sonos_player.poll_interval == POLL_INTERVAL


def test_transitional_event_shortens_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A transitional state delivered by subscription event is watched closely too."""
    event = MagicMock()
    event.variables = {"transport_state": "TRANSITIONING"}

    sonos_player._handle_avtransport_event(event)

    assert sonos_player.poll_interval == TRANSITION_POLL_INTERVAL


async def test_repeated_commands_collapse_to_one_pending_poll(
    timer_mass: MusicAssistant,
) -> None:
    """A burst of commands leaves a single pending poll instead of one poll per command."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")

    await player.volume_set(30)
    first_handle = timer_mass._tracked_timers[_poll_id(player)]
    await player.volume_set(40)
    await player.volume_mute(True)
    await player.play()

    assert _pending_polls(timer_mass) == [_poll_id(player)]
    assert first_handle.cancelled()


async def test_each_speaker_keeps_its_own_pending_poll(timer_mass: MusicAssistant) -> None:
    """Commands to different speakers are polled independently."""
    kitchen = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    study = _make_player(timer_mass, "RINCON_000E58BBBBBB01400", "Study")

    await kitchen.volume_set(30)
    await study.volume_set(40)

    assert _pending_polls(timer_mass) == sorted([_poll_id(kitchen), _poll_id(study)])


async def test_set_members_polls_the_speakers_it_regrouped(timer_mass: MusicAssistant) -> None:
    """Grouping polls each speaker that was joined or unjoined, not the coordinator."""
    kitchen = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    study = _make_player(timer_mass, "RINCON_000E58BBBBBB01400", "Study")
    hallway = _make_player(timer_mass, "RINCON_000E58CCCCCC01400", "Hallway")
    cast("MagicMock", timer_mass.players).get_player.side_effect = {
        study.player_id: study,
        hallway.player_id: hallway,
    }.get

    await kitchen.set_members(player_ids_to_add=[study.player_id, hallway.player_id])

    assert _pending_polls(timer_mass) == sorted([_poll_id(study), _poll_id(hallway)])


async def test_grouping_failure_is_reported_as_a_player_command_failure(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker that refuses to join surfaces as a typed player command failure."""
    kitchen = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    study = _make_player(timer_mass, "RINCON_000E58BBBBBB01400", "Study")
    cast("MagicMock", timer_mass.players).get_player.side_effect = {study.player_id: study}.get
    study.soco.join.side_effect = SoCoException("the speaker refused to join")

    with pytest.raises(PlayerCommandFailed, match="Kitchen"):
        await kitchen.set_members(player_ids_to_add=[study.player_id])


async def test_unload_cancels_the_pending_poll(timer_mass: MusicAssistant) -> None:
    """An unloaded speaker is not polled by a command that was still in flight."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    await player.volume_set(30)
    handle = timer_mass._tracked_timers[_poll_id(player)]

    await player.on_unload()

    assert _pending_polls(timer_mass) == []
    assert handle.cancelled()


async def test_unload_cancels_a_poll_that_already_started(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that started just before the speaker was unloaded is aborted."""
    monkeypatch.setattr(player_module, "COMMAND_POLL_DELAY", 0)
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    polling = asyncio.Event()

    async def _slow_poll() -> None:
        polling.set()
        await asyncio.sleep(5)

    monkeypatch.setattr(player, "poll", _slow_poll)
    player.schedule_poll()
    await polling.wait()
    task = timer_mass._tracked_tasks[_poll_id(player)]

    await player.on_unload()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unloaded_player_ignores_results_from_a_running_poll(
    timer_mass: MusicAssistant,
) -> None:
    """A poll left running in its worker thread cannot update an unloaded speaker."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    await player.on_unload()

    with patch.object(player, "_update_attributes") as update_attributes:
        player.update_player()

    update_attributes.assert_not_called()


async def test_unloaded_player_ignores_group_topology_updates(
    timer_mass: MusicAssistant,
) -> None:
    """A group update raised by a running poll cannot update an unloaded speaker."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    # let the speaker report itself as coordinator of a two-speaker group, so a
    # topology update would regroup and signal the change if it were not unloaded
    member = MagicMock(uid="RINCON_000E58BBBBBB01400", is_visible=True)
    player.soco.group.coordinator.uid = player.player_id
    player.soco.group.members = [player.soco.group.coordinator, member]
    await player.on_unload()

    with patch.object(player, "update_state") as update_state:
        await player.create_update_groups_coro()
        await asyncio.sleep(0)

    assert player.group_members == []
    update_state.assert_not_called()


async def test_on_unload_unsubscribes_from_soco_events(timer_mass: MusicAssistant) -> None:
    """Unloading a speaker also tears down its soco event subscriptions."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    subscription = MagicMock()
    subscription.unsubscribe = AsyncMock()
    player._subscriptions = [subscription]

    await player.on_unload()

    subscription.unsubscribe.assert_awaited_once()
    assert player._subscriptions == []


async def test_on_unload_unsubscribes_even_when_already_unavailable(
    timer_mass: MusicAssistant,
) -> None:
    """Unlike offline(), on_unload() must still tear down an unavailable speaker's events."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    subscription = MagicMock()
    subscription.unsubscribe = AsyncMock()
    player._subscriptions = [subscription]
    player._attr_available = False

    await player.on_unload()

    subscription.unsubscribe.assert_awaited_once()
    assert player._subscriptions == []


async def test_unloaded_player_is_not_resubscribed_by_a_late_poll(
    timer_mass: MusicAssistant,
) -> None:
    """A poll that reaches an unloaded speaker must not subscribe it to events again."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    # the speaker was marked unavailable earlier, so it carries no subscriptions and
    # unloading it leaves that unavailable state behind for a late poll to act on
    player._attr_available = False
    await player.on_unload()

    with patch.object(player, "subscribe", AsyncMock()) as subscribe:
        await player.poll()

    subscribe.assert_not_called()
    assert player._attr_available is False


async def test_speaker_answering_a_poll_again_is_resubscribed(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker that answers a poll after being unavailable is subscribed to events again."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    player.soco.group.coordinator.uid = player.player_id
    player.soco.group.members = [player.soco.group.coordinator]
    player._attr_available = False

    with patch.object(player, "subscribe", AsyncMock()) as subscribe:
        await player.poll()

    subscribe.assert_called_once()
    assert player._attr_available is True


async def test_unloading_mid_subscribe_keeps_no_subscriptions(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker unloaded while subscribing must not keep the subscriptions it created."""
    player = _make_player(timer_mass, "RINCON_000E58AAAAAA01400", "Kitchen")
    subscribing = asyncio.Event()
    speaker_responds = asyncio.Event()

    async def _slow_subscribe_target(_target: object, _callback: object) -> None:
        subscribing.set()
        await speaker_responds.wait()
        player._subscriptions.append(MagicMock(unsubscribe=AsyncMock()))

    with patch.object(player, "_subscribe_target", _slow_subscribe_target):
        subscribe_task = asyncio.create_task(player.subscribe())
        await subscribing.wait()

        unload_task = asyncio.create_task(player.on_unload())
        await asyncio.sleep(0)
        assert not unload_task.done()

        speaker_responds.set()
        async with asyncio.timeout(5):
            await asyncio.gather(subscribe_task, unload_task)

    assert player._subscriptions == []


async def test_unsubscribe_drops_subscriptions_even_when_cancelled() -> None:
    """A cancelled unsubscribe must not leave stale entries that block resubscribing."""
    provider = MagicMock()
    player = SonosPlayer(provider=provider, soco=_make_soco())
    subscription = MagicMock()
    subscription.unsubscribe = AsyncMock(side_effect=partial(asyncio.sleep, 5))
    player._subscriptions = [subscription]

    task = asyncio.create_task(player.unsubscribe())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert player._subscriptions == []
    assert player.missing_subscriptions == SUBSCRIPTION_SERVICES


async def _subscribe_with_failing_speaker(player: SonosPlayer) -> None:
    """Let the given player attempt to subscribe to a speaker that cannot be reached."""
    with (
        patch.object(player, "_subscribe_target", AsyncMock(side_effect=OSError("unreachable"))),
        patch.object(player, "update_state"),
    ):
        async with asyncio.timeout(5):
            await player.subscribe()


async def test_failed_subscribe_marks_the_speaker_offline() -> None:
    """A failed subscription must take the speaker offline and release the lock."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())

    await _subscribe_with_failing_speaker(player)

    assert player.available is False
    assert not player._subscription_lock.locked()


async def test_speaker_can_resubscribe_after_a_failed_subscribe() -> None:
    """A speaker that failed to subscribe must still be able to subscribe later."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    await _subscribe_with_failing_speaker(player)

    with patch.object(player, "_subscribe_target", AsyncMock()) as subscribe_target:
        async with asyncio.timeout(5):
            await player.subscribe()

    assert subscribe_target.await_count == len(SUBSCRIPTION_SERVICES)


async def test_speaker_taken_offline_mid_subscribe_keeps_no_subscriptions() -> None:
    """A speaker that goes offline while subscribing must not keep the subscriptions it created."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    subscribing = asyncio.Event()
    speaker_responds = asyncio.Event()

    async def _slow_subscribe_target(_target: object, _callback: object) -> None:
        subscribing.set()
        await speaker_responds.wait()
        player._subscriptions.append(MagicMock(unsubscribe=AsyncMock()))

    with (
        patch.object(player, "_subscribe_target", _slow_subscribe_target),
        patch.object(player, "update_state"),
    ):
        subscribe_task = asyncio.create_task(player.subscribe())
        await subscribing.wait()

        offline_task = asyncio.create_task(player.offline())
        await asyncio.sleep(0)
        assert not offline_task.done()

        speaker_responds.set()
        async with asyncio.timeout(5):
            await asyncio.gather(subscribe_task, offline_task)

    assert player.available is False
    assert player._subscriptions == []
    assert player.missing_subscriptions == SUBSCRIPTION_SERVICES


async def test_speaker_going_offline_is_not_resubscribed_halfway() -> None:
    """No new subscriptions may be created while a speaker is still going offline."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    unsubscribing = asyncio.Event()
    speaker_responds = asyncio.Event()

    async def _slow_unsubscribe() -> None:
        unsubscribing.set()
        await speaker_responds.wait()

    player._subscriptions = [MagicMock(unsubscribe=_slow_unsubscribe)]

    with (
        patch.object(player, "_subscribe_target", AsyncMock()) as subscribe_target,
        patch.object(player, "update_state"),
    ):
        offline_task = asyncio.create_task(player.offline())
        await unsubscribing.wait()

        subscribe_task = asyncio.create_task(player.subscribe())
        await asyncio.sleep(0)
        subscribe_target.assert_not_called()

        speaker_responds.set()
        async with asyncio.timeout(5):
            await asyncio.gather(offline_task, subscribe_task)

    assert subscribe_target.await_count == len(SUBSCRIPTION_SERVICES)
