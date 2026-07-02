"""
Server-side per-queue data for the Player Queues controller.

`PlayerQueueData` is the controller's internal record for a single queue: it holds the wire-facing
`PlayerQueue` plus the server-only state that used to live in a pile of parallel dicts keyed by
queue_id (its items, its dynamic-source items, and a handful of runtime-only fields). It also owns
the (de)serialization of the parts that must survive a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.media_items import ItemMapping, MediaItemType, media_from_dict
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues.helpers import has_dynamic_source

if TYPE_CHECKING:
    from music_assistant.controllers.player_queues.helpers import CompareState


@dataclass(slots=True)
class PlayerQueueData:
    """The controller's server-side record for a single queue."""

    queue: PlayerQueue
    # the queue's items (the wire `PlayerQueue` only carries a count)
    items: list[QueueItem] = field(default_factory=list)
    # the full media items behind the queue's dynamic `sources`; mutated as finite sources are
    # added and retired, projected onto the lighter `queue.sources` for the wire
    source_items: list[MediaItemType] = field(default_factory=list)

    # runtime-only fields below; not persisted, reset to these defaults on restart
    prev_state: CompareState | None = None
    transitioning: bool = False
    play_action_refcount: int = 0
    last_counted_play: str | None = None
    # session_id whose flow stream was fully generated
    flow_buffer_completed: str | None = None

    def to_cache(self) -> dict[str, Any]:
        """Return the state dict to persist (queue + source items); items are cached separately."""
        data = self.queue.to_cache()
        data["source_items"] = [item.to_dict() for item in self.source_items]
        return data

    def items_to_cache(self) -> list[dict[str, Any]]:
        """Return the cacheable representation of the queue items (only those with a media item)."""
        return [item.to_cache() for item in self.items if item.media_item is not None]

    @classmethod
    def from_cache(
        cls, state_data: dict[str, Any], items_data: list[dict[str, Any]]
    ) -> PlayerQueueData:
        """
        Rebuild a `PlayerQueueData` from its cached state and items.

        :param state_data: The cached state dict (as written by `to_cache`).
        :param items_data: The cached queue items (as written by `items_to_cache`).
        """
        queue = PlayerQueue.from_dict(state_data)
        # reset the play-action-in-progress flag on restore (MA may have been killed mid-action)
        queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
        # from_cache deserializes enqueued_media_items into full MediaItemType objects and sources
        # into ItemMappings (from_dict/mashumaro leaves them as plain dicts)
        queue.from_cache(state_data)
        if (raw_sources := state_data.get("source_items")) is not None:
            source_items = [
                item
                for x in raw_sources
                if isinstance(x, dict) and not isinstance(item := media_from_dict(x), ItemMapping)
            ]
        else:
            # legacy cache predating source_items persistence: rebuild the full source items by
            # matching the persisted `sources` ItemMappings against the enqueued media items
            by_uri = {item.uri: item for item in queue.enqueued_media_items}
            source_items = [
                by_uri[mapping.uri] for mapping in queue.sources if mapping.uri in by_uri
            ]
        queue.is_dynamic = has_dynamic_source(source_items)
        items = [QueueItem.from_cache(item_data) for item_data in items_data]
        return cls(queue=queue, items=items, source_items=source_items)
