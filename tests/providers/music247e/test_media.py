"""Test 24-7 (247e) media lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.music247e import media
from music_assistant.providers.music247e.api_client import Music247eAPIClient
from music_assistant.providers.music247e.media import Music247eMediaManager

if TYPE_CHECKING:
    import pytest

    from music_assistant.providers.music247e.provider import Music247eProvider


async def test_get_artist_albums_stops_paging_at_limit(
    provider: Music247eProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large discography stops paging once the album limit is reached."""

    async def _post_graphql(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        start = int(variables["after"] or 0)
        end = start + variables["first"]
        albums = {
            "items": [{"id": str(idx)} for idx in range(start, end)],
            "pageInfo": {"hasNextPage": end < 1000, "endCursor": str(end)},
        }
        return {"data": {"catalog": {"artist": {"albums": albums}}}}

    monkeypatch.setattr(media, "parse_album", lambda _provider, album: album["id"])
    provider.auth = Mock()
    api = Music247eAPIClient(provider)
    api.post_graphql = AsyncMock(side_effect=_post_graphql)  # type: ignore[method-assign]
    provider.api = api

    albums = await Music247eMediaManager(provider).get_artist_albums("artist1")

    assert albums == [str(idx) for idx in range(100)]
    assert api.post_graphql.await_count == 2
