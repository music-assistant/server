"""Helpers for collections."""

from music_assistant_models.enums import MediaType

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
