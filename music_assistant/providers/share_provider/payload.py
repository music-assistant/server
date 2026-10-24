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

SUPPORTED_SHARE_MEDIA_TYPES = frozenset(
    {MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST, MediaType.PLAYLIST}
)


@dataclass
class TrackStub:
    """Minimal track representation inside a playlist payload."""

    title: str
    artist: str | None = None
    isrc: str | None = None
    mbid: str | None = None
    mb_track: str | None = None
    album: str | None = None
    duration: int | None = None
    provider_refs: dict[str, str] | None = None
    version: str | None = None


@dataclass
class SharePayload:
    """Provider-agnostic share payload encoded in the share link."""

    type: Literal["track", "album", "artist", "playlist"]
    title: str
    artist: str | None = None
    album: str | None = None
    isrc: str | None = None
    mbid: str | None = None
    mb_track: str | None = None
    barcode: str | None = None
    art_ref: str | None = None
    provider_refs: dict[str, str] | None = None
    tracks: list[TrackStub] | None = None
    version: str | None = None

    def to_base64(self) -> str:
        """Encode payload as URL-safe base64 JSON."""
        data = {key: value for key, value in asdict(self).items() if value is not None}
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
        art_ref = data.get("art_ref")
        if art_ref is not None and (
            not isinstance(art_ref, str) or expand_artwork_ref(art_ref) is None
        ):
            raise ValueError("Invalid share artwork reference")
        provider_refs = _validated_provider_refs(
            data.get("provider_refs"), "Invalid share provider references"
        )
        raw_tracks = data.get("tracks")
        tracks: list[TrackStub] | None = None
        if raw_tracks is not None:
            if not isinstance(raw_tracks, list):
                raise ValueError("Invalid share playlist tracks")
            tracks = []
            for raw_track in raw_tracks:
                if not isinstance(raw_track, dict) or not isinstance(raw_track.get("title"), str):
                    raise TypeError("Invalid share playlist track")
                optional_fields = ("artist", "version", "isrc", "mbid", "mb_track", "album")
                if any(
                    raw_track.get(key) is not None and not isinstance(raw_track[key], str)
                    for key in optional_fields
                ):
                    raise ValueError("Invalid share playlist track metadata")
                duration = raw_track.get("duration")
                if duration is not None and (type(duration) is not int or duration < 0):
                    raise ValueError("Invalid share playlist track duration")
                tracks.append(
                    TrackStub(
                        title=raw_track["title"],
                        artist=raw_track.get("artist"),
                        version=raw_track.get("version"),
                        isrc=raw_track.get("isrc"),
                        mbid=raw_track.get("mbid"),
                        mb_track=raw_track.get("mb_track"),
                        album=raw_track.get("album"),
                        duration=duration,
                        provider_refs=_validated_provider_refs(
                            raw_track.get("provider_refs"),
                            "Invalid share playlist provider references",
                        ),
                    )
                )
        return cls(
            type=data["type"],
            title=data["title"],
            artist=data.get("artist"),
            version=data.get("version"),
            album=data.get("album"),
            isrc=data.get("isrc"),
            mbid=data.get("mbid"),
            mb_track=data.get("mb_track"),
            barcode=data.get("barcode"),
            art_ref=art_ref,
            provider_refs=provider_refs,
            tracks=tracks,
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
            raise ValueError(error_message)
        refs[domain] = ref
    return refs
