"""
Shared base for the Player Queues controller and its logic mixins.

The controller's loading, playback-tracking and stream-feeding logic is split across mixins
(see queue_loader/playback_tracker/stream_feeder). They operate on the controller's own per-queue
records and call back into its core operations. This base declares that shared surface - the
per-queue state, the stateful helper services and the core-operation signatures - so each mixin
type-checks on its own while the real implementations live on PlayerQueuesController.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.enums import QueueOption
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.autoplay import Autoplay
    from music_assistant.controllers.player_queues.managed_pool import ManagedPool
    from music_assistant.controllers.player_queues.media_resolver import MediaResolver
    from music_assistant.controllers.player_queues.smart_shuffle import SmartShuffle
    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.models.player import Player, PlayerMedia


class _PlayerQueuesBase(CoreController):
    """
    Shared state and core-operation contract for the player-queues logic mixins.

    Carries the per-queue records and stateful helper services the mixins operate on, plus the
    signatures of the controller operations they call into.
    """

    domain: str = "player_queues"

    # per-queue records + stateful helper services, populated in PlayerQueuesController.__init__
    _queue_data: dict[str, PlayerQueueData]
    _autoplay: Autoplay
    _smart_shuffle: SmartShuffle
    _managed_pool: ManagedPool
    _media_resolver: MediaResolver

    if TYPE_CHECKING:
        # core operations implemented on PlayerQueuesController
        def get(self, queue_id: str) -> PlayerQueue | None: ...
        def get_item(
            self, queue_id: str, item_id_or_index: int | str | None
        ) -> QueueItem | None: ...
        def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None: ...
        def get_next_item(self, queue_id: str, cur_index: int | str) -> QueueItem | None: ...
        def clear(self, queue_id: str, skip_stop: bool = False) -> None: ...
        def mark_ended(self, queue_id: str) -> None: ...
        def signal_update(self, queue_id: str, items_changed: bool = False) -> None: ...
        def on_player_update(
            self, player: Player, changed_values: dict[str, tuple[Any, Any]]
        ) -> None: ...
        def is_smart_shuffle_active(self, queue: PlayerQueue) -> bool: ...
        def store_sources(self, queue: PlayerQueue, items: list[MediaItemType]) -> None: ...
        async def load(
            self,
            queue_id: str,
            queue_items: list[QueueItem],
            insert_at_index: int = 0,
            keep_remaining: bool = True,
            keep_played: bool = True,
            shuffle: bool = False,
            pin_first: bool = False,
        ) -> None: ...
        async def play_index(
            self,
            queue_id: str,
            index: int | str,
            seek_position: int = 0,
            fade_in: bool = False,
        ) -> None: ...
        async def load_next_queue_item(self, queue_id: str, current_item_id: str) -> QueueItem: ...
        async def player_media_from_queue_item(self, queue_item: QueueItem) -> PlayerMedia: ...
        def _set_transitioning(self, queue_id: str, value: bool) -> None: ...
        def _clear(self, queue_id: str, skip_stop: bool = False) -> None: ...
        async def _cleanup_queue_audio_data(self, queue_id: str) -> None: ...
        async def _apply_shuffle(
            self, queue_id: str, option: QueueOption, shuffle: bool | None
        ) -> None: ...
        # implemented on QueueLoaderMixin; declared here for the cross-mixin call in PlaybackTracker
        def _apply_probed_duration(self, queue_item: QueueItem) -> None: ...
        async def _fill_dynamic_tracks(self, queue_id: str) -> None: ...
        async def _fill_autoplay_tracks(self, queue_id: str) -> None: ...
