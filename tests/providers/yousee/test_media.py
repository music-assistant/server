"""Test YouSee Musik media lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.yousee import media
from music_assistant.providers.yousee.api_client import YouSeeAPIClient
from music_assistant.providers.yousee.media import YouSeeMediaManager

if TYPE_CHECKING:
    import pytest

    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


async def test_get_artist_albums_stops_paging_at_limit(
    provider: YouSeeMusikProvider, monkeypatch: pytest.MonkeyPatch
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

    async def _parse_album(_provider: Any, album: dict[str, Any]) -> str:
        return str(album["id"])

    monkeypatch.setattr(media, "parse_album", _parse_album)
    provider.auth = Mock()
    api = YouSeeAPIClient(provider)
    api.post_graphql = AsyncMock(side_effect=_post_graphql)  # type: ignore[method-assign]
    provider.api = api

    albums = await YouSeeMediaManager(provider).get_artist_albums("artist1")

    assert albums == [str(idx) for idx in range(100)]
    assert api.post_graphql.await_count == 2
