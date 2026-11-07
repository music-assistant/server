"""Test share payload tests."""

from __future__ import annotations

import base64
import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import (
    InvalidDataError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Track,
    UniqueList,
)

from music_assistant.providers.share_provider import (
    SharePayload,
    TrackStub,
)
from music_assistant.providers.share_provider.links import (
    build_provider_links,
    build_provider_refs,
    compact_artwork_ref,
    expand_artwork_ref,
    expand_provider_ref,
    is_valid_provider_link,
)
from music_assistant.providers.share_provider.resolver import ShareLinkResolver

from .helpers import BARCODE, _mass, _provider, _track


@pytest.mark.parametrize(
    ("configured_base_url", "expected_base_url"),
    [
        (None, "https://share.music-assistant.io"),
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


async def test_generate_share_link_rejects_oversized_playlist() -> None:
    """Reject playlist payloads that cannot fit in a single API DataChannel message."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_item_by_uri = AsyncMock(return_value=MagicMock())
    payload = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="x" * (50 * 1024))],
    )

    with (
        patch.object(provider, "_build_payload", new=AsyncMock(return_value=payload)),
        pytest.raises(InvalidDataError) as err,
    ):
        await provider.generate_share_link("library://playlist/1")

    assert err.value.translation_key == "share_playlist_too_large"


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
    assert payload.provider_refs == {"spotify": "/album/album%2F123"}


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
    assert entries[0].default_value == "https://share.music-assistant.io"


@pytest.mark.parametrize(
    "url",
    [
        "https://archive.org/download/album/cover:front.jpg",
        "https://archive.org/download/album/cover.jpg?part=front:2",
        "https://ia800.archive.org/download/album/cover:front.jpg?format=full:2",
    ],
)
def test_artwork_refs_roundtrip_colons_in_path_and_query(url: str) -> None:
    """Artwork paths and queries retain colons when compacted and expanded."""
    ref = compact_artwork_ref(url)

    assert ref is not None
    assert expand_artwork_ref(ref) == url


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


def test_build_provider_refs_omits_provider_hosts() -> None:
    """Share payload references keep localized paths without full URLs."""
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

    assert build_provider_refs(item) == {
        "apple_music": "/at/album/a-little-bit-off/1487244856?i=1487244862",
        "spotify": "/track/track-id",
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


def test_payload_uses_compact_provider_and_artwork_refs_without_version_field() -> None:
    """Share metadata is encoded without a redundant protocol-version field."""
    apple_url = "https://music.apple.com/at/album/123?i=456"
    art_url = "https://is1-ssl.mzstatic.com/image/thumb/cover/1000x1000bb.jpg"
    art_ref = compact_artwork_ref(art_url)
    assert art_ref is not None
    payload = SharePayload(
        type="track",
        title="Track",
        version="Live",
        art_ref=art_ref,
        provider_refs={
            "apple_music": build_provider_refs(
                MagicMock(
                    provider_mappings=[
                        MagicMock(
                            provider_domain="apple_music",
                            item_id="456",
                            url=apple_url,
                        )
                    ]
                )
            )["apple_music"]
        },
    )

    encoded = payload.to_base64()
    decoded = SharePayload.from_base64(encoded)
    decoded_json = base64.urlsafe_b64decode(encoded).decode()

    assert decoded.art_ref == art_ref
    assert decoded.version == "Live"
    assert decoded.provider_refs == {
        "apple_music": "/at/album/123?i=456",
    }
    assert expand_artwork_ref(art_ref) == art_url
    assert expand_provider_ref("apple_music", decoded.provider_refs["apple_music"]) == apple_url
    assert "https://" not in decoded_json
    assert '"v"' not in decoded_json


def test_playlist_payload_omits_null_track_fields() -> None:
    """Nested playlist stubs omit null fields from their serialized payload."""
    payload = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Unversioned track")],
    )

    decoded = json.loads(base64.urlsafe_b64decode(payload.to_base64()).decode())

    assert decoded["tracks"] == [{"title": "Unversioned track"}]


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


async def test_payload_rejects_unsupported_media_types() -> None:
    """Reject unknown media types before dispatching a share payload."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "podcast", "title": "Podcast"}).encode()
    ).decode()

    with pytest.raises(ValueError, match="Unsupported share payload"):
        SharePayload.from_base64(encoded)


def test_payload_does_not_require_a_version_field() -> None:
    """The share payload contains no protocol-version marker."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track"}).encode()
    ).decode()

    assert SharePayload.from_base64(encoded).title == "Track"


def test_track_payload_roundtrips_optional_duration() -> None:
    """Individual-track payloads preserve their optional duration."""
    payload = SharePayload(type="track", title="Track", duration=218)

    assert SharePayload.from_base64(payload.to_base64()).duration == 218


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_track_duration(duration: object) -> None:
    """Individual-track duration must be a nonnegative integer."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track", "duration": duration}).encode()
    ).decode()

    with pytest.raises(ValueError, match="Invalid share track duration"):
        SharePayload.from_base64(encoded)


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_playlist_track_duration(duration: object) -> None:
    """Playlist durations are nonnegative integer seconds when present."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {
                "type": "playlist",
                "title": "Mix",
                "tracks": [{"title": "Track", "duration": duration}],
            }
        ).encode()
    ).decode()

    with pytest.raises(ValueError, match="Invalid share playlist track duration"):
        SharePayload.from_base64(encoded)


def test_payload_drops_invalid_optional_links_and_artwork() -> None:
    """A malformed optional ref does not invalidate otherwise usable share data."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {
                "type": "playlist",
                "title": "Playlist",
                "art_ref": "x:evil.example:/art.jpg",
                "provider_refs": {
                    "spotify": "/playlist/123",
                    "unknown": "/playlist/456",
                },
                "tracks": [
                    {
                        "title": "Track",
                        "provider_refs": {
                            "tidal": "/track/123",
                            "unknown": "/track/456",
                        },
                    }
                ],
            }
        ).encode()
    ).decode()

    payload = SharePayload.from_base64(encoded)

    assert payload.art_ref is None
    assert payload.provider_refs == {"spotify": "/playlist/123"}
    assert payload.tracks is not None
    assert payload.tracks[0].provider_refs == {"tidal": "/track/123"}
