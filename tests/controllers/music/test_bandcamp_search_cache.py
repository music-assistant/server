"""Repair legacy Bandcamp identities when reading core search caches."""

from typing import Any, cast
from unittest.mock import Mock

import pytest
from bandcamp_async_api.models import BCArtist, SearchResultAlbum
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import Album, Artist, SearchResults

from music_assistant.controllers.music.constants import CACHE_CATEGORY_SEARCH_RESULTS
from music_assistant.providers.bandcamp.converters import BandcampConverters
from tests.common import set_music_source_access
from tests.controllers.music.test_search import _make_controller, _make_search_provider

INSTANCE = "bandcamp--account"
WEBSITE = "https://test.bandcamp.com"


@pytest.mark.parametrize("combined", [False, True])
async def test_legacy_core_search_cache(mass_minimal: Any, combined: bool) -> None:
    """Existing serialized core caches normalize only Bandcamp catalog identities."""
    await mass_minimal.cache._setup_database()
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
    album.uri = f"{WEBSITE.replace('https://', 'http://')}/album/test-album"
    album.artists[0].uri = WEBSITE.replace("https://", "http://")
    artist = BandcampConverters("bandcamp", INSTANCE).artist_from_api(
        BCArtist(id=123, name="Test Artist", url=WEBSITE)
    )
    artist.uri = WEBSITE.replace("https://", "http://")
    other = Artist(item_id="other", provider="other", name="Other", provider_mappings=set())
    other.uri = "https://other.example/artist"
    result = SearchResults(artists=[artist, other], albums=[album])
    provider = _make_search_provider(INSTANCE, "bandcamp")
    provider.type = ProviderType.MUSIC
    controller = _make_controller([provider])
    set_music_source_access(controller.mass, {INSTANCE: None})
    controller.mass.cache = mass_minimal.cache
    cast("Mock", controller.mass).providers = [provider]
    key = f"Test-album-artist-25-0-{INSTANCE}-" if combined else "Test-album-artist-25"
    await controller.mass.cache.set(
        key=key,
        data=result.to_dict(),
        provider="music" if combined else INSTANCE,
        category=CACHE_CATEGORY_SEARCH_RESULTS,
    )
    if combined:
        found = await controller.search(
            "Test", [MediaType.ALBUM, MediaType.ARTIST], providers=[INSTANCE]
        )
    else:
        found = await controller.search_provider(
            "Test", INSTANCE, [MediaType.ALBUM, MediaType.ARTIST], limit=25
        )
    assert isinstance(found.artists[0], Artist)
    assert isinstance(found.albums[0], Album)
    assert found.artists[0].uri == f"{INSTANCE}://artist/123"
    assert found.albums[0].uri == f"{INSTANCE}://album/123-456"
    assert found.albums[0].artists[0].uri == f"{INSTANCE}://artist/123"
    assert found.artists[1].uri == other.uri
    assert next(iter(found.artists[0].provider_mappings)).url == WEBSITE
    assert next(iter(found.albums[0].provider_mappings)).url == f"{WEBSITE}/album/test-album"
    provider.search.assert_not_awaited()
