"""Tests that the music provider that streamed an item learns how its playback progresses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.models.music_provider import MusicProvider

QUEUE_ID = "player1"
STREAMING = "plex--1"
# another provider with a mapping on the same tracks, which did not stream them
OTHER = "tidal--1"


def _track(item_id: str) -> QueueItem:
    mappings = {
        ProviderMapping(item_id=item_id, provider_domain="plex", provider_instance=STREAMING),
        ProviderMapping(item_id=item_id, provider_domain="tidal", provider_instance=OTHER),
    }
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=180,
        media_item=Track(
            item_id=item_id,
            provider="library",
            name=item_id,
            duration=180,
            provider_mappings=mappings,
        ),
    )


def _close(target: Any, *_args: Any, **_kwargs: Any) -> None:
    """Stand in for create_task: the calls are recorded, the coroutines never run."""
    if hasattr(target, "close"):
        target.close()


def _controller() -> tuple[PlayerQueuesController, dict[str, Any]]:
    """Build a bare controller holding two tracks, with stream loading stubbed."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player", available=True, items=2
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[_track("t1"), _track("t2")])}

    async def load_item(queue_item: QueueItem, seek_position: int = 0, **_kwargs: Any) -> None:
        queue_item.streamdetails = StreamDetails(
            provider=STREAMING,
            item_id=f"/library/metadata/{queue_item.queue_item_id}",
            audio_format=AudioFormat(),
            media_type=MediaType.TRACK,
            seek_position=seek_position,
        )

    ctrl._load_item = AsyncMock(side_effect=load_item)  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.closing = False
    ctrl.mass.create_task.side_effect = _close
    ctrl.mass.players.get_player.return_value.offline_by_power_control = False
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.music.get_playback_speed = AsyncMock(return_value=1.0)
    ctrl.mass.players.play_media = AsyncMock()
    providers: dict[str, Any] = {}
    for instance_id in (STREAMING, OTHER):
        prov = MagicMock(spec=MusicProvider)
        prov.instance_id = instance_id
        prov.available = True
        prov.unloading = False
        prov.on_playback_progress = Mock()
        providers[instance_id] = prov
    ctrl.mass.get_provider.side_effect = lambda instance_id, **_kwargs: providers.get(instance_id)
    return ctrl, providers


def _player_reports(
    ctrl: PlayerQueuesController, state: PlaybackState, item_id: str, elapsed: float
) -> None:
    """Feed a player update reporting the item it plays and its position."""
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        resolve_output_protocol=lambda: SimpleNamespace(
            current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id=item_id, uri=None)
        ),
        name="Player",
        available=True,
        playback_state=state,
        corrected_elapsed_time=elapsed,
        group_members=[],
    )
    ctrl._update_queue_from_player(player)  # type: ignore[arg-type]


def _hook_calls(prov: Any) -> list[tuple[str, PlaybackState, int]]:
    """Return the item, state and position of each progress report the provider received."""
    return [
        (prov_item_id.rsplit("/", 1)[-1], state, report.seconds_played)
        for report, prov_item_id, state in (
            c.args for c in prov.on_playback_progress.call_args_list
        )
    ]


def _recorded_plays(ctrl: PlayerQueuesController) -> list[tuple[str, int, bool]]:
    """Return the item, position and is_playing of each play forwarded to the playlog."""
    mark_item_played = cast("Mock", ctrl.mass.music.mark_item_played)
    return [
        (c.args[0].item_id, c.kwargs["seconds_played"], c.kwargs["is_playing"])
        for c in mark_item_played.call_args_list
    ]


async def test_streaming_provider_follows_the_playback() -> None:
    """Start, pause, resume, the next track and stop reach the provider that streamed them."""
    ctrl, providers = _controller()
    await ctrl.play_index(QUEUE_ID, 0)
    ctrl.mark_item_served(QUEUE_ID, "t1")

    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 30)
    _player_reports(ctrl, PlaybackState.PAUSED, "t1", 40)
    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 40)
    # the next track is enqueued on the player and streamed
    await ctrl._load_item(ctrl._queue_data[QUEUE_ID].items[1])
    ctrl.mark_item_served(QUEUE_ID, "t2")
    _player_reports(ctrl, PlaybackState.PLAYING, "t2", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "t2", 12)
    _player_reports(ctrl, PlaybackState.IDLE, "t2", 12)

    assert _hook_calls(providers[STREAMING]) == [
        ("t1", PlaybackState.PLAYING, 0),
        ("t1", PlaybackState.PLAYING, 30),
        ("t1", PlaybackState.PAUSED, 40),
        ("t1", PlaybackState.PLAYING, 40),
        ("t1", PlaybackState.IDLE, 40),
        ("t2", PlaybackState.PLAYING, 0),
        ("t2", PlaybackState.IDLE, 12),
    ]
    providers[OTHER].on_playback_progress.assert_not_called()
    # the playlog still gets no report of a start, a resume or the next track starting
    assert _recorded_plays(ctrl) == [
        ("t1", 30, True),
        ("t1", 40, False),
        ("t1", 40, False),
        ("t2", 12, False),
    ]


async def test_item_never_served_is_not_reported() -> None:
    """A track the player names but never received audio for reaches no provider."""
    ctrl, providers = _controller()
    await ctrl.play_index(QUEUE_ID, 0)

    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 30)
    _player_reports(ctrl, PlaybackState.PAUSED, "t1", 40)

    providers[STREAMING].on_playback_progress.assert_not_called()


async def test_stream_error_still_ends_the_playback() -> None:
    """A track whose stream failed midway is not recorded, but the provider learns it stopped."""
    ctrl, providers = _controller()
    await ctrl.play_index(QUEUE_ID, 0)
    ctrl.mark_item_served(QUEUE_ID, "t1")

    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 30)
    streamdetails = ctrl._queue_data[QUEUE_ID].items[0].streamdetails
    assert streamdetails is not None
    streamdetails.stream_error = True
    _player_reports(ctrl, PlaybackState.IDLE, "t1", 30)

    assert _hook_calls(providers[STREAMING])[-1] == ("t1", PlaybackState.IDLE, 30)
    assert ("t1", 30, False) not in _recorded_plays(ctrl)


async def test_unavailable_streaming_instance_is_not_replaced() -> None:
    """Another account of the same service never receives the progress of an item it did not stream."""
    ctrl, providers = _controller()
    providers[STREAMING].available = False
    other_account = MagicMock(spec=MusicProvider)
    other_account.instance_id = "plex--2"
    other_account.available = True
    other_account.unloading = False
    other_account.on_playback_progress = Mock()
    # mass.get_provider widens an unavailable instance to another one of the same domain
    cast("Mock", ctrl.mass.get_provider).side_effect = (
        lambda _instance_id, return_unavailable=False: (
            providers[STREAMING] if return_unavailable else other_account
        )
    )
    await ctrl.play_index(QUEUE_ID, 0)
    ctrl.mark_item_served(QUEUE_ID, "t1")

    _player_reports(ctrl, PlaybackState.PLAYING, "t1", 30)
    _player_reports(ctrl, PlaybackState.IDLE, "t1", 40)

    other_account.on_playback_progress.assert_not_called()
    providers[STREAMING].on_playback_progress.assert_not_called()
