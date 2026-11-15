"""Tests for the requests Spotify listings make that are cached as a whole."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call

from music_assistant.providers.spotify import provider as spotify_provider
from music_assistant.providers.spotify.provider import SpotifyProvider
from tests.common import use_real_create_task

if TYPE_CHECKING:
    import pytest


def _make_provider(total: int) -> tuple[SpotifyProvider, AsyncMock]:
    """Return a Spotify provider with an empty cache whose API lists the given number of items."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(provider.mass)

    async def _get_page(_endpoint: str, limit: int, offset: int, **_kwargs: Any) -> dict[str, Any]:
        items = [{"id": f"i{i}"} for i in range(offset, min(offset + limit, total))]
        return {"etag": "etag", "total": total, "items": items}

    get_data = AsyncMock(side_effect=_get_page)
    provider._get_data = get_data  # type: ignore[method-assign]
    return provider, get_data


async def test_album_tracks_take_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """An album of up to 50 tracks is listed with a single request."""
    monkeypatch.setattr(spotify_provider, "parse_track", lambda item, _prov: item["id"])
    provider, get_data = _make_provider(total=30)

    tracks = await provider.get_album_tracks("album1")

    assert tracks == [f"i{i}" for i in range(30)]
    assert get_data.await_args_list == [call("albums/album1/tracks", limit=50, offset=0)]


async def test_audiobook_chapters_take_one_request() -> None:
    """An audiobook of up to 50 chapters is listed with a single request."""
    provider, get_data = _make_provider(total=30)

    chapters = await provider._get_audiobook_chapters_data("book1")

    assert [chapter["id"] for chapter in chapters] == [f"i{i}" for i in range(30)]
    assert get_data.await_args_list == [
        call("audiobooks/book1/chapters", limit=50, offset=0, market="from_token")
    ]
