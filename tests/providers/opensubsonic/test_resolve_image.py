"""Test image resolving for the Open Subsonic provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


@pytest.mark.parametrize(
    "url",
    [
        "https://navidrome.example.com/share/img/abc?size=300",
        "http://lastfm.example.com/i/u/300x300/abc.png",
    ],
)
async def test_resolve_image_returns_remote_urls(url: str) -> None:
    """Remote image URLs are returned as-is instead of being passed to getCoverArt."""
    prov = OpenSonicProvider.__new__(OpenSonicProvider)
    prov.logger = MagicMock()
    prov.conn = MagicMock()
    prov.conn.get_cover_art = AsyncMock()

    assert await prov.resolve_image(url) == url
    prov.conn.get_cover_art.assert_not_called()
