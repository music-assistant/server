"""Natural completion uses MA repeat semantics and rejects stale decoder callbacks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.providers.msx_bridge.player import MSXPlayer

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient


@pytest.mark.parametrize(
    ("repeat", "index", "expected"),
    [
        (RepeatMode.OFF, 0, 1),
        (RepeatMode.OFF, 1, None),
        (RepeatMode.ONE, 0, 0),
        (RepeatMode.ONE, 1, 1),
        (RepeatMode.ALL, 0, 1),
        (RepeatMode.ALL, 1, 0),
    ],
)
async def test_natural_completion_uses_real_queue_repeat_selection(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    player: MSXPlayer,
    repeat: RepeatMode,
    index: int,
    expected: int | None,
) -> None:
    """Duplicate callbacks never repeat a transition, including repeat-one cycles."""
    items = [
        QueueItem(queue_id=player.player_id, queue_item_id=str(i), name="duplicate", duration=8)
        for i in range(2)
    ]
    queue = PlayerQueue(
        queue_id=player.player_id,
        active=True,
        display_name="Queue",
        available=True,
        items=2,
        current_index=index,
        current_item=items[index],
        repeat_mode=repeat,
        state=PlaybackState.PLAYING,
    )
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller._queue_data = {player.player_id: PlayerQueueData(queue=queue, items=items)}
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.get_next_item = controller.get_next_item
    mass_mock.player_queues.get_active_queue.return_value = queue
    mass_mock.player_queues.get.return_value = queue
    mass_mock.player_queues.mark_ended = Mock()
    mass_mock.player_queues.stop = AsyncMock()

    async def select(_id: str, item_id: str) -> None:
        queue.current_index = int(item_id)
        queue.current_item = items[int(item_id)]
        await player.play_media(
            PlayerMedia(uri="http://ma/track", source_id=player.player_id, queue_item_id=item_id)
        )

    mass_mock.player_queues.play_index.side_effect = select
    await select(player.player_id, str(index))
    player.bind_native_completion("decoder-1", player.current_media)
    response = await http_client.get("/api/complete/msx_test?playback_id=decoder-1")
    assert response.status == 200
    if expected is None:
        assert player.playback_state == PlaybackState.IDLE
        assert player.current_media is None
        assert (await response.json())["response"]["data"]["action"] == (
            "[player:eject|player:hide]"
        )
        mass_mock.player_queues.stop.assert_not_awaited()
        mass_mock.player_queues.mark_ended.assert_not_called()
        mass_mock.player_queues.play_index.assert_not_awaited()
    else:
        assert queue.current_index == expected
        mass_mock.player_queues.play_index.assert_awaited_once_with(player.player_id, str(expected))
    count = mass_mock.player_queues.play_index.await_count
    response = await http_client.get("/api/complete/msx_test?playback_id=decoder-1")
    assert response.status == 200
    assert mass_mock.player_queues.play_index.await_count == count
