"""Helpers for collections."""

from collections.abc import Iterable
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import MediaCollection, MediaItem

from music_assistant.constants import COLLECTION_ITEM_ID_SEPARATOR


def get_collection_item_id(collection_name: str, item_media_type: MediaType) -> str:
    """Get item id for a collection."""
    return COLLECTION_ITEM_ID_SEPARATOR.join((item_media_type.value.lower(), collection_name))


def get_collection_name_from_item_id(collection_item_id: str) -> str:
    """Get collection's name from item id."""
    return collection_item_id.split(COLLECTION_ITEM_ID_SEPARATOR, maxsplit=1)[1]


def get_collection_item_media_type_from_item_id(collection_item_id: str) -> MediaType:
    """Get media_type of items in a collection."""
    return MediaType(collection_item_id.split(COLLECTION_ITEM_ID_SEPARATOR, maxsplit=1)[0].lower())


def has_available_item(items: Iterable[MediaItem | MediaCollection[Any]]) -> bool:
    """
    Return whether at least one item in a (possibly collapsed) listing is available.

    :param items: The listing to inspect; collections count as available when any of
        the items they hold is available.
    """
    for item in items:
        if isinstance(item, MediaCollection):
            # a collapsed collection carries no provider mappings of its own, so its
            # inherited `available` is always false: look at the items it holds instead
            if any(member.available for member in item.items):
                return True
        elif item.available:
            return True
    return False
