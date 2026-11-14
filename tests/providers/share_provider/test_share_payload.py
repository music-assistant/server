"""Test share payload tests."""

from __future__ import annotations

import base64
import json
import zlib
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Track,
    UniqueList,
)

from music_assistant.providers.share_provider import (
    SharePayload,
)
from music_assistant.providers.share_provider.links import (
    build_provider_links,
    is_valid_artwork_url,
    is_valid_provider_link,
)
from music_assistant.providers.share_provider.payload import (
    MAX_DECODED_PAYLOAD_SIZE,
    MAX_ENCODED_PAYLOAD_SIZE,
)
from music_assistant.providers.share_provider.resolver import ShareLinkResolver

from .helpers import BARCODE, _mass, _provider, _track


def _encode_data(data: object) -> str:
    """Encode arbitrary input independently of payload model validation."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(json.dumps(data).encode()) + compressor.flush()
    return "1" + base64.urlsafe_b64encode(compressed).decode().rstrip("=")


@pytest.mark.parametrize(
    ("configured_base_url", "expected_base_url"),
    [
        (None, "https://app.music-assistant.io/share"),
        ("https://shares.example", "https://shares.example"),
    ],
)
async def test_generate_share_link_encodes_payload_in_fragment(
    configured_base_url: str | None, expected_base_url: str
) -> None:
    """Generated share links use the configured/default base and contain a decodable payload."""
    provider = _provider()
    mass = _mass(provider)
    track = _track(item_id="track", name="Song")
    mass.music.get_item_by_uri = AsyncMock(return_value=track)
    cast("MagicMock", provider.config).get_value.return_value = configured_base_url

    link = await provider.generate_share_link("catalog--1://track/track")

    base_url, separator, encoded_payload = link.partition("#")
    assert separator
    assert base_url == f"{expected_base_url}/"
    payload = SharePayload.from_base64(encoded_payload)
    assert payload.type == "track"
    assert payload.title == "Song"
    mass.music.get_item_by_uri.assert_awaited_once_with("catalog--1://track/track")


async def test_build_album_payload_includes_barcode_mbid_and_provider_link() -> None:
    """Album shares carry the identifiers and provider links available to the sender."""
    provider = _provider()
    album = MagicMock()
    album.media_type = MediaType.ALBUM
    album.name = "Album"
    album.version = "Deluxe"
    album.artist_str = "Artist"
    album.image = None
    album.provider_mappings = [
        MagicMock(
            provider_domain="spotify",
            item_id="album/123",
            url="https://open.spotify.com/album/album%2F123",
        )
    ]
    album.get_external_id.side_effect = {
        ExternalID.MB_ALBUM: "album-mbid",
        ExternalID.BARCODE: BARCODE,
    }.__getitem__

    payload = await provider._build_payload(album)

    assert payload.type == "album"
    assert payload.version == "Deluxe"
    assert payload.mbid == "album-mbid"
    assert payload.barcode == BARCODE
    assert payload.provider_urls == {"spotify": "https://open.spotify.com/album/album%2F123"}


async def test_build_track_payload_includes_duration_for_core_matching() -> None:
    """New individual-track shares include duration as matching evidence."""
    provider = _provider()
    track = _track(item_id="track", duration=218)

    payload = await provider._build_payload(track)

    assert payload.duration == 218


async def test_track_payload_preserves_separate_artist_credits() -> None:
    """New individual-track shares preserve structured credits for Core matching."""
    provider = _provider()
    track = Track(
        item_id="multi-artist",
        provider="catalog--1",
        name="Song",
        duration=218,
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two", provider="catalog--1", name="Second", provider_mappings=set()
                ),
            ]
        ),
    )

    payload = await provider._build_payload(track)
    reconstructed = ShareLinkResolver(provider.mass)._track_from_payload(payload)

    assert payload.artist == "First, Second"
    assert payload.artists == ["First", "Second"]
    assert [artist.name for artist in reconstructed.artists] == ["First", "Second"]


async def test_album_payload_preserves_separate_artist_credits() -> None:
    """Album payloads preserve artist credits as separate structured values."""
    provider = _provider()
    album = Album(
        item_id="album",
        provider="catalog--1",
        name="Album",
        version="Deluxe",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two", provider="catalog--1", name="Second", provider_mappings=set()
                ),
            ]
        ),
    )

    payload = await provider._build_payload(album)
    reference = ShareLinkResolver._album_reference(payload)

    assert payload.artists == ["First", "Second"]
    assert ShareLinkResolver._matches_album_reference(payload, reference, album)


def test_name_only_album_is_not_reconstructed_as_a_builtin_mapping() -> None:
    """An album label stays share metadata rather than a fetchable builtin identity."""
    provider = _provider()
    payload = SharePayload(type="track", title="Track", artist="Artist", album="Album")

    item = provider._playlist_item_from_payload(payload)
    reconstructed = ShareLinkResolver(provider.mass)._track_from_payload(payload)

    assert item.album is None
    assert reconstructed.album is None
    assert SharePayload.from_base64(item.path.removeprefix("share://track/")).album == "Album"


async def test_share_base_url_is_an_instance_config_option() -> None:
    """The config controller can discover the share URL option on the provider."""
    entries = await _provider().get_config_entries()
    assert len(entries) == 1
    assert entries[0].key == "share_base_url"
    assert entries[0].default_value == "https://app.music-assistant.io/share"


@pytest.mark.parametrize(
    "url",
    [
        "https://archive.org/download/album/cover:front.jpg",
        "https://archive.org/download/album/cover.jpg?part=front:2",
        "https://ia800.archive.org/download/album/cover:front.jpg?format=full:2",
    ],
)
def test_artwork_urls_roundtrip_colons_in_path_and_query(url: str) -> None:
    """Artwork URLs remain intact rather than being encoded as host codes."""
    payload = SharePayload(type="album", title="Album", art_url=url)
    assert is_valid_artwork_url(url)
    assert SharePayload.from_base64(payload.to_base64()).art_url == url


def test_build_provider_links_keeps_apple_music_canonical_url() -> None:
    """Apple mappings carry a canonical storefront URL that must not be rebuilt."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/us/album/album-name/987654321?i=123456789",
        )
    ]

    assert build_provider_links(item) == {
        "apple_music": "https://music.apple.com/us/album/album-name/987654321?i=123456789"
    }


def test_build_provider_links_preserves_full_localized_urls() -> None:
    """Share payload URLs preserve the provider's host and localized path."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/at/album/a-little-bit-off/1487244856?i=1487244862",
        ),
        MagicMock(
            provider_domain="spotify",
            item_id="track-id",
            url="https://open.spotify.com/track/track-id",
        ),
    ]

    assert build_provider_links(item) == {
        "apple_music": "https://music.apple.com/at/album/a-little-bit-off/1487244856?i=1487244862",
        "spotify": "https://open.spotify.com/track/track-id",
    }


def test_build_provider_links_uses_only_mapping_urls() -> None:
    """Mappings without a provider URL do not get a guessed link."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(provider_domain="qobuz", item_id="Example Performer", url=None),
        MagicMock(provider_domain="qobuz", item_id="12345", url=None),
    ]
    assert build_provider_links(item) == {}

    item.provider_mappings = [
        MagicMock(
            provider_domain="qobuz",
            item_id="Artist Name",
            url="https://open.qobuz.com/artist/54321",
        ),
    ]
    assert build_provider_links(item) == {"qobuz": "https://open.qobuz.com/artist/54321"}


def test_payload_uses_full_urls_and_versioned_raw_deflate() -> None:
    """The version prefix identifies raw-deflated JSON with complete URLs."""
    apple_url = "https://music.apple.com/at/album/123?i=456"
    art_url = "https://is1-ssl.mzstatic.com/image/thumb/cover/1000x1000bb.jpg"
    payload = SharePayload(
        type="track",
        title="Track",
        version="Live",
        art_url=art_url,
        provider_urls={"apple_music": apple_url},
    )

    encoded = payload.to_base64()
    decoded = SharePayload.from_base64(encoded)
    assert encoded.startswith("1")
    body = encoded[1:]
    decoded_json = zlib.decompress(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), -15
    ).decode()

    assert decoded.art_url == art_url
    assert decoded.version == "Live"
    assert decoded.provider_urls == {"apple_music": apple_url}
    assert "https://" in decoded_json
    assert "art_ref" not in decoded_json
    assert "provider_refs" not in decoded_json
    assert '"v"' not in decoded_json


def test_build_provider_links_ignores_invalid_mapping_urls() -> None:
    """Provider mappings only expose URLs on their approved HTTPS host."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="javascript:alert(1)",
        )
    ]

    assert build_provider_links(item) == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://open.spotify.com/track/1",
        "https://open.spotify.com.evil.com/track/1",
        "https://user@open.spotify.com/track/1",
        "https://user:password@open.spotify.com/track/1",
    ],
)
def test_provider_links_reject_http_lookalike_hosts_and_userinfo(url: str) -> None:
    """Provider URLs must use the exact HTTPS host without userinfo."""
    assert not is_valid_provider_link("spotify", url)


@pytest.mark.parametrize("media_type", ["podcast", "playlist"])
def test_payload_rejects_unsupported_media_types(media_type: str) -> None:
    """Reject unknown media types before dispatching a share payload."""
    encoded = _encode_data({"type": media_type, "title": "Unsupported"})

    with pytest.raises(ValueError, match="Unsupported share payload"):
        SharePayload.from_base64(encoded)


def test_payload_version_is_outside_json() -> None:
    """Versioned links do not need a duplicate version field inside the JSON."""
    encoded = _encode_data({"type": "track", "title": "Track"})

    assert SharePayload.from_base64(encoded).title == "Track"


def test_track_payload_roundtrips_optional_duration() -> None:
    """Individual-track payloads preserve their optional duration."""
    payload = SharePayload(type="track", title="Track", duration=218)

    assert SharePayload.from_base64(payload.to_base64()).duration == 218


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_track_duration(duration: object) -> None:
    """Individual-track duration must be a nonnegative integer."""
    encoded = _encode_data({"type": "track", "title": "Track", "duration": duration})

    with pytest.raises(ValueError, match="Invalid share track duration"):
        SharePayload.from_base64(encoded)


def test_payload_drops_invalid_optional_links_and_artwork() -> None:
    """A malformed optional ref does not invalidate otherwise usable share data."""
    encoded = _encode_data(
        {
            "type": "track",
            "title": "Track",
            "art_url": "https://evil.example/art.jpg",
            "provider_urls": {
                "spotify": "https://open.spotify.com/track/123",
                "unknown": "https://open.spotify.com/track/456",
            },
        }
    )

    payload = SharePayload.from_base64(encoded)

    assert payload.art_url is None
    assert payload.provider_urls == {"spotify": "https://open.spotify.com/track/123"}


@pytest.mark.parametrize(
    ("domain", "url", "expected"),
    [
        ("ytmusic", "https://music.youtube.com/browse/MPREb_example", True),
        ("ytmusic", "https://music.youtube.com/playlist?list=OLAK5uy_abc", True),
        ("qobuz", "https://www.qobuz.com/us-en/album/album/123", True),
        ("tidal", "https://listen.tidal.com/album/123", True),
        ("spotify", "https://music.apple.com/de/album/123", False),
        ("spotify", "https://open.spotify.com/track/", False),
        ("spotify", "/track/123", False),
        ("spotify", "https://open.spotify.com", False),
    ],
)
def test_provider_urls_use_core_classification(domain: str, url: str, expected: bool) -> None:
    """Public item URL formats are validated by core rather than a plugin-owned table."""
    assert is_valid_provider_link(domain, url) is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://is1-ssl.mzstatic.com/art.jpg",
        "https://mzstatic.com.evil.example/art.jpg",
        "https://user@is1-ssl.mzstatic.com/art.jpg",
        "m:is1-ssl:/art.jpg",
    ],
)
def test_artwork_rejects_unsafe_urls_and_old_codes(url: str) -> None:
    """Only complete approved HTTPS artwork URLs are accepted."""
    assert not is_valid_artwork_url(url)


@pytest.mark.parametrize("encoded", ["", "2abc", "eyJ0eXBlIjoiYWxidW0ifQ", "1", "1***", "1a"])
def test_payload_rejects_invalid_versions_and_base64(encoded: str) -> None:
    """Neither an unknown version nor the unreleased old format is accepted."""
    with pytest.raises(ValueError, match=r"Unsupported share format version|Invalid share payload"):
        SharePayload.from_base64(encoded)


def test_payload_rejects_truncated_and_trailing_compressed_data() -> None:
    """A compressed link must contain exactly one complete deflate stream."""
    encoded = SharePayload(type="track", title="Track").to_base64()
    body = encoded[1:]
    compressed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    for malformed in (compressed[:-1], compressed + b"trailing"):
        invalid = "1" + base64.urlsafe_b64encode(malformed).decode().rstrip("=")
        with pytest.raises(ValueError, match="Invalid compressed share payload"):
            SharePayload.from_base64(invalid)


def test_payload_bounds_both_compressed_and_decompressed_input() -> None:
    """A tiny deflate bomb cannot allocate an unbounded decoded payload."""
    oversized = _encode_data({"type": "track", "title": "x" * MAX_DECODED_PAYLOAD_SIZE})
    with pytest.raises(ValueError, match="too large"):
        SharePayload.from_base64(oversized)
    with pytest.raises(ValueError, match="too large"):
        SharePayload.from_base64("1" + "a" * MAX_ENCODED_PAYLOAD_SIZE)
    with pytest.raises(ValueError, match="too large"):
        SharePayload(type="track", title="x" * MAX_DECODED_PAYLOAD_SIZE).to_base64()


def test_payload_accepts_exact_decoded_size_limit() -> None:
    """The maximum decoded JSON length is inclusive."""
    empty = json.dumps({"type": "track", "title": ""}, separators=(",", ":")).encode()
    payload = SharePayload(type="track", title="x" * (MAX_DECODED_PAYLOAD_SIZE - len(empty)))
    assert SharePayload.from_base64(payload.to_base64()) == payload
    with pytest.raises(ValueError, match="too large"):
        SharePayload(type="track", title=payload.title + "x").to_base64()


@pytest.mark.parametrize("data", [[], None, {"type": "track"}, {"type": "track", "title": 42}])
def test_payload_rejects_invalid_json_shapes(data: object) -> None:
    """Successful decompression does not bypass metadata validation."""
    with pytest.raises((TypeError, ValueError), match=r"Invalid share payload|title is missing"):
        SharePayload.from_base64(_encode_data(data))


def test_payload_roundtrips_unicode_and_compresses_full_urls() -> None:
    """UTF-8 metadata and complete URLs survive the versioned compressed format."""
    payload = SharePayload(
        type="album",
        title="Björk 日本語 🎵",
        artist="Björk",
        art_url="https://is1-ssl.mzstatic.com/image/thumb/cover/1000x1000bb.jpg",
        provider_urls={
            "apple_music": "https://music.apple.com/de/album/album-name/1442624385",
            "spotify": "https://open.spotify.com/album/4Z8W4fKeB5YxbusRsdQVPb",
        },
    )
    encoded = payload.to_base64()
    assert SharePayload.from_base64(encoded) == payload
    uncompressed = json.dumps(
        {
            "type": payload.type,
            "title": payload.title,
            "artist": payload.artist,
            "art_url": payload.art_url,
            "provider_urls": payload.provider_urls,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert len(encoded) < len(base64.urlsafe_b64encode(uncompressed))
