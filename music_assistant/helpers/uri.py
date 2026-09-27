"""Helpers for creating/parsing URI's."""

import asyncio
import os
import re
from typing import Final
from urllib.parse import parse_qs, urlsplit

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidProviderID, InvalidProviderURI
from music_assistant_models.helpers import create_uri as create_uri_org

base62_length22_id_pattern = re.compile(r"^[a-zA-Z0-9]{22}$")

# plain stream URLs that resolve to the builtin provider, which takes the URL as its item_id
BUILTIN_URL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "rtsp://", "rtmp://")

# the media types a provider publishes a canonical share URL for
_CANONICAL_URL_TYPES: Final[frozenset[MediaType]] = frozenset(
    {MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK}
)
# providers whose artist, album and track ids are plain numbers
_NUMERIC_ID_PROVIDERS: Final[frozenset[str]] = frozenset({"tidal", "deezer", "apple_music"})

# share URL hosts of the form open.<provider>.com, by provider domain
_OPEN_HOSTS: Final[dict[str, str]] = {
    "open.spotify.com": "spotify",
    "open.qobuz.com": "qobuz",
}

_APPLE_TYPE_MAP: Final[dict[str, MediaType]] = {
    "station": MediaType.PLAYLIST,
    "playlist": MediaType.PLAYLIST,
    "album": MediaType.ALBUM,
    "artist": MediaType.ARTIST,
    "song": MediaType.TRACK,
}
_APPLE_HOSTS: Final[tuple[str, ...]] = (
    "music.apple.com",
    "itunes.apple.com",
    "geo.itunes.apple.com",
)

_DEEZER_TYPE_MAP: Final[dict[str, MediaType]] = {
    "track": MediaType.TRACK,
    "album": MediaType.ALBUM,
    "artist": MediaType.ARTIST,
    "playlist": MediaType.PLAYLIST,
    "show": MediaType.PODCAST,
    "episode": MediaType.PODCAST_EPISODE,
}

_QOBUZ_TYPE_MAP: Final[dict[str, MediaType]] = {
    "album": MediaType.ALBUM,
    "interpreter": MediaType.ARTIST,
}

_DISCOGS_TYPE_MAP: Final[dict[MediaType, str]] = {
    MediaType.ARTIST: "artist",
    MediaType.ALBUM: "release",
}

# create alias to original create_uri function
create_uri = create_uri_org


def valid_base62_length22(item_id: str) -> bool:
    """Validate Spotify style ID."""
    return bool(base62_length22_id_pattern.match(item_id))


def valid_id(provider: str, media_type: MediaType, item_id: str) -> bool:
    """Validate Provider ID."""
    if provider == "spotify":
        return valid_base62_length22(item_id)
    if provider in _NUMERIC_ID_PROVIDERS and media_type in _CANONICAL_URL_TYPES:
        return item_id.isdigit()
    return True


async def parse_uri(uri: str, validate_id: bool = False) -> tuple[MediaType, str, str]:
    """
    Try to parse URI to Mass identifiers.

    Returns Tuple: MediaType, provider_instance_id_or_domain, item_id
    """
    try:
        if uri.startswith(("http://", "https://")) and (share_url := _parse_share_url(uri)):
            # public share URL of a streaming service
            media_type, provider_instance_id_or_domain, item_id = share_url
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
        # IndexError covers truncated share URLs with no path segments.
        msg = f"Not a valid Music Assistant uri: {uri}"
        raise InvalidProviderURI(msg) from err
    if validate_id and not valid_id(provider_instance_id_or_domain, media_type, item_id):
        msg = f"Invalid {provider_instance_id_or_domain} ID: {item_id} found in URI: {uri}"
        raise InvalidProviderID(msg)
    return (media_type, provider_instance_id_or_domain, item_id)


def canonical_provider_url(
    provider_domain: str, media_type: MediaType, item_id: str, storefront: str | None = None
) -> str | None:
    """
    Return the public URL of a provider item in the form MusicBrainz links to it.

    :param provider_domain: Domain of the music provider the item belongs to.
    :param media_type: Media type of the item.
    :param item_id: The provider's item id.
    :param storefront: Apple Music storefront (country code) the URL is scoped to.
    :return: The URL, or None when the provider has no canonical URL for this kind of item.
    """
    if media_type not in _CANONICAL_URL_TYPES:
        return None
    kind = media_type.value
    if provider_domain == "spotify":
        return f"https://open.spotify.com/{kind}/{item_id}"
    if provider_domain == "tidal":
        return f"https://tidal.com/{kind}/{item_id}"
    if provider_domain == "deezer":
        return f"https://www.deezer.com/{kind}/{item_id}"
    if provider_domain == "apple_music" and storefront and media_type != MediaType.TRACK:
        return f"https://music.apple.com/{storefront}/{kind}/{item_id}"
    if provider_domain == "ytmusic" and media_type == MediaType.ARTIST:
        return f"https://music.youtube.com/channel/{item_id}"
    return None


def apple_storefront_from_url(url: str) -> str | None:
    """
    Return the storefront (country code) an Apple Music URL is scoped to.

    :param url: Apple Music share URL, e.g. ``https://music.apple.com/us/album/name/123``.
    """
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in _APPLE_HOSTS:
        return None
    # a storefront is a two-letter country code; a URL without one starts with the type
    storefront = next((segment for segment in parsed.path.split("/") if segment), "")
    if len(storefront) == 2 and storefront.isascii() and storefront.isalpha():
        return storefront
    return None


def discogs_id_from_url(url: str, media_type: MediaType) -> str | None:
    """
    Return the Discogs id of an artist or release URL.

    :param url: Discogs URL, e.g. ``https://www.discogs.com/artist/3840``.
    :param media_type: Kind of Discogs entity the URL must point to: ARTIST or ALBUM.
    :return: The numeric id, or None when the URL is not that kind of Discogs entity
        (a Discogs master, for instance, is not a release).
    """
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in ("discogs.com", "www.discogs.com"):
        return None
    path = [segment for segment in parsed.path.split("/") if segment]
    if len(path) < 2 or path[0] != _DISCOGS_TYPE_MAP.get(media_type):
        return None
    # the id may carry a name slug: https://www.discogs.com/release/1234-Artist-Title
    match = re.match(r"\d+", path[1])
    return match.group(0) if match else None


def _parse_share_url(uri: str) -> tuple[MediaType, str, str] | None:
    """
    Parse the public share URL of a streaming service.

    :param uri: The URL to parse.
    :return: (media_type, provider_domain, item_id), or None when the host is not a known
        streaming service. A known host with a truncated or unsupported path raises
        KeyError, ValueError or IndexError.
    """
    parsed = urlsplit(uri)
    host = parsed.netloc.lower()
    path = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    if domain := _OPEN_HOSTS.get(host):
        # https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e
        # https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT
        # https://open.qobuz.com/album/0634904032432
        if path and path[0].startswith("intl-"):
            path = path[1:]
        return (MediaType(path[0]), domain, path[1])
    if host in ("tidal.com", "listen.tidal.com"):
        # https://tidal.com/browse/track/123456
        # https://tidal.com/track/123456
        # https://listen.tidal.com/album/123456
        if path and path[0] == "browse":
            path = path[1:]
        return (MediaType(path[0]), "tidal", path[1])
    if host in ("qobuz.com", "www.qobuz.com"):
        # https://www.qobuz.com/us-en/album/{slug}/{id}
        # https://www.qobuz.com/us-en/interpreter/{slug}/{id}
        return (_QOBUZ_TYPE_MAP[path[1]], "qobuz", path[3])
    if host == "music.youtube.com":
        return _parse_ytmusic_url(path, query)
    if host in _APPLE_HOSTS:
        return _parse_apple_url(path, query)
    if host in ("deezer.com", "www.deezer.com"):
        return _parse_deezer_url(path)
    return None


def _parse_ytmusic_url(path: list[str], query: dict[str, list[str]]) -> tuple[MediaType, str, str]:
    """Parse the path and query of a YouTube Music share URL."""
    # https://music.youtube.com/channel/{channel_id}
    # https://music.youtube.com/watch?v={video_id}
    # a release share URL (playlist?list=OLAK5uy_...) names no album browse id, so it
    # is unsupported like any other unknown path
    if path[0] == "channel":
        return (MediaType.ARTIST, "ytmusic", path[1])
    if path[0] == "watch" and (video_id := query.get("v", [""])[0]):
        return (MediaType.TRACK, "ytmusic", video_id)
    raise KeyError(path[0])


def _parse_apple_url(path: list[str], query: dict[str, list[str]]) -> tuple[MediaType, str, str]:
    """Parse the path and query of an Apple Music or (legacy) iTunes share URL."""
    # https://music.apple.com/{storefront}/{type}/{slug}/{id}
    # https://music.apple.com/{storefront}/{type}/{id}  (no slug)
    # https://itunes.apple.com/{storefront}/{type}/{slug}/id{id}  (legacy)
    if len(path) < 3:
        raise KeyError
    apple_type = path[1]
    # Track share links are album URLs with a ?i=<track_id> query param
    if apple_type == "album" and (track_id := query.get("i", [""])[0]):
        return (MediaType.TRACK, "apple_music", track_id)
    item_id = path[-1]
    if item_id.startswith("id") and item_id[2:].isdigit():
        item_id = item_id[2:]
    return (_APPLE_TYPE_MAP[apple_type], "apple_music", item_id)


def _parse_deezer_url(path: list[str]) -> tuple[MediaType, str, str]:
    """Parse the path of a Deezer share URL."""
    # https://www.deezer.com/track/123456
    # https://www.deezer.com/en/track/123456 (with locale)
    # https://deezer.com/album/789
    # Find the type segment by checking against the known map
    for index, segment in enumerate(path[:-1]):
        if segment in _DEEZER_TYPE_MAP:
            if not path[index + 1].isdigit():
                raise KeyError
            return (_DEEZER_TYPE_MAP[segment], "deezer", path[index + 1])
    raise KeyError
