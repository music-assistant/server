"""Bandcamp catalog identities remain separate from their public websites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bandcamp_async_api.models import BCArtist, FollowingItem, SearchResultAlbum, SearchResultArtist
from music_assistant_models.enums import MediaType

from music_assistant.helpers.uri import parse_uri
from music_assistant.providers.bandcamp.converters import BandcampConverters

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist

INSTANCE = "bandcamp--account"
WEBSITE = "https://test.bandcamp.com"


@pytest.fixture(params=["api", "search", "following"])
def artist(request: pytest.FixtureRequest) -> Artist:
    """Convert each real Bandcamp artist response model."""
    converters = BandcampConverters("bandcamp", INSTANCE)
    if request.param == "api":
        return converters.artist_from_api(BCArtist(id=123, name="Test Artist", url=WEBSITE))
    if request.param == "search":
        return converters.artist_from_search(
            SearchResultArtist(id=123, name="Test Artist", url=WEBSITE)
        )
    return converters.artist_from_following(
        FollowingItem(band_id=123, name="Test Artist", url=WEBSITE)
    )


async def test_search_album_artist_uses_music_assistant_uri() -> None:
    """Nested artist identities resolve independently of their public website."""
    album = BandcampConverters("bandcamp", INSTANCE).album_from_search(
        SearchResultAlbum(
            id=456,
            name="Test Album",
            artist_id=123,
            artist_name="Test Artist",
            artist_url=WEBSITE,
            url=f"{WEBSITE}/album/test-album",
        ),
        artist_item_id="123",
    )
    assert album.artists[0].uri == f"{INSTANCE}://artist/123"
    assert album.uri == f"{INSTANCE}://album/123-456"
    assert next(iter(album.provider_mappings)).url == f"{WEBSITE}/album/test-album"
    assert await parse_uri(album.uri) == (MediaType.ALBUM, INSTANCE, "123-456")
    assert album.artists[0].uri is not None
    assert await parse_uri(album.artists[0].uri) == (MediaType.ARTIST, INSTANCE, "123")


async def test_converted_artist_uri_resolves_to_catalog(artist: Artist) -> None:
    """Every artist converter preserves the catalog ID and the public mapping URL."""
    assert artist.uri == f"{INSTANCE}://artist/123"
    assert await parse_uri(artist.uri) == (MediaType.ARTIST, INSTANCE, "123")
    mapping = next(iter(artist.provider_mappings))
    assert (mapping.item_id, mapping.provider_instance, mapping.url) == ("123", INSTANCE, WEBSITE)
