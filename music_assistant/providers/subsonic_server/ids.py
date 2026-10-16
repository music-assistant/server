"""
Subsonic ids for Music Assistant media items.

A library item gets ``<prefix>-<library id>`` (``al-12`` is library album 12). An item that only
exists on a music provider, such as an album track that is not in the library, gets
``<prefix>-p<token>``, where the token is the provider instance and the provider item id,
base64url encoded.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import create_uri

from .constants import ErrorCode
from .response import SubsonicError

LIBRARY = "library"
PROVIDER_MARKER = "p"
PREFIX_BY_MEDIA_TYPE: dict[MediaType, str] = {
    MediaType.ARTIST: "ar",
    MediaType.ALBUM: "al",
    MediaType.TRACK: "tr",
    MediaType.PLAYLIST: "pl",
}
MEDIA_TYPE_BY_PREFIX: dict[str, MediaType] = {
    prefix: media_type for media_type, prefix in PREFIX_BY_MEDIA_TYPE.items()
}


@dataclass(frozen=True)
class ItemRef:
    """A media item referenced by a Subsonic id."""

    media_type: MediaType
    item_id: str
    provider: str = LIBRARY

    @property
    def is_library(self) -> bool:
        """Return True when the id refers to a library item."""
        return self.provider == LIBRARY

    @property
    def uri(self) -> str:
        """Return the Music Assistant uri of the item."""
        return create_uri(self.media_type, self.provider, self.item_id)


def to_subsonic_id(media_type: MediaType, item_id: str | int, provider: str = LIBRARY) -> str:
    """
    Return the Subsonic id of a media item.

    :param media_type: The media type of the item.
    :param item_id: The library id, or the provider item id.
    :param provider: ``library`` or the provider instance id the item comes from.
    """
    prefix = PREFIX_BY_MEDIA_TYPE[media_type]
    if provider == LIBRARY:
        return f"{prefix}-{item_id}"
    token = base64.urlsafe_b64encode(f"{provider}\n{item_id}".encode()).decode().rstrip("=")
    return f"{prefix}-{PROVIDER_MARKER}{token}"


def parse_subsonic_id(value: str, *allowed: MediaType) -> ItemRef:
    """
    Return the item a Subsonic id refers to.

    :param value: The Subsonic id.
    :param allowed: The media types the caller accepts; any type when empty.
    """
    prefix, _, rest = value.partition("-")
    media_type = MEDIA_TYPE_BY_PREFIX.get(prefix)
    if media_type is None or (allowed and media_type not in allowed):
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Unknown id: {value}")
    if rest.isdigit():
        return ItemRef(media_type, rest)
    if rest.startswith(PROVIDER_MARKER):
        token = rest.removeprefix(PROVIDER_MARKER)
        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError) as err:
            raise SubsonicError(ErrorCode.NOT_FOUND, f"Unknown id: {value}") from err
        provider, separator, item_id = decoded.partition("\n")
        if separator and provider and item_id:
            return ItemRef(media_type, item_id, provider)
    raise SubsonicError(ErrorCode.NOT_FOUND, f"Unknown id: {value}")
