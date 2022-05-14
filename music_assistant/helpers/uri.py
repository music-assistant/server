"""Helpers for creating/parsing URI's."""

from typing import Tuple

from music_assistant.models.enums import MediaType, ProviderType
from music_assistant.models.errors import MusicAssistantError


def parse_uri(uri: str) -> Tuple[MediaType, ProviderType, str]:
    """
    Try to parse URI to Mass identifiers.

    Returns Tuple: MediaType, provider, item_id
    """
    try:
        if uri.startswith("https://open."):
            # public share URL (e.g. Spotify or Qobuz, not sure about others)
            # https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e
            provider = ProviderType.parse(uri.split(".")[1])
            media_type_str = uri.split("/")[3]
            media_type = MediaType(media_type_str)
            item_id = uri.split("/")[4].split("?")[0]
        elif "://" in uri:
            # music assistant-style uri
            # provider://media_type/item_id
            provider = ProviderType.parse(uri.split("://")[0])
            media_type_str = uri.split("/")[2]
            media_type = MediaType(media_type_str)
            item_id = uri.split(f"{media_type_str}/")[1]
        elif ":" in uri:
            # spotify new-style uri
            provider, media_type_str, item_id = uri.split(":")
            provider = ProviderType.parse(provider)
            media_type = MediaType(media_type_str)
        else:
            raise KeyError
    except (TypeError, AttributeError, ValueError, KeyError) as err:
        raise MusicAssistantError(f"Not a valid Music Assistant uri: {uri}") from err
    return (media_type, provider, item_id)


def create_uri(media_type: MediaType, provider: ProviderType, item_id: str) -> str:
    """Create Music Assistant URI from MediaItem values."""
    return f"{provider.value}://{media_type.value}/{item_id}"
