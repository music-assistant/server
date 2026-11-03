"""Tests that a player joining a group keeps the position of its own parked queue."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant.models.player import Player

QUEUE_ID = "member"
LEADER_ID = "leader"
PROVIDER = "library"
RESUMED_AT = 9
PARKED_AT = RESUMED_AT + 2
# where the leader's track is when the member joins its group
LEADER_POSITION = 42.765


def _track() -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="own",
        name="Own track",
        duration=200,
        media_item=Track(
            item_id="own",
            provider=PROVIDER,
            name="Own track",
            duration=200,
            provider_mappings={
                ProviderMapping(item_id="own", provider_domain=PROVIDER, provider_instance=PROVIDER)
            },
        ),
    )


def _controller(item: QueueItem) -> PlayerQueuesController:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Member", available=True, items=1
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[item])}

    async def load_item(queue_item: QueueItem, seek_position: int = 0, **_kwargs: Any) -> None:
        queue_item.streamdetails = StreamDetails(
            provider=PROVIDER,
            item_id="own",
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
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.music.get_playback_speed = AsyncMock(return_value=1.0)
    ctrl.mass.players.play_media = AsyncMock()
    return ctrl


def _member(state: PlaybackState, elapsed: float, *, synced_to: str | None = None) -> Player:
    """Build the member player as the queue controller sees it."""
    source = synced_to or QUEUE_ID
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        type=PlayerType.PLAYER,
        extra_data={},
        active_output_protocol=None,
        current_media=SimpleNamespace(
            source_id=source, queue_item_id="other" if synced_to else "own", uri=None
        ),
        state=SimpleNamespace(
            name="Member",
            available=True,
            playback_state=state,
            corrected_elapsed_time=elapsed,
            group_members=[],
            active_source=source,
        ),
    )
    return cast("Player", player)


def _player_update(ctrl: PlayerQueuesController, player: Player) -> None:
    """Feed a player update, the immediate position correction first like the players controller."""
    ctrl.on_player_elapsed_time_corrected(player)
    ctrl.on_player_update(player, {})


@pytest.mark.parametrize(
    ("parked_state", "parked_elapsed"),
    [(PlaybackState.IDLE, 0), (PlaybackState.PAUSED, PARKED_AT - RESUMED_AT)],
)
async def test_group_join_keeps_own_queue_position(
    parked_state: PlaybackState, parked_elapsed: float
) -> None:
    """The leader's position never lands in the parked queue of a member joining its group."""
    ctrl = _controller(_track())
    queue = ctrl._queue_data[QUEUE_ID].queue
    await ctrl.play_index(QUEUE_ID, 0, seek_position=RESUMED_AT)
    ctrl.on_player_update(_member(PlaybackState.PLAYING, 0), {})
    ctrl.on_player_update(_member(PlaybackState.PLAYING, PARKED_AT - RESUMED_AT), {})
    # parked outside MA, so the queue keeps no resume position of its own
    ctrl.on_player_update(_member(parked_state, parked_elapsed), {})
    assert int(queue.elapsed_time) == PARKED_AT

    _player_update(ctrl, _member(PlaybackState.PLAYING, LEADER_POSITION, synced_to=LEADER_ID))
    # leaving the group stops the member, which resets its position
    _player_update(ctrl, _member(PlaybackState.IDLE, 0))

    assert int(queue.elapsed_time) == PARKED_AT
    await ctrl.resume(QUEUE_ID)
    load_item = cast("AsyncMock", ctrl._load_item)
    assert load_item.call_args.kwargs["seek_position"] == PARKED_AT
