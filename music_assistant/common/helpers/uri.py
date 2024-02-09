"""Helpers for creating/parsing URI's."""

import os

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.errors import MusicAssistantError


def parse_uri(uri: str) -> tuple[MediaType, str, str]:
    """Try to parse URI to Mass identifiers.

    Returns Tuple: MediaType, provider_instance_id_or_domain, item_id
    """
    try:
        if uri.startswith("https://open."):
            # public share URL (e.g. Spotify or Qobuz, not sure about others)
            # https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e
            provider_instance_id_or_domain = uri.split(".")[1]
            media_type_str = uri.split("/")[3]
            media_type = MediaType(media_type_str)
            item_id = uri.split("/")[4].split("?")[0]
        elif uri.startswith(("http://", "https://")):
            # Translate a plain URL to the URL provider
            provider_instance_id_or_domain = "url"
            media_type = MediaType.UNKNOWN
            item_id = uri
        elif "://" in uri:
            # music assistant-style uri
            # provider://media_type/item_id
            provider_instance_id_or_domain = uri.split("://")[0]
            media_type_str = uri.split("/")[2]
            media_type = MediaType(media_type_str)
            item_id = uri.split(f"{media_type_str}/")[1]
        elif ":" in uri:
            # spotify new-style uri
            provider_instance_id_or_domain, media_type_str, item_id = uri.split(":")
            media_type = MediaType(media_type_str)
        elif os.path.isfile(uri):
            # Translate a local file (which is not from file provider) to the URL provider
            provider_instance_id_or_domain = "url"
            media_type = MediaType.TRACK
            item_id = uri
        else:
            raise KeyError
    except (TypeError, AttributeError, ValueError, KeyError) as err:
        msg = f"Not a valid Music Assistant uri: {uri}"
        raise MusicAssistantError(msg) from err
    return (media_type, provider_instance_id_or_domain, item_id)


def create_uri(media_type: MediaType, provider_instance_id_or_domain: str, item_id: str) -> str:
    """Create Music Assistant URI from MediaItem values."""
    return f"{provider_instance_id_or_domain}://{media_type.value}/{item_id}"
