"""Test the albums listed for a NetEase Cloud Music artist."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider


async def test_get_artist_albums_stops_paging_at_limit(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """A large discography stops paging once the album limit is reached."""

    async def _get(_path: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        offset, limit = params["offset"], params["limit"]
        albums = [{"id": idx} for idx in range(offset, min(offset + limit, 1000))]
        return {"hotAlbums": albums, "more": offset + limit < 1000}

    provider._client = Mock(get=AsyncMock(side_effect=_get))
    provider._parse_album = Mock(  # type: ignore[method-assign]
        side_effect=lambda album: SimpleNamespace(item_id=str(album["id"]))
    )
    get_artist_albums = cast("Any", NeteaseCloudMusicProvider.get_artist_albums).__wrapped__

    albums = await get_artist_albums(provider, "6452")

    assert [album.item_id for album in albums] == [str(idx) for idx in range(100)]
    assert provider._client.get.await_count == 1
