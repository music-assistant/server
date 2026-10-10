"""Library changes preserve the real API's success and failure results."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from ya_passport_auth import SecretStr
from yandex_music import ClientAsync

from music_assistant.providers.yandex_music.api_client import YandexMusicClient


@pytest.mark.parametrize(
    "method",
    ["like_track", "unlike_track", "like_album", "unlike_album", "like_artist", "unlike_artist"],
)
async def test_library_change_does_not_report_rejected_response_as_success(method: str) -> None:
    """A valid HTTP response without API acknowledgement must remain a failure."""
    underlying = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = underlying
    with patch.object(underlying.request, "post", AsyncMock(return_value="not-ok")):
        assert await getattr(client, method)("42") is False


@pytest.mark.parametrize(
    "method",
    ["like_track", "unlike_track", "like_album", "unlike_album", "like_artist", "unlike_artist"],
)
async def test_library_change_preserves_acknowledged_success(method: str) -> None:
    """Track revisions and the album/artist ok response acknowledge a change."""
    underlying = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = underlying
    response = {"revision": 1} if method.endswith("track") else "ok"
    with patch.object(underlying.request, "post", AsyncMock(return_value=response)):
        assert await getattr(client, method)("42") is True
