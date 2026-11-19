"""Share-link payload models and serialization."""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import asdict, dataclass
from typing import Literal

from music_assistant_models.enums import MediaType

from music_assistant.providers.share_provider.links import (
    is_valid_artwork_url,
    is_valid_provider_link,
)

SUPPORTED_SHARE_MEDIA_TYPES = frozenset({MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST})
SHARE_FORMAT_VERSION = "1"
MAX_ENCODED_PAYLOAD_SIZE = 65536
MAX_DECODED_PAYLOAD_SIZE = 65536


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
    art_url: str | None = None
    provider_urls: dict[str, str] | None = None
    version: str | None = None
    duration: int | None = None

    def to_base64(self) -> str:
        """Encode a versioned, raw-deflated JSON payload as URL-safe base64."""
        data = _drop_none(asdict(self))
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > MAX_DECODED_PAYLOAD_SIZE:
            raise ValueError("Share payload is too large")
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        compressed = compressor.compress(raw) + compressor.flush()
        encoded = SHARE_FORMAT_VERSION + base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        if len(encoded) > MAX_ENCODED_PAYLOAD_SIZE:
            raise ValueError("Share payload is too large")
        return encoded

    @classmethod
    def from_base64(cls, encoded: str) -> SharePayload:
        """Decode a versioned, raw-deflated JSON payload from URL-safe base64."""
        if not isinstance(encoded, str) or not encoded.startswith(SHARE_FORMAT_VERSION):
            raise ValueError("Unsupported share format version")
        if len(encoded) > MAX_ENCODED_PAYLOAD_SIZE:
            raise ValueError("Share payload is too large")
        body = encoded[1:]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", body):
            raise ValueError("Invalid share payload")
        try:
            padded = body + "=" * (-len(body) % 4)
            compressed = base64.b64decode(padded, altchars=b"-_", validate=True)
            decompressor = zlib.decompressobj(-15)
            raw = decompressor.decompress(compressed, MAX_DECODED_PAYLOAD_SIZE + 1)
        except (binascii.Error, zlib.error) as err:
            raise ValueError("Invalid share payload") from err
        if len(raw) > MAX_DECODED_PAYLOAD_SIZE:
            raise ValueError("Share payload is too large")
        if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
            raise ValueError("Invalid compressed share payload")
        try:
            data = json.loads(raw)
        except ValueError as err:
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
        art_url = data.get("art_url")
        if not isinstance(art_url, str) or not is_valid_artwork_url(art_url):
            art_url = None
        provider_urls = _validated_provider_urls(
            data.get("provider_urls"), "Invalid share provider URLs"
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
            art_url=art_url,
            provider_urls=provider_urls,
            duration=share_duration,
        )


def _validated_provider_urls(value: object, error_message: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(error_message)
    refs: dict[str, str] = {}
    for domain, ref in value.items():
        if not isinstance(domain, str) or not isinstance(ref, str):
            raise TypeError(error_message)
        if not is_valid_provider_link(domain, ref):
            continue
        refs[domain] = ref
    return refs or None


def _drop_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value
