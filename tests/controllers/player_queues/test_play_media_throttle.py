"""Tests that resolving the media to play goes through the request throttler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import QueueOption
from music_assistant_models.media_items import Artist, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER


async def test_resolving_the_media_to_play_is_throttled() -> None:
    """Looking up and expanding the requested media runs without a throttler bypass."""
    artist = Artist(item_id="ar1", provider="spotify--abc", name="Ar", provider_mappings=set())
    track = Track(
        item_id="t1",
        provider="spotify--abc",
        name="T1",
        provider_mappings={
            ProviderMapping(
                item_id="t1", provider_domain="spotify", provider_instance="spotify--abc"
            )
        },
    )
    bypassed: list[bool] = []

    def record_bypass[T](result: T) -> T:
        bypassed.append(BYPASS_THROTTLER.get())
        return result

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.music.get_item_by_uri = AsyncMock(side_effect=lambda *_: record_bypass(artist))
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl._apply_shuffle = AsyncMock()  # type: ignore[method-assign]
    ctrl._enqueue_with_option = AsyncMock()  # type: ignore[method-assign]
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda *_, **__: record_bypass([track])
    )
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}

    await ctrl._handle_play_media("q1", "spotify--abc://artist/ar1", QueueOption.PLAY)

    assert bypassed == [False, False]
    ctrl._enqueue_with_option.assert_awaited_once()
