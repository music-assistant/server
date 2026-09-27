"""Tests for the share URL parsing and provider URL helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidProviderID, InvalidProviderURI

from music_assistant.helpers.uri import (
    apple_storefront_from_url,
    canonical_provider_url,
    discogs_id_from_url,
    parse_uri,
)

SPOTIFY_ID = "4Z8W4fKeB5YxbusRsdQVPb"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # spotify
        (
            f"https://open.spotify.com/artist/{SPOTIFY_ID}",
            (MediaType.ARTIST, "spotify", SPOTIFY_ID),
        ),
        (
            f"https://open.spotify.com/track/{SPOTIFY_ID}?si=04a63c8234ac413e",
            (MediaType.TRACK, "spotify", SPOTIFY_ID),
        ),
        (
            f"https://open.spotify.com/intl-de/album/{SPOTIFY_ID}",
            (MediaType.ALBUM, "spotify", SPOTIFY_ID),
        ),
        (
            "https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e",
            (MediaType.PLAYLIST, "spotify", "5lH9NjOeJvctAO92ZrKQNB"),
        ),
        # tidal
        ("https://tidal.com/artist/64518", (MediaType.ARTIST, "tidal", "64518")),
        ("https://tidal.com/browse/track/123456", (MediaType.TRACK, "tidal", "123456")),
        ("https://listen.tidal.com/album/7890", (MediaType.ALBUM, "tidal", "7890")),
        ("https://tidal.com/track/123456/", (MediaType.TRACK, "tidal", "123456")),
        # deezer
        ("https://www.deezer.com/artist/399", (MediaType.ARTIST, "deezer", "399")),
        ("https://www.deezer.com/en/track/123456", (MediaType.TRACK, "deezer", "123456")),
        ("https://deezer.com/album/789", (MediaType.ALBUM, "deezer", "789")),
        # apple music
        (
            "https://music.apple.com/de/artist/dead-sara/123456789",
            (MediaType.ARTIST, "apple_music", "123456789"),
        ),
        ("https://music.apple.com/gb/artist/657515", (MediaType.ARTIST, "apple_music", "657515")),
        (
            "https://music.apple.com/de/album/some-album/1234567890?i=987654321",
            (MediaType.TRACK, "apple_music", "987654321"),
        ),
        (
            "https://itunes.apple.com/us/album/in-rainbows/id1109714933",
            (MediaType.ALBUM, "apple_music", "1109714933"),
        ),
        (
            "https://geo.itunes.apple.com/us/artist/radiohead/id657515",
            (MediaType.ARTIST, "apple_music", "657515"),
        ),
        # qobuz
        ("https://open.qobuz.com/album/0634904032432", (MediaType.ALBUM, "qobuz", "0634904032432")),
        (
            "https://www.qobuz.com/us-en/album/in-rainbows-radiohead/0634904032432",
            (MediaType.ALBUM, "qobuz", "0634904032432"),
        ),
        (
            "https://www.qobuz.com/us-en/interpreter/radiohead/43840",
            (MediaType.ARTIST, "qobuz", "43840"),
        ),
        # youtube music
        (
            "https://music.youtube.com/channel/UCr_iyUANcn9OX_yy9piYoLw",
            (MediaType.ARTIST, "ytmusic", "UCr_iyUANcn9OX_yy9piYoLw"),
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            (MediaType.TRACK, "ytmusic", "dQw4w9WgXcQ"),
        ),
        # anything else is a plain stream URL for the builtin provider
        (
            "https://www.discogs.com/artist/3840",
            (MediaType.UNKNOWN, "builtin", "https://www.discogs.com/artist/3840"),
        ),
        (
            "https://radiohead.bandcamp.com/",
            (MediaType.UNKNOWN, "builtin", "https://radiohead.bandcamp.com/"),
        ),
        (
            f"https://open.spotify.example/track/{SPOTIFY_ID}",
            (MediaType.UNKNOWN, "builtin", f"https://open.spotify.example/track/{SPOTIFY_ID}"),
        ),
    ],
)
async def test_parse_uri_share_urls(url: str, expected: tuple[MediaType, str, str]) -> None:
    """Parse every share URL form a streaming service, or MusicBrainz, hands out."""
    assert await parse_uri(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com",
        "https://open.spotify.com/track/",
        "https://tidal.com/browse/track/",
        "https://www.qobuz.com/us-en/label/xl-recordings/1234",
        "https://music.youtube.com/playlist?list=OLAK5uy_abc",
        "https://music.apple.com/gb",
        "https://www.deezer.com/artist/not-a-number",
    ],
)
async def test_parse_uri_rejects_truncated_or_unsupported_share_urls(url: str) -> None:
    """A known host with a path that names no item is invalid, not a builtin stream."""
    with pytest.raises(InvalidProviderURI):
        await parse_uri(url)


@pytest.mark.parametrize(
    ("domain", "media_type", "storefront", "expected"),
    [
        ("spotify", MediaType.ARTIST, None, f"https://open.spotify.com/artist/{SPOTIFY_ID}"),
        ("spotify", MediaType.TRACK, None, f"https://open.spotify.com/track/{SPOTIFY_ID}"),
        ("tidal", MediaType.ALBUM, None, f"https://tidal.com/album/{SPOTIFY_ID}"),
        ("deezer", MediaType.TRACK, None, f"https://www.deezer.com/track/{SPOTIFY_ID}"),
        ("apple_music", MediaType.ALBUM, "gb", f"https://music.apple.com/gb/album/{SPOTIFY_ID}"),
        ("apple_music", MediaType.ALBUM, None, None),
        ("apple_music", MediaType.TRACK, "gb", None),
        ("ytmusic", MediaType.ARTIST, None, f"https://music.youtube.com/channel/{SPOTIFY_ID}"),
        ("ytmusic", MediaType.ALBUM, None, None),
        ("qobuz", MediaType.ALBUM, None, None),
        ("spotify", MediaType.PLAYLIST, None, None),
    ],
)
def test_canonical_provider_url(
    domain: str, media_type: MediaType, storefront: str | None, expected: str | None
) -> None:
    """Build the URL MusicBrainz links an item with, only where the provider has one."""
    assert canonical_provider_url(domain, media_type, SPOTIFY_ID, storefront) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/artist/{SPOTIFY_ID[:10]}",
        "https://tidal.com/artist/not-a-number",
        "https://music.apple.com/us/artist/radiohead",
    ],
)
async def test_parse_uri_rejects_a_malformed_id_when_validating(url: str) -> None:
    """A share URL whose id the provider cannot have is rejected as an invalid id."""
    with pytest.raises(InvalidProviderID):
        await parse_uri(url, validate_id=True)


async def test_parse_uri_validation_leaves_non_numeric_playlist_ids_alone() -> None:
    """Tidal and Apple Music playlists carry non-numeric ids, only their catalog items are numeric."""
    tidal_playlist = "7ab5d2b6-93fb-4181-a008-a1d18e2cebfa"
    apple_playlist = "pl.f4d106fed2bd41149aaacabb233eb5eb"
    assert await parse_uri(f"https://tidal.com/playlist/{tidal_playlist}", validate_id=True) == (
        MediaType.PLAYLIST,
        "tidal",
        tidal_playlist,
    )
    assert await parse_uri(
        f"https://music.apple.com/us/playlist/todays-hits/{apple_playlist}", validate_id=True
    ) == (MediaType.PLAYLIST, "apple_music", apple_playlist)


async def test_canonical_provider_url_round_trips_through_parse_uri() -> None:
    """Every canonical URL parses back to the item it was built from."""
    for domain, media_type in (
        ("spotify", MediaType.ALBUM),
        ("tidal", MediaType.TRACK),
        ("deezer", MediaType.ARTIST),
        ("ytmusic", MediaType.ARTIST),
    ):
        url = canonical_provider_url(domain, media_type, "1234")
        assert url is not None
        assert await parse_uri(url) == (media_type, domain, "1234")


def test_apple_storefront_from_url() -> None:
    """Read the storefront off an Apple Music URL, and nothing off any other URL."""
    assert (
        apple_storefront_from_url("https://music.apple.com/us/album/in-rainbows/1109714933") == "us"
    )
    assert apple_storefront_from_url("https://music.apple.com/gb/artist/657515") == "gb"
    assert apple_storefront_from_url("https://music.apple.com/album/123") is None
    assert apple_storefront_from_url("https://music.apple.com/") is None
    assert apple_storefront_from_url("https://open.spotify.com/artist/x") is None


@pytest.mark.parametrize(
    ("url", "media_type", "expected"),
    [
        ("https://www.discogs.com/artist/3840", MediaType.ARTIST, "3840"),
        ("https://www.discogs.com/artist/3840-Radiohead", MediaType.ARTIST, "3840"),
        ("https://www.discogs.com/release/1119453", MediaType.ALBUM, "1119453"),
        ("https://www.discogs.com/master/21491", MediaType.ALBUM, None),
        ("https://www.discogs.com/artist/3840anything", MediaType.ARTIST, None),
        ("https://www.discogs.com/release/1119453", MediaType.ARTIST, None),
        ("https://www.discogs.com/artist/3840", MediaType.TRACK, None),
        ("https://www.wikidata.org/wiki/Q42", MediaType.ARTIST, None),
    ],
)
def test_discogs_id_from_url(url: str, media_type: MediaType, expected: str | None) -> None:
    """Read the Discogs id off an artist or release URL, never off a master or other page."""
    assert discogs_id_from_url(url, media_type) == expected
