"""Test that Deezer tells favorite audiobooks from favorite albums on every sync."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from music_assistant.providers.deezer import media
from music_assistant.providers.deezer.media import DeezerMediaManager

if TYPE_CHECKING:
    import pytest

    from music_assistant.providers.deezer.provider import DeezerProvider


async def test_new_favorite_audiobooks_are_seen_without_a_restart(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audiobook favorited after a sync is an audiobook in the next one, not an album."""
    favorites = ["1"]

    def favorite_albums(**_kwargs: Any) -> SimpleNamespace:
        edges = [
            SimpleNamespace(node=SimpleNamespace(id=album_id), favorited_at=None)
            for album_id in favorites
        ]
        page_info = SimpleNamespace(has_next_page=False, end_cursor=None)
        albums = SimpleNamespace(edges=edges, page_info=page_info)
        return SimpleNamespace(user_favorites=SimpleNamespace(albums=albums))

    gql = AsyncMock()
    gql.get_favorite_audiobooks.return_value = None
    gql.get_favorite_albums.side_effect = favorite_albums
    gql.check_audiobook_ids.side_effect = set  # every favorite is an audiobook
    provider.gql_client = gql
    manager = DeezerMediaManager(provider)
    monkeypatch.setattr(manager, "get_audiobook", AsyncMock(side_effect=lambda item_id: item_id))
    monkeypatch.setattr(manager, "_get_personal_songs", AsyncMock(return_value=[]))
    monkeypatch.setattr(media, "parse_album", lambda _provider, node: node.id)

    first = [audiobook async for audiobook in manager.get_library_audiobooks()]
    favorites.append("2")
    albums = [album async for album in manager.get_library_albums()]
    second: list[Any] = [audiobook async for audiobook in manager.get_library_audiobooks()]

    assert first == ["1"]
    assert albums == []
    assert set(second) == {"1", "2"}
