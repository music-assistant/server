"""Utility helpers for the Apple Music provider."""

from __future__ import annotations

import re
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError


def is_library_id(library_id: Any) -> bool:
    """Return True if the ID matches the Apple Music library ID format."""
    if not isinstance(library_id, str):
        return False
    return bool(re.fullmatch(r"[ailp]\.[a-zA-Z0-9]+", library_id))


def is_catalog_id(catalog_id: str) -> bool:
    """Return True if the ID is a catalog ID (numeric or starts with 'pl.')."""
    return catalog_id.isnumeric() or catalog_id.startswith("pl.")


def translate_media_type_to_apple_type(media_type: MediaType) -> str:
    """Translate a MediaType to the Apple Music API endpoint segment."""
    match media_type:
        case MediaType.ARTIST:
            return "artists"
        case MediaType.ALBUM:
            return "albums"
        case MediaType.TRACK:
            return "songs"
        case MediaType.PLAYLIST:
            return "playlists"
    raise MusicAssistantError(f"Unsupported media type: {media_type}")
