"""Share-link payload models and serialization."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass
from typing import Literal

from music_assistant_models.enums import MediaType

from music_assistant.providers.share_provider.links import (
    expand_artwork_ref,
    expand_provider_ref,
)

SUPPORTED_SHARE_MEDIA_TYPES = frozenset({MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST})


@dataclass
class SharePayload:
    """Provider-agnostic share payload encoded in the share link."""

    type: Literal["track", "album", "artist"]
    title: str
    artist: str | None = None
    artists: list[str] | None = None
    album: str | None = None
    isrc: str | None = None
    mbid: str | None = None
    mb_track: str | None = None
    barcode: str | None = None
    art_ref: str | None = None
    provider_refs: dict[str, str] | None = None
    version: str | None = None
    duration: int | None = None

    def to_base64(self) -> str:
        """Encode payload as URL-safe base64 JSON."""
        data = _drop_none(asdict(self))
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()

    @classmethod
    def from_base64(cls, encoded: str) -> SharePayload:
        """Decode payload from URL-safe base64 JSON."""
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            data = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        except (binascii.Error, ValueError) as err:
            raise ValueError("Invalid share payload") from err
        if not isinstance(data, dict):
            raise TypeError("Invalid share payload")
        if data.get("type") not in {media_type.value for media_type in SUPPORTED_SHARE_MEDIA_TYPES}:
            raise ValueError("Unsupported share payload")
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            raise ValueError("Share payload title is missing")
        for field_name in (
            "artist",
            "version",
            "album",
            "isrc",
            "mbid",
            "mb_track",
            "barcode",
        ):
            value = data.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Invalid share payload field: {field_name}")
        raw_artists = data.get("artists")
        if raw_artists is not None and (
            not isinstance(raw_artists, list)
            or any(not isinstance(artist, str) or not artist.strip() for artist in raw_artists)
        ):
            raise ValueError("Invalid share artist credits")
        payload_artists = (
            [artist.strip() for artist in raw_artists] if raw_artists is not None else None
        )
        share_duration = data.get("duration")
        if share_duration is not None and (type(share_duration) is not int or share_duration < 0):
            raise ValueError("Invalid share track duration")
        art_ref = data.get("art_ref")
        if not isinstance(art_ref, str) or expand_artwork_ref(art_ref) is None:
            art_ref = None
        provider_refs = _validated_provider_refs(
            data.get("provider_refs"), "Invalid share provider references"
        )
        return cls(
            type=data["type"],
            title=data["title"],
            artist=data.get("artist"),
            artists=payload_artists,
            version=data.get("version"),
            album=data.get("album"),
            isrc=data.get("isrc"),
            mbid=data.get("mbid"),
            mb_track=data.get("mb_track"),
            barcode=data.get("barcode"),
            art_ref=art_ref,
            provider_refs=provider_refs,
            duration=share_duration,
        )


def _validated_provider_refs(value: object, error_message: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(error_message)
    refs: dict[str, str] = {}
    for domain, ref in value.items():
        if not isinstance(domain, str) or not isinstance(ref, str):
            raise TypeError(error_message)
        if expand_provider_ref(domain, ref) is None:
            continue
        refs[domain] = ref
    return refs or None


def _drop_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value
