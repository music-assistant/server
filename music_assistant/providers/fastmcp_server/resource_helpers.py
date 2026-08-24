"""Serialization and brief converters shared by MCP resources."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, NamedTuple

from music_assistant_models.enums import MediaType

from .models import PlayerBrief, QueueBrief, QueueItemBrief

if TYPE_CHECKING:
    from collections.abc import Sequence


def to_brief_player(player: Any, active_queue: Any = None) -> PlayerBrief:
    """
    Convert a Player-like object to ``PlayerBrief``.

    :param player: Player-like object to summarize.
    :param active_queue: Active player queue, when available.
    """
    state_obj = getattr(player, "playback_state", None) or getattr(player, "state", None)
    state_value = (
        str(getattr(state_obj, "value", state_obj)) if state_obj is not None else "unknown"
    )

    player_state = getattr(player, "state", None)
    if player_state is not None and hasattr(player_state, "powered"):
        powered_val = bool(player_state.powered) if player_state.powered is not None else True
        current_media = getattr(player_state, "current_media", None)
    else:
        powered_val = bool(getattr(player, "powered", True))
        current_media = getattr(player, "current_media", None)

    current_item: str | None = None
    if current_media is not None:
        current_item = _str_or_none(getattr(current_media, "title", None)) or _str_or_none(
            getattr(current_media, "uri", None)
        )

    available_val = bool(getattr(player, "available", True))
    enabled_val = bool(getattr(player, "enabled", True))
    needs_setup_val = bool(getattr(player, "needs_setup", False))

    if player_state is not None and hasattr(player_state, "active_group"):
        active_group_val = _str_or_none(player_state.active_group)
        synced_to_val = _str_or_none(player_state.synced_to)
    else:
        active_group_val = _str_or_none(getattr(player, "active_group", None))
        synced_to_val = _str_or_none(getattr(player, "synced_to", None))

    volume_muted_val, group_volume_val, group_volume_muted_val = _volume_fields(
        player, player_state
    )

    if not available_val:
        state_value = "unavailable"
    elif not enabled_val:
        state_value = "disabled"
    elif needs_setup_val:
        state_value = "needs_setup"
    elif synced_to_val is not None or active_group_val is not None:
        state_value = "synced"
    elif active_queue is not None:
        queue_state = getattr(active_queue, "state", None)
        state_value = (
            str(getattr(queue_state, "value", queue_state))
            if queue_state is not None
            else state_value
        )

    external_source: str | None = None
    if active_queue is not None:
        now_playing = _external_now_playing(getattr(active_queue, "current_item", None))
        if now_playing is not None:
            external_source = now_playing.instance_id
            if now_playing.title:
                current_item = now_playing.title

    return PlayerBrief(
        player_id=str(getattr(player, "player_id", "")),
        name=str(getattr(player, "display_name", None) or getattr(player, "name", "")),
        state=state_value,
        volume_level=_int(getattr(player, "volume_level", None)),
        powered=powered_val,
        current_item=current_item,
        available=available_val,
        enabled=enabled_val,
        needs_setup=needs_setup_val,
        active_group=active_group_val,
        synced_to=synced_to_val,
        volume_muted=volume_muted_val,
        group_volume=group_volume_val,
        group_volume_muted=group_volume_muted_val,
        external_source=external_source,
    )


def to_brief_queue(
    queue: Any, items: Sequence[Any] | None = None, *, items_offset: int = 0
) -> QueueBrief:
    """
    Convert a PlayerQueue-like object to ``QueueBrief``.

    :param queue: Queue-like object to summarize.
    :param items: Optional queue rows to include.
    :param items_offset: Absolute index of the first included row.
    """
    repeat_mode = getattr(queue, "repeat_mode", None)
    repeat_value = str(getattr(repeat_mode, "value", repeat_mode)) if repeat_mode else "off"
    brief_items: list[QueueItemBrief] = []
    if items:
        for row_index, item in enumerate(items):
            now_playing = _external_now_playing(item)
            item_name = (
                now_playing.title
                if now_playing and now_playing.title
                else str(getattr(item, "name", ""))
            )
            brief_items.append(
                QueueItemBrief(
                    item_id=str(getattr(item, "queue_item_id", "")),
                    name=item_name,
                    index=items_offset + row_index,
                    duration=_int(getattr(item, "duration", None)),
                    artists=_names(getattr(getattr(item, "media_item", None), "artists", None)),
                )
            )
    raw_total = getattr(queue, "items", None)
    explicit_count = _int(raw_total) if isinstance(raw_total, int) else None
    if explicit_count is None:
        explicit_count = _int(
            getattr(queue, "items_count", None) or getattr(queue, "items_total", None)
        )
    return QueueBrief(
        queue_id=str(getattr(queue, "queue_id", "")),
        current_index=_int(getattr(queue, "current_index", None)),
        item_count=explicit_count,
        shuffle=bool(getattr(queue, "shuffle_enabled", False)),
        repeat=repeat_value,
        items=brief_items,
        available=bool(getattr(queue, "available", True)),
        index_in_buffer=_int(getattr(queue, "index_in_buffer", None)),
        next_insertable_index=_min_insert_index(queue),
        items_start_index=items_offset,
    )


def safe_active_queue(mass: Any, player_id: str) -> Any:
    """
    Resolve a player's active queue, degrading to ``None`` on errors.

    :param mass: Music Assistant instance.
    :param player_id: Player whose queue should be resolved.
    """
    try:
        return mass.player_queues.get_active_queue(player_id)
    except Exception:
        return None


def to_resource_text(value: Any) -> str | None:
    """
    Serialize a resource return value as JSON text.

    :param value: MA domain object, provider brief, JSON-compatible value, or None.
    """
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return json.dumps(value.to_dict(), ensure_ascii=False, default=str)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json.dumps(dataclasses.asdict(value), ensure_ascii=False, default=str)
    return json.dumps(value, ensure_ascii=False, default=str)


class _ExternalNowPlaying(NamedTuple):
    """External source provider and track title."""

    instance_id: str
    title: str | None


def _external_now_playing(queue_item: Any) -> _ExternalNowPlaying | None:
    """Return external source metadata for an audio-source queue item."""
    streamdetails = getattr(queue_item, "streamdetails", None)
    if streamdetails is None:
        return None
    media_type = getattr(streamdetails, "media_type", None)
    media_type_value = (
        str(getattr(media_type, "value", media_type)) if media_type is not None else None
    )
    if media_type_value not in {MediaType.AUDIO_SOURCE.value, MediaType.PLUGIN_SOURCE.value}:
        return None
    provider = _str_or_none(getattr(streamdetails, "provider", None))
    if provider is None:
        return None
    metadata = getattr(streamdetails, "stream_metadata", None)
    title = _str_or_none(getattr(metadata, "title", None)) if metadata is not None else None
    return _ExternalNowPlaying(provider, title)


def _int(value: Any) -> int | None:
    """Coerce an optional integer without surfacing incompatible values."""
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _min_insert_index(queue: object) -> int:
    """Return the first queue index where new rows may be inserted."""
    floor = getattr(queue, "current_index", None)
    floor_value = floor if isinstance(floor, int) else -1
    buffered = getattr(queue, "index_in_buffer", None)
    if isinstance(buffered, int):
        floor_value = max(floor_value, buffered)
    return floor_value + 1


def _names(items: Any) -> list[str]:
    """Return stable display names from optional item objects."""
    if not items:
        return []
    return [str(getattr(item, "name", item)) for item in items]


def _str_or_none(value: Any) -> str | None:
    """Convert a present value to string."""
    return None if value is None else str(value)


def _volume_fields(player: Any, player_state: Any) -> tuple[bool | None, int | None, bool | None]:
    """Extract mute and group-volume fields from canonical player state first."""
    if player_state is not None and hasattr(player_state, "volume_muted"):
        raw_muted = player_state.volume_muted
        volume_muted = bool(raw_muted) if raw_muted is not None else None
    else:
        raw_muted = getattr(player, "volume_muted", None)
        volume_muted = bool(raw_muted) if raw_muted is not None else None

    if player_state is not None and hasattr(player_state, "group_volume"):
        group_volume = _int(player_state.group_volume)
        raw_group_muted = getattr(player_state, "group_volume_muted", None)
        group_muted = bool(raw_group_muted) if raw_group_muted is not None else None
    else:
        group_volume = _int(getattr(player, "group_volume", None))
        raw_group_muted = getattr(player, "group_volume_muted", None)
        group_muted = bool(raw_group_muted) if raw_group_muted is not None else None
    return volume_muted, group_volume, group_muted
