"""
Server-side per-queue data for the Player Queues controller.

`PlayerQueueData` is the controller's internal record for a single queue: it holds the wire-facing
`PlayerQueue` plus the server-only state that used to live in a pile of parallel dicts keyed by
queue_id (its items, its dynamic-source items, and a handful of runtime-only fields). It also owns
the (de)serialization of the parts that must survive a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.media_items import ItemMapping, MediaItemType, media_from_dict
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS, MASS_LOGGER_NAME
from music_assistant.controllers.player_queues.constants import CACHE_FORMAT_VERSION
from music_assistant.controllers.player_queues.helpers import has_dynamic_source

if TYPE_CHECKING:
    from music_assistant.controllers.player_queues.helpers import CompareState

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.player_queues")


@dataclass(slots=True)
class PlayerQueueData:
    """The controller's server-side record for a single queue."""

    queue: PlayerQueue
    # the queue's items (the wire `PlayerQueue` only carries a count)
    items: list[QueueItem] = field(default_factory=list)
    # the full media items behind the queue's dynamic `sources`; mutated as finite sources are
    # added and retired, projected onto the lighter `queue.sources` for the wire
    source_items: list[MediaItemType] = field(default_factory=list)
    # the parent media items the user enqueued; the seed for autoplay's "similar" mode and the
    # human-readable active-playlist label. Persisted so autoplay survives a restart.
    enqueued_media_items: list[MediaItemType] = field(default_factory=list)
    # the user this queue plays for (drives per-user recency/filtering). Persisted.
    userid: str | None = None

    # runtime-only fields below; not persisted, reset to these defaults on restart
    prev_state: CompareState | None = None
    transitioning: bool = False
    play_action_refcount: int = 0
    last_counted_play: str | None = None
    # session_id whose flow stream was fully generated
    flow_buffer_completed: str | None = None
    # the current stream session id (set when a stream starts, cleared between sessions)
    session_id: str | None = None
    # per-item play log for the active flow-mode stream
    flow_mode_stream_log: list[PlayLogEntry] = field(default_factory=list)
    # queue_item_id most recently handed to the player as the next item
    next_item_id_enqueued: str | None = None
    # set when the queue items changed since the last cache write; the debounced saver writes the
    # (heavier) items payload only when this is set, while the small state is written every save
    items_cache_dirty: bool = False

    def to_cache(self) -> dict[str, Any]:
        """
        Return the persistable state as a versioned envelope (items are cached separately).

        The wire `PlayerQueue` snapshot is nested under `queue`; the server-only state that must
        survive a restart sits alongside it, so more server state can be persisted later without
        colliding with wire field names.
        """
        queue = self.queue.to_dict()
        # drop the derived/runtime wire fields that must not be restored verbatim
        for key in (
            "flow_mode",
            "current_item",
            "next_item",
            "index_in_buffer",
            "smart_fades_active",
            "smart_shuffle_active",
        ):
            queue.pop(key, None)
        return {
            "cache_format_version": CACHE_FORMAT_VERSION,
            "queue": queue,
            "enqueued_media_items": [item.to_dict() for item in self.enqueued_media_items],
            "source_items": [item.to_dict() for item in self.source_items],
            "userid": self.userid,
        }

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
        stored_version = state_data.get("cache_format_version")
        if stored_version is not None and stored_version != CACHE_FORMAT_VERSION:
            # a deliberate breaking change to the layout: don't try to read incompatible data
            raise ValueError(
                f"incompatible queue cache format {stored_version} "
                f"(expected {CACHE_FORMAT_VERSION})"
            )
        # the nested layout keeps the wire snapshot under `queue`; caches written before this
        # refactor stored the wire fields at the top level, so fall back to the whole dict for them
        queue_data = state_data.get("queue", state_data)
        # The scalar settings (shuffle/repeat/crossfade/autoplay/...) live on the wire queue and are
        # the part that must survive a restart; deserialize them first and independently of the
        # media payloads below, which are versioned MediaItem dicts far more likely to fail across a
        # provider/model change. A single unreadable item must not cost the user their settings.
        queue = PlayerQueue.from_dict(queue_data)
        # reset the play-action-in-progress flag on restore (MA may have been killed mid-action)
        queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
        # re-deserialize the source ItemMappings (from_dict/mashumaro can leave them as plain dicts);
        # the legacy `radio_source` key is still read for caches written before the rename
        queue.sources = [
            item
            for x in queue_data.get("sources", queue_data.get("radio_source", []))
            if isinstance(x, dict) and isinstance(item := media_from_dict(x), ItemMapping)
        ]
        enqueued_media_items = cls._full_media_items(
            state_data.get("enqueued_media_items"), queue.queue_id
        )
        source_items = cls._source_items_from_cache(state_data, queue, enqueued_media_items)
        queue.is_dynamic = has_dynamic_source(source_items)
        items: list[QueueItem] = []
        for item_data in items_data:
            try:
                items.append(QueueItem.from_cache(item_data))
            except Exception as err:
                LOGGER.warning("Skipping unreadable queue item for %s: %s", queue.queue_id, err)
        return cls(
            queue=queue,
            items=items,
            source_items=source_items,
            enqueued_media_items=enqueued_media_items,
            userid=state_data.get("userid"),
        )

    @staticmethod
    def _full_media_items(raw: list[Any] | None, queue_id: str) -> list[MediaItemType]:
        """Deserialize a list of full media items, skipping any that no longer deserialize."""
        result: list[MediaItemType] = []
        for x in raw or []:
            if not isinstance(x, dict):
                continue
            try:
                item = media_from_dict(x)
            except Exception as err:
                LOGGER.warning("Skipping unreadable media item for %s: %s", queue_id, err)
                continue
            # keep only full media items; ItemMappings are the lighter wire projection
            if not isinstance(item, ItemMapping):
                result.append(item)
        return result

    @staticmethod
    def _source_items_from_cache(
        state_data: dict[str, Any], queue: PlayerQueue, enqueued_media_items: list[MediaItemType]
    ) -> list[MediaItemType]:
        """Restore the queue's source items, skipping any that no longer deserialize."""
        raw_sources = state_data.get("source_items")
        if raw_sources is None:
            # legacy cache predating source_items persistence: rebuild the full source items by
            # matching the persisted `sources` ItemMappings against the enqueued media items
            by_uri = {item.uri: item for item in enqueued_media_items}
            return [by_uri[mapping.uri] for mapping in queue.sources if mapping.uri in by_uri]
        return PlayerQueueData._full_media_items(raw_sources, queue.queue_id)
