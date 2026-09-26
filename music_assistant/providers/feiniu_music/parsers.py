"""Convert native library objects without exposing file paths or authentication."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType, ImageType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import UNKNOWN_ARTIST, UNKNOWN_ARTIST_ID_MBID

DOMAIN = "feiniu_music"


def number(value: Any) -> int:
    """Return a nonnegative integer or the model's unknown sentinel."""
    return value if type(value) is int and value >= 0 else 0


def audio_format(spec: dict[str, Any]) -> AudioFormat:
    """Translate known audio fields, leaving missing numeric fields unknown."""
    try:
        content_type = ContentType(str(spec.get("format", "?")))
    except ValueError:
        content_type = ContentType.UNKNOWN
    try:
        codec = ContentType(str(spec.get("codec", "?")))
    except ValueError:
        codec = ContentType.UNKNOWN
    return AudioFormat(
        content_type=content_type,
        codec_type=codec,
        sample_rate=number(spec.get("sampleRate")),
        bit_depth=number(spec.get("bitDepth")),
        channels=number(spec.get("channel")),
        bit_rate=number(spec.get("bitrate")) // 1000 or None,
    )


def parse_artist(data: dict[str, Any], instance: str) -> Artist:
    """Parse a native artist."""
    item = Artist(**_base(data, instance, "name", "Unnamed artist"))
    _image(item, data, instance)
    return item


def unknown_artist(instance: str) -> Artist:
    """Return MA's explicit unknown-artist placeholder, scoped to this instance."""
    item = parse_artist({"guid": UNKNOWN_ARTIST, "name": UNKNOWN_ARTIST}, instance)
    item.mbid = UNKNOWN_ARTIST_ID_MBID
    return item


def parse_album(data: dict[str, Any], instance: str) -> Album:
    """Parse a native album and its actual artist associations."""
    item = Album(
        **_base(data, instance, "name", "Untitled album"),
        year=number(data.get("originalReleaseYear")) or None,
        artists=UniqueList(
            parse_artist(x, instance) for x in (data.get("artists") or []) if isinstance(x, dict)
        ),
    )
    _image(item, data, instance)
    return item


def parse_track(data: dict[str, Any], instance: str) -> Track:
    """Parse stable identity and associations, excluding NAS paths and stream URLs."""
    base = _base(data, instance, "title", "Untitled track")
    spec = data.get("audioSpec") or {}
    base["provider_mappings"] = {
        ProviderMapping(
            item_id=base["item_id"],
            provider_domain=DOMAIN,
            provider_instance=instance,
            audio_format=audio_format(spec),
            available=not data.get("isCue", False),
        )
    }
    item = Track(
        **base,
        duration=number(data.get("duration")) // 1000,
        disc_number=number(data.get("discNo")),
        track_number=number(data.get("trackNo")),
        artists=UniqueList(
            parse_artist(x, instance) for x in (data.get("artists") or []) if isinstance(x, dict)
        ),
    )
    if not item.artists:
        item.artists.append(unknown_artist(instance))
    album = data.get("album")
    if isinstance(album, dict) and album.get("guid"):
        item.album = parse_album(album, instance)
    _image(item, data if data.get("coverId") else album or {}, instance)
    return item


def parse_playlist(data: dict[str, Any], instance: str) -> Playlist:
    """Parse a read-only playlist."""
    item = Playlist(**_base(data, instance, "name", "Untitled playlist"), is_editable=False)
    _image(item, data, instance)
    return item


def _base(data: dict[str, Any], instance: str, title_key: str, fallback: str) -> dict[str, Any]:
    guid = data.get("guid")
    if not isinstance(guid, str) or not guid:
        raise InvalidDataError("FeiNiu item lacks a stable GUID")
    name = data.get(title_key)
    return {
        "item_id": guid,
        "provider": instance,
        "name": name if isinstance(name, str) and name else fallback,
        "provider_mappings": {
            ProviderMapping(item_id=guid, provider_domain=DOMAIN, provider_instance=instance)
        },
    }


def _image(item: Artist | Album | Track | Playlist, data: dict[str, Any], instance: str) -> None:
    cover = data.get("coverId")
    if isinstance(cover, str) and cover:
        item.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB, provider=instance, path=cover, remotely_accessible=False
                )
            ]
        )
