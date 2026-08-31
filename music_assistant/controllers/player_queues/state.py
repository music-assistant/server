"""
Server-side per-queue state for the Player Queues controller.

Two distinct types model a queue. `PlayerQueue` (in the models package) is the lightweight *wire
snapshot* shared with API clients — the client-relevant playback state and flags only.
`PlayerQueueData`, defined here, is the *complete server-side record*: it wraps that wire
`PlayerQueue` and adds the state that never leaves the server — the ordered queue items, the full
media items behind the dynamic sources, the enqueued parent items, the owning user, the transient
stream-session fields, and a handful of runtime-only fields. It also owns the (de)serialization of
the parts that must survive a restart; the wire model itself carries no cache logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    MediaItemType,
    media_from_dict,
)
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS, MASS_LOGGER_NAME
from music_assistant.controllers.player_queues.constants import CACHE_FORMAT_VERSION
from music_assistant.controllers.player_queues.helpers import has_dynamic_source

if TYPE_CHECKING:
    from music_assistant.controllers.player_queues.helpers import CompareState

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.player_queues")

# playback-progress fields on the queue snapshot that advance constantly while playing; a change in
# only these does not warrant a fresh cache write, so they are ignored when deciding whether the
# persisted state actually changed (see `cache_significant`).
_VOLATILE_CACHE_FIELDS = ("elapsed_time", "elapsed_time_last_updated", "playback_speed")


@dataclass(slots=True)
class PlayerQueueData:
    """The complete server-side record for a queue: the wire `PlayerQueue` plus all server-only state."""

    queue: PlayerQueue
    # the queue's items (the wire `PlayerQueue` only carries a count)
    items: list[QueueItem] = field(default_factory=list)
    # the full media items behind the queue's dynamic `sources`; mutated as finite sources are
    # added and retired, projected onto the lighter `queue.sources` for the wire
    source_items: list[MediaItemType] = field(default_factory=list)
    # the parent media items the user enqueued; the seed for autoplay's "similar" mode.
    # Persisted so autoplay survives a restart.
    enqueued_media_items: list[MediaItemType] = field(default_factory=list)
    # the enqueued albums already credited as played; an album is credited once per time it is
    # enqueued, so re-enqueueing it (or a new play that clears the enqueued list) arms it again.
    # Persisted alongside the enqueued items, so a restart does not re-credit the same enqueue.
    credited_albums: set[Album] = field(default_factory=set)
    # the user this queue plays for (drives per-user recency/filtering). Persisted.
    userid: str | None = None
    # the crossfade/autoplay toggles the user flips from the player controls. None means the queue
    # still follows the global default; flipping a toggle pins it to an explicit value. Persisted.
    crossfade_override: bool | None = None
    autoplay_override: bool | None = None

    # runtime-only fields below; not persisted, reset to these defaults on restart
    prev_state: CompareState | None = None
    transitioning: bool = False
    play_action_refcount: int = 0
    last_counted_play: str | None = None
    # session_id whose flow stream was fully generated
    flow_buffer_completed: str | None = None
    # session_id whose flow stream ended because the queue ran out of items, as opposed
    # to ending early to restart on a format change or a live item
    flow_queue_exhausted: str | None = None
    # the current stream session id (set when a stream starts, cleared between sessions)
    session_id: str | None = None
    # per-item play log for the active flow-mode stream
    flow_mode_stream_log: list[PlayLogEntry] = field(default_factory=list)
    # queue_item_id most recently handed to the player as the next item
    next_item_id_enqueued: str | None = None
    # set when the queue items changed since the last cache write; the debounced saver writes the
    # (heavier) items payload only when this is set
    items_cache_dirty: bool = False
    # the significant part of the state last written to cache (volatile playback-progress fields
    # stripped); lets the debounced saver skip a redundant write when nothing meaningful changed
    last_saved_state: dict[str, Any] | None = None

    def to_cache(self) -> dict[str, Any]:
        """
        Return the persistable state as a versioned envelope (items are cached separately).

        The wire `PlayerQueue` snapshot is nested under `queue`; the server-only state that must
        survive a restart sits alongside it, so more server state can be persisted later without
        colliding with wire field names.
        """
        queue = self.queue.to_dict()
        # drop the derived/runtime wire fields that must not be restored verbatim; crossfade/
        # autoplay are derived from their overrides now, and dont_stop_the_music_enabled is the
        # deprecated autoplay mirror, which would otherwise resurrect it via __pre_deserialize__
        for key in (
            "flow_mode",
            "current_item",
            "next_item",
            "index_in_buffer",
            "smart_fades_active",
            "smart_shuffle_active",
            "crossfade_enabled",
            "autoplay_enabled",
            "dont_stop_the_music_enabled",
        ):
            queue.pop(key, None)
        return {
            "cache_format_version": CACHE_FORMAT_VERSION,
            "queue": queue,
            "enqueued_media_items": [item.to_dict() for item in self.enqueued_media_items],
            "credited_albums": sorted(
                uri for album in self.credited_albums if (uri := album.uri) is not None
            ),
            "source_items": [item.to_dict() for item in self.source_items],
            "userid": self.userid,
            "crossfade_override": self.crossfade_override,
            "autoplay_override": self.autoplay_override,
        }

    @staticmethod
    def cache_significant(state: dict[str, Any]) -> dict[str, Any]:
        """
        Return the change-detection view of a `to_cache` dict.

        Strips the volatile playback-progress fields from the queue snapshot so the debounced saver
        only writes when persist-worthy content (settings, items, sources, current index, ...)
        actually changed, not merely because the elapsed time advanced.
        """
        queue = {k: v for k, v in state["queue"].items() if k not in _VOLATILE_CACHE_FIELDS}
        return {**state, "queue": queue}

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
        # `sources` is a typed list[ItemMapping] that mashumaro deserializes all-or-nothing, so a
        # single unreadable mapping would abort the whole restore. Pull it out (and the legacy
        # `radio_source` key for pre-rename caches) before from_dict and rebuild it resiliently,
        # skipping any mapping that no longer deserializes.
        raw_sources = queue_data.get("sources", queue_data.get("radio_source", []))
        wire = {k: v for k, v in queue_data.items() if k not in ("sources", "radio_source")}
        queue = PlayerQueue.from_dict(wire)
        # reset the play-action-in-progress flag on restore (MA may have been killed mid-action)
        queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
        queue.sources = [
            item
            for item in cls._deserialize_media(raw_sources, queue.queue_id, "queue source")
            if isinstance(item, ItemMapping)
        ]
        enqueued_media_items = [
            item
            for item in cls._deserialize_media(
                state_data.get("enqueued_media_items"), queue.queue_id, "enqueued item"
            )
            if not isinstance(item, ItemMapping)
        ]
        # only an album still in the enqueued list can be looked up again, so the credits are
        # restored by matching the persisted uris against it
        credited_uris = set(state_data.get("credited_albums") or [])
        credited_albums = {
            item
            for item in enqueued_media_items
            if isinstance(item, Album) and item.uri in credited_uris
        }
        source_items = cls._source_items_from_cache(state_data, queue, enqueued_media_items)
        queue.is_dynamic = has_dynamic_source(source_items)
        items: list[QueueItem] = []
        for item_data in items_data:
            try:
                items.append(QueueItem.from_cache(item_data))
            except Exception as err:
                LOGGER.warning("Skipping unreadable queue item for %s: %s", queue.queue_id, err)
        # keep the wire-facing count consistent with the items actually restored (some may have been
        # skipped above), so refill / "running low" logic reads the right number
        queue.items = len(items)
        return cls(
            queue=queue,
            items=items,
            source_items=source_items,
            enqueued_media_items=enqueued_media_items,
            credited_albums=credited_albums,
            userid=state_data.get("userid"),
            crossfade_override=state_data.get("crossfade_override"),
            autoplay_override=state_data.get("autoplay_override"),
        )

    @staticmethod
    def legacy_toggle_state(state_data: dict[str, Any]) -> dict[str, bool]:
        """
        Return the crossfade/autoplay toggles stored by a cache snapshot predating the overrides.

        Keyed by `"crossfade"` / `"autoplay"`, and empty when the snapshot already carries the
        override fields (nothing to import) or stored no value for a toggle.

        :param state_data: The cached state dict (as written by `to_cache`).
        """
        if "crossfade_override" in state_data:
            return {}
        # caches written before this refactor stored the wire fields at the top level
        queue_data = state_data.get("queue", state_data)
        result: dict[str, bool] = {}
        if (crossfade := queue_data.get("crossfade_enabled")) is not None:
            result["crossfade"] = bool(crossfade)
        autoplay = queue_data.get("autoplay_enabled", queue_data.get("dont_stop_the_music_enabled"))
        if autoplay is not None:
            result["autoplay"] = bool(autoplay)
        return result

    @staticmethod
    def _deserialize_media(
        raw: list[Any] | None, queue_id: str, label: str
    ) -> list[MediaItemType | ItemMapping]:
        """Deserialize media dicts, skipping (and logging) any that no longer deserialize."""
        result: list[MediaItemType | ItemMapping] = []
        for x in raw or []:
            if not isinstance(x, dict):
                continue
            try:
                result.append(media_from_dict(x))
            except Exception as err:
                LOGGER.warning("Skipping unreadable %s for %s: %s", label, queue_id, err)
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
        return [
            item
            for item in PlayerQueueData._deserialize_media(
                raw_sources, queue.queue_id, "source item"
            )
            if not isinstance(item, ItemMapping)
        ]
