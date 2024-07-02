"""Helpers for creating/parsing URI's."""

import asyncio
import os
import re

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.errors import InvalidProviderID, InvalidProviderURI

base62_length22_id_pattern = re.compile(r"^[a-zA-Z0-9]{22}$")


def valid_base62_length22(item_id: str) -> bool:
    """Validate Spotify style ID."""
    return bool(base62_length22_id_pattern.match(item_id))


def valid_id(provider: str, item_id: str) -> bool:
    """Validate Provider ID."""
    if provider == "spotify":
        return valid_base62_length22(item_id)
    else:
        return True


async def parse_uri(uri: str, validate_id: bool = False) -> tuple[MediaType, str, str]:
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
        elif uri.startswith("https://tidal.com/browse/"):
            # Tidal public share URL
            # https://tidal.com/browse/track/123456
            provider_instance_id_or_domain = "tidal"
            media_type_str = uri.split("/")[4]
            media_type = MediaType(media_type_str)
            item_id = uri.split("/")[5].split("?")[0]
        elif uri.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            # Translate a plain URL to the builtin provider
            provider_instance_id_or_domain = "builtin"
            media_type = MediaType.UNKNOWN
            item_id = uri
        elif "://" in uri and len(uri.split("/")) >= 4:
            # music assistant-style uri
            # provider://media_type/item_id
            provider_instance_id_or_domain, rest = uri.split("://", 1)
            media_type_str, item_id = rest.split("/", 1)
            media_type = MediaType(media_type_str)
        elif ":" in uri and len(uri.split(":")) == 3:
            # spotify new-style uri
            provider_instance_id_or_domain, media_type_str, item_id = uri.split(":")
            media_type = MediaType(media_type_str)
        elif "/" in uri and await asyncio.to_thread(os.path.isfile, uri):
            # Translate a local file (which is not from a file provider!) to the builtin provider
            provider_instance_id_or_domain = "builtin"
            media_type = MediaType.UNKNOWN
            item_id = uri
        else:
            raise KeyError
    except (TypeError, AttributeError, ValueError, KeyError) as err:
        msg = f"Not a valid Music Assistant uri: {uri}"
        raise InvalidProviderURI(msg) from err
    if validate_id and not valid_id(provider_instance_id_or_domain, item_id):
        msg = f"Invalid {provider_instance_id_or_domain} ID: {item_id} found in URI: {uri}"
        raise InvalidProviderID(msg)
    return (media_type, provider_instance_id_or_domain, item_id)


def create_uri(media_type: MediaType, provider_instance_id_or_domain: str, item_id: str) -> str:
    """Create Music Assistant URI from MediaItem values."""
    return f"{provider_instance_id_or_domain}://{media_type.value}/{item_id}"
