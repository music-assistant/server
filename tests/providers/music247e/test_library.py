"""Test 24-7 (247e) library operations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.music247e.library import Music247eLibraryManager
from music_assistant.providers.music247e.parsers import parse_playlist

if TYPE_CHECKING:
    from music_assistant.providers.music247e.provider import Music247eProvider


@pytest.mark.parametrize("owned", [True, False], ids=["owned", "followed"])
@pytest.mark.parametrize("success", [True, False])
async def test_remove_playlist(
    provider: Music247eProvider,
    monkeypatch: pytest.MonkeyPatch,
    owned: bool,
    success: bool,
) -> None:
    """Owned playlists are deleted, followed playlists are removed from favorites."""
    playlist = parse_playlist(
        provider,
        {"id": "playlist1", "title": "Playlist One", "isOwned": owned, "share": None},
    )
    get_playlist = AsyncMock(return_value=playlist)
    monkeypatch.setattr(provider, "get_playlist", get_playlist)
    section = "playlists" if owned else "favorites"
    operation = "delete" if owned else "removePlaylist"
    api = Mock()
    api.post_graphql = AsyncMock(return_value={"data": {section: {operation: {"ok": success}}}})
    provider.api = api
    provider.auth = Mock()
    manager = Music247eLibraryManager(provider)

    result = await manager.remove_item("playlist1", MediaType.PLAYLIST)

    assert result is success
    get_playlist.assert_awaited_once_with("playlist1")
    api.post_graphql.assert_awaited_once()
    query, variables = api.post_graphql.call_args.args
    assert variables == {"id": "playlist1"}
    query = " ".join(query.split())
    if owned:
        assert "playlists { delete(id: $id) { ok } }" in query
        assert "favorites" not in query
    else:
        assert "favorites { removePlaylist (id: $id) { ok } }" in query
        assert "delete(" not in query
