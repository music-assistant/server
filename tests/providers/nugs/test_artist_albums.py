"""Test the albums listed for a Nugs.net artist."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.nugs import NugsProvider


async def test_get_artist_albums_stops_paging_at_limit(provider: NugsProvider) -> None:
    """A large discography stops paging once the album limit is reached."""

    async def _get_data(_nugs_api: str, _endpoint: str, **kwargs: Any) -> dict[str, Any]:
        offset, limit = kwargs["offset"], kwargs["limit"]
        items = [{"id": str(idx)} for idx in range(offset, min(offset + limit, 1000))]
        return {"total": 1000, "items": items}

    provider._get_data = AsyncMock(side_effect=_get_data)  # type: ignore[method-assign]
    provider._parse_album = Mock(side_effect=lambda item: item["id"])  # type: ignore[method-assign]
    get_artist_albums = cast("Any", NugsProvider.get_artist_albums).__wrapped__

    albums = await get_artist_albums(provider, "1125")

    assert albums == [str(idx) for idx in range(100)]
    assert provider._get_data.await_count == 1
