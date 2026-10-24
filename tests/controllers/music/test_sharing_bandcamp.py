"""Share Bandcamp mappings through Music Assistant catalog identities."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from bandcamp_async_api.models import BCArtist, FollowingItem, SearchResultAlbum, SearchResultArtist
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.providers.bandcamp.converters import BandcampConverters
from tests.common import set_music_source_access

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


async def test_share_converted_bandcamp_artist(
    artist: Artist, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalog artist URIs resolve the converted provider mapping's public website."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    controller.mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.create_task(coro)
    set_music_source_access(controller.mass, {INSTANCE: None})
    provider = MagicMock(domain="bandcamp", instance_id=INSTANCE, type=ProviderType.MUSIC)
    provider.get_artist = AsyncMock(return_value=artist)
    controller.mass.get_provider.return_value = provider
    controller.mass.cache.handle_refresh.return_value = nullcontext()
    controller.artists = ArtistsController.__new__(ArtistsController)
    controller.artists.mass = controller.mass
    library_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(controller.artists, "get_library_item_by_prov_id", library_lookup)

    uri = f"{artist.provider}://artist/{artist.item_id}"
    assert await controller.get_item_share_info(uri) == {
        "title": "Test Artist",
        "text": None,
        "links": [{"provider_domain": "bandcamp", "provider_instance": INSTANCE, "url": WEBSITE}],
    }
    mapping = next(iter(artist.provider_mappings))
    assert (mapping.item_id, mapping.provider_instance, mapping.url) == ("123", INSTANCE, WEBSITE)
    library_lookup.assert_awaited_once_with("123", INSTANCE)
    provider.get_artist.assert_awaited_once_with("123")
    controller.mass.metadata.schedule_update_metadata.assert_not_called()

    provider.get_artist.reset_mock()
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info(WEBSITE)
    provider.get_artist.assert_not_awaited()


async def test_share_converted_bandcamp_search_album(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalog album URIs share the public website without resolving nested artists."""
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
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    controller.mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.create_task(coro)
    set_music_source_access(controller.mass, {INSTANCE: None})
    provider = MagicMock(domain="bandcamp", instance_id=INSTANCE, type=ProviderType.MUSIC)
    provider.get_album = AsyncMock(return_value=album)
    provider.get_artist = AsyncMock(side_effect=ProviderUnavailableError("Artist offline"))
    controller.mass.get_provider.return_value = provider
    controller.mass.cache.handle_refresh.return_value = nullcontext()
    controller.mass.music = controller
    controller.artists = ArtistsController.__new__(ArtistsController)
    controller.artists.mass = controller.mass
    monkeypatch.setattr(
        controller.artists, "get_library_item_by_prov_id", AsyncMock(return_value=None)
    )
    controller.albums = AlbumsController.__new__(AlbumsController)
    controller.albums.mass = controller.mass
    monkeypatch.setattr(
        controller.albums, "get_library_item_by_prov_id", AsyncMock(return_value=None)
    )
    uri = f"{album.provider}://album/{album.item_id}"
    assert await controller.get_item_share_info(uri) == {
        "title": "Test Album",
        "text": None,
        "links": [
            {
                "provider_domain": "bandcamp",
                "provider_instance": INSTANCE,
                "url": f"{WEBSITE}/album/test-album",
            }
        ],
    }
    provider.get_album.assert_awaited_once_with("123-456")
    provider.get_artist.assert_not_awaited()
