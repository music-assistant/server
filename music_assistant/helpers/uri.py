"""Helpers for creating/parsing URI's."""

import asyncio
import os
import re
from typing import Final

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidProviderID, InvalidProviderURI
from music_assistant_models.helpers import create_uri as create_uri_org

base62_length22_id_pattern = re.compile(r"^[a-zA-Z0-9]{22}$")

# plain stream URLs that resolve to the builtin provider, which takes the URL as its item_id
BUILTIN_URL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "rtsp://", "rtmp://")

# create alias to original create_uri function
create_uri = create_uri_org


def valid_base62_length22(item_id: str) -> bool:
    """Validate Spotify style ID."""
    return bool(base62_length22_id_pattern.match(item_id))


def valid_id(provider: str, item_id: str) -> bool:
    """Validate Provider ID."""
    if provider == "spotify":
        return valid_base62_length22(item_id)
    return True


async def parse_uri(uri: str, validate_id: bool = False) -> tuple[MediaType, str, str]:  # noqa: PLR0915
    """
    Try to parse URI to Mass identifiers.

    Returns Tuple: MediaType, provider_instance_id_or_domain, item_id
    """
    try:
        if uri.startswith("https://open."):
            # public share URL (e.g. Spotify or Qobuz, not sure about others)
            # https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e
            provider_instance_id_or_domain = uri.split(".")[1]
            media_type_str = uri.split("/")[3]
            media_type = MediaType(media_type_str)
            item_id = uri.split("/")[4].split("?", maxsplit=1)[0]
            if not item_id:
                # a truncated URL with a trailing slash (no id segment)
                raise KeyError
        elif uri.startswith("https://tidal.com/browse/"):
            # Tidal public share URL
            # https://tidal.com/browse/track/123456
            provider_instance_id_or_domain = "tidal"
            media_type_str = uri.split("/")[4]
            media_type = MediaType(media_type_str)
            item_id = uri.split("/")[5].split("?", maxsplit=1)[0]
            if not item_id:
                # a truncated URL with a trailing slash (no id segment)
                raise KeyError
        elif uri.startswith("https://music.apple.com/"):
            # Apple Music share URL
            # https://music.apple.com/{storefront}/{type}/{slug}/{id}
            _apple_type_map = {
                "station": MediaType.PLAYLIST,
                "playlist": MediaType.PLAYLIST,
                "album": MediaType.ALBUM,
                "artist": MediaType.ARTIST,
                "song": MediaType.TRACK,
            }
            parts = uri.rstrip("/").split("?")[0].split("/")
            # parts: ['https:', '', 'music.apple.com', '{sf}', '{type}', '{slug}', '{id}']
            # or:    ['https:', '', 'music.apple.com', '{sf}', '{type}', '{id}']  (no slug)
            # Track share links are album URLs with a ?i=<track_id> query param
            query = uri.split("?", 1)[1] if "?" in uri else ""
            track_id_from_query = next(
                (p.split("=", 1)[1] for p in query.split("&") if p.startswith("i=")),
                None,
            )
            if len(parts) >= 6:
                apple_type = parts[4]
                if apple_type == "album" and track_id_from_query:
                    provider_instance_id_or_domain = "apple_music"
                    media_type = MediaType.TRACK
                    item_id = track_id_from_query
                elif apple_type in _apple_type_map:
                    item_id = parts[-1]
                    if not item_id:
                        raise KeyError
                    provider_instance_id_or_domain = "apple_music"
                    media_type = _apple_type_map[apple_type]
                else:
                    raise KeyError
            else:
                raise KeyError
        elif uri.startswith(("https://www.deezer.com/", "https://deezer.com/")):
            # Deezer share URL
            # https://www.deezer.com/track/123456
            # https://www.deezer.com/en/track/123456 (with locale)
            # https://deezer.com/album/789
            _deezer_type_map = {
                "track": MediaType.TRACK,
                "album": MediaType.ALBUM,
                "artist": MediaType.ARTIST,
                "playlist": MediaType.PLAYLIST,
                "show": MediaType.PODCAST,
                "episode": MediaType.PODCAST_EPISODE,
            }
            parts = uri.rstrip("/").split("?")[0].split("/")
            # Find the type segment by checking against the known map
            deezer_type = None
            deezer_id = None
            for i, part in enumerate(parts):
                if part in _deezer_type_map and i + 1 < len(parts):
                    deezer_type = part
                    deezer_id = parts[i + 1]
                    break
            if deezer_type is None or not deezer_id or not deezer_id.isdigit():
                raise KeyError
            provider_instance_id_or_domain = "deezer"
            media_type = _deezer_type_map[deezer_type]
            item_id = deezer_id
        elif uri.startswith(BUILTIN_URL_SCHEMES):
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
    except (TypeError, AttributeError, ValueError, KeyError, IndexError) as err:
        # IndexError covers a recognized share-URL prefix that is truncated
        # (e.g. "https://open.spotify.com" with no path segments to split out)
        msg = f"Not a valid Music Assistant uri: {uri}"
        raise InvalidProviderURI(msg) from err
    if validate_id and not valid_id(provider_instance_id_or_domain, item_id):
        msg = f"Invalid {provider_instance_id_or_domain} ID: {item_id} found in URI: {uri}"
        raise InvalidProviderID(msg)
    return (media_type, provider_instance_id_or_domain, item_id)
