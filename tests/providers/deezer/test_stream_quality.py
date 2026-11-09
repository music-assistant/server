"""Test the formats a Deezer account asks for."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.deezer.gw_client import GWClient


async def _formats(user_data: dict[str, Any]) -> list[str]:
    """Return the formats a client asks for after setup with the given user data."""
    client = GWClient(Mock(), "arl")
    with patch.object(GWClient, "_gw_api_call", AsyncMock(return_value=user_data)):
        await client.setup()
    return [fmt["format"] for fmt in client.formats]


async def test_lossless_account_asks_for_hi_res_first(gw_user_data: dict[str, Any]) -> None:
    """A lossless account asks for the 24-bit file first, Deezer falls back per track."""
    assert await _formats(gw_user_data) == ["FLAC_24", "FLAC", "MP3_320", "MP3_128"]
