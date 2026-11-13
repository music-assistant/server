"""Test the albums listed for a Deezer artist."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from music_assistant.providers.deezer import media
from music_assistant.providers.deezer.media import DeezerMediaManager

if TYPE_CHECKING:
    import pytest

    from music_assistant.providers.deezer.provider import DeezerProvider


def _page(start: int, total: int) -> SimpleNamespace:
    """Return one page of 25 albums of an artist, as the GraphQL client parses it."""
    edges = [SimpleNamespace(node=SimpleNamespace(id=str(idx))) for idx in range(start, start + 25)]
    has_next_page = start + 25 < total
    page_info = SimpleNamespace(has_next_page=has_next_page, end_cursor=str(start + 25))
    return SimpleNamespace(albums=SimpleNamespace(edges=edges, page_info=page_info))


async def test_get_artist_albums_stops_paging_at_limit(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large discography stops paging once the album limit is reached."""
    monkeypatch.setattr(media, "parse_album", lambda _provider, node: node.id)
    provider.gql_client.get_artist = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_page(start, 1000) for start in range(0, 1000, 25)]
    )
    get_artist_albums = cast("Any", DeezerMediaManager.get_artist_albums).__wrapped__

    albums = await get_artist_albums(DeezerMediaManager(provider), "123")

    assert albums == [str(idx) for idx in range(100)]
    assert provider.gql_client.get_artist.await_count == 4
