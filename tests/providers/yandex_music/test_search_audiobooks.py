"""Tests for audiobook search routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.yandex_music.provider import YandexMusicProvider

from .conftest import use_real_create_task


def _fake_album(*, album_id: int, title: str, meta_type: str | None, type_: str | None) -> Mock:
    """Minimal Yandex Album stand-in sufficient for classify_album + parse_audiobook."""
    album = Mock()
    album.id = album_id
    album.title = title
    album.version = None
    album.available = True
    album.meta_type = meta_type
    album.type = type_
    album.artists = []
    album.labels = []
    album.description = None
    album.short_description = None
    album.content_warning = None
    album.genre = None
    album.release_date = None
    album.cover_uri = None
    album.og_image = None
    album.listening_finished = None
    album.track_count = None
    return album


def _fake_search_result(albums: list[Mock]) -> Mock:
    """Build a search-result stub with an `albums.results` list and empty siblings."""
    result = Mock()
    result.tracks = None
    result.artists = None
    result.playlists = None
    result.podcasts = None
    result.albums = Mock()
    result.albums.results = albums
    return result


@pytest.fixture
def provider_mock() -> Mock:
    """Return a provider mock with a stubbed api_client.search."""
    provider = Mock(spec=YandexMusicProvider)
    provider.domain = "yandex_music"
    provider.instance_id = "yandex_music_instance"
    provider.logger = Mock()
    provider.client = AsyncMock()
    # @use_cache decorator reads self.mass.cache — stub returning None (cache miss)
    provider.mass = Mock()
    provider.mass.cache = AsyncMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    use_real_create_task(provider.mass)
    return provider


@pytest.mark.asyncio
async def test_search_audiobook_only_filters_albums(provider_mock: Mock) -> None:
    """Requesting AUDIOBOOK only routes audiobook albums and drops music ones."""
    music = _fake_album(album_id=1, title="Plain Music", meta_type="music", type_="music")
    book = _fake_album(album_id=2, title="Cool Book", meta_type="podcast", type_="audiobook")
    provider_mock.client.search = AsyncMock(return_value=_fake_search_result([music, book]))

    result = await YandexMusicProvider.search(
        provider_mock, "query", [MediaType.AUDIOBOOK], limit=5
    )

    # Single Yandex API call with type_="album"
    provider_mock.client.search.assert_awaited_once()
    assert provider_mock.client.search.await_args.kwargs["search_type"] == "album"

    assert [a.item_id for a in result.audiobooks] == ["2"]
    assert list(result.albums) == []


@pytest.mark.asyncio
async def test_search_album_and_audiobook_split(provider_mock: Mock) -> None:
    """Requesting both ALBUM and AUDIOBOOK splits the albums bucket cleanly."""
    music = _fake_album(album_id=10, title="Music", meta_type="music", type_="music")
    book = _fake_album(album_id=20, title="Book", meta_type="podcast", type_="audiobook")
    podcast = _fake_album(album_id=30, title="Podcast", meta_type="podcast", type_="podcast")
    provider_mock.client.search = AsyncMock(
        return_value=_fake_search_result([music, book, podcast])
    )

    result = await YandexMusicProvider.search(
        provider_mock, "q", [MediaType.ALBUM, MediaType.AUDIOBOOK], limit=5
    )

    assert [a.item_id for a in result.albums] == ["10"]
    assert [a.item_id for a in result.audiobooks] == ["20"]


@pytest.mark.asyncio
async def test_search_audiobook_not_dropped_by_limit_when_music_dominates(
    provider_mock: Mock,
) -> None:
    """
    Limit applied per bucket after classification, not before.

    Audiobooks tail-listed by Yandex must still appear when top ``limit``
    results are music albums.
    """
    music_albums = [
        _fake_album(album_id=i, title=f"Music {i}", meta_type="music", type_="music")
        for i in range(5)
    ]
    tail_audiobook = _fake_album(
        album_id=99, title="Tail Book", meta_type="podcast", type_="audiobook"
    )
    provider_mock.client.search = AsyncMock(
        return_value=_fake_search_result([*music_albums, tail_audiobook])
    )

    result = await YandexMusicProvider.search(provider_mock, "q", [MediaType.AUDIOBOOK], limit=3)

    # Even with only 3 results requested and 5 music albums ahead of it,
    # the audiobook tail entry still lands in the audiobooks bucket.
    assert [a.item_id for a in result.audiobooks] == ["99"]


@pytest.mark.asyncio
async def test_search_album_bucket_respects_limit_independently(
    provider_mock: Mock,
) -> None:
    """Albums bucket is capped at ``limit`` regardless of audiobook count."""
    albums = [
        _fake_album(album_id=i, title=f"M{i}", meta_type="music", type_="music") for i in range(10)
    ]
    provider_mock.client.search = AsyncMock(return_value=_fake_search_result(albums))

    result = await YandexMusicProvider.search(
        provider_mock, "q", [MediaType.ALBUM, MediaType.AUDIOBOOK], limit=3
    )

    assert len(result.albums) == 3
    assert list(result.audiobooks) == []


@pytest.mark.asyncio
async def test_search_albums_type_mapping_dedupe(provider_mock: Mock) -> None:
    """ALBUM + AUDIOBOOK both map to Yandex 'album' — dedup keeps a single call type."""
    provider_mock.client.search = AsyncMock(return_value=_fake_search_result([]))

    await YandexMusicProvider.search(
        provider_mock, "q", [MediaType.ALBUM, MediaType.AUDIOBOOK], limit=3
    )

    provider_mock.client.search.assert_awaited_once()
    # both map to "album"; with dedup there's a single requested_type → search_type='album'
    assert provider_mock.client.search.await_args.kwargs["search_type"] == "album"
