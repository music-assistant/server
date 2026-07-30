"""Test MusicMe web search fallback and playerInit extraction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.musicme.provider import MusicMeProvider

PLAYER_INIT_HTML = """\
<html><head><script>
var viewmode = 'web';
window.playerInit = {"registeredUser":1,"adSupported":0,"catalogSize":"13253842",\
"contentOnLoad":{"id":"94585","type":"artist","mode":"radio","autoPlay":0},\
"userId":"abc123"};
</script></head></html>
"""

PLAYER_INIT_ALBUM_HTML = """\
<html><head><script>
window.playerInit = {"registeredUser":1,"contentOnLoad":{"id":"5034644330297",\
"type":"album","mode":"stream","autoPlay":0},"userId":"abc123"};
</script></head></html>
"""

NO_PLAYER_INIT_HTML = "<html><body>No init here</body></html>"


class TestWebSearchFallback:
    """Tests for _web_search_fallback."""

    @pytest.mark.asyncio
    async def test_extracts_artist_from_player_init(self, provider: MusicMeProvider) -> None:
        """Test that an artist page redirects to the correct artist lookup."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=PLAYER_INIT_HTML.encode("latin-1"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        provider.http_session = MagicMock()
        provider.http_session.get = MagicMock(return_value=mock_resp)

        provider._api_get = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "item": {"id": 94585, "name": "Zaz", "uri": "/Zaz/"},
                "results": {
                    "albums": [
                        {"barcode": "001", "name": "Album1", "streamable": 2},
                    ]
                },
            }
        )

        result = await provider._web_search_fallback(
            "Zaz", [MediaType.ARTIST, MediaType.ALBUM], limit=10
        )

        assert result is not None
        assert len(result.artists) == 1
        assert result.artists[0].name == "Zaz"
        assert len(result.albums) == 1

    @pytest.mark.asyncio
    async def test_extracts_album_from_player_init(self, provider: MusicMeProvider) -> None:
        """Test that an album page redirects to the correct album lookup."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=PLAYER_INIT_ALBUM_HTML.encode("latin-1"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        provider.http_session = MagicMock()
        provider.http_session.get = MagicMock(return_value=mock_resp)

        provider._api_get = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "item": {"barcode": "5034644330297", "name": "Veridis Quo", "streamable": 2},
                "results": {
                    "tracks": [
                        {"barcode": "5034644330297-01_01", "title": "Track1", "streamable": 2},
                    ]
                },
            }
        )

        result = await provider._web_search_fallback(
            "Veridis Quo", [MediaType.ALBUM, MediaType.TRACK], limit=10
        )

        assert result is not None
        assert len(result.albums) == 1
        assert len(result.tracks) == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_no_player_init(self, provider: MusicMeProvider) -> None:
        """Test that missing playerInit returns None."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=NO_PLAYER_INIT_HTML.encode("latin-1"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        provider.http_session = MagicMock()
        provider.http_session.get = MagicMock(return_value=mock_resp)

        result = await provider._web_search_fallback("nope", [MediaType.ARTIST], limit=10)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self, provider: MusicMeProvider) -> None:
        """Test that non-200 status returns None."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        provider.http_session = MagicMock()
        provider.http_session.get = MagicMock(return_value=mock_resp)

        result = await provider._web_search_fallback("test", [MediaType.ARTIST], limit=10)

        assert result is None
