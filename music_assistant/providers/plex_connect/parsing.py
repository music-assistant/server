"""Small stateless parsing helpers shared across the Plex remote control mixins."""

from __future__ import annotations

from typing import Any


def plex_item_fields(item: Any) -> tuple[str | None, int | None]:
    """
    Return the ``(key, playQueueItemID)`` of a Plex play queue item.

    Either field may be absent on a given item, in which case it is returned as None.

    :param item: A plexapi play queue item.
    """
    return getattr(item, "key", None), getattr(item, "playQueueItemID", None)


def plex_key_for_item(media_item: Any, provider_instance: str) -> str | None:
    """
    Return the Plex item_id mapped to the given provider instance, if any.

    :param media_item: A Music Assistant media item (or None).
    :param provider_instance: The Plex provider instance_id to match.
    """
    if not media_item:
        return None
    return next(
        (
            mapping.item_id
            for mapping in media_item.provider_mappings
            if mapping.provider_instance == provider_instance
        ),
        None,
    )
