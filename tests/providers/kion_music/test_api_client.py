"""Unit tests for the KION Music API client."""

from __future__ import annotations

from unittest import mock

import pytest
from yandex_music.exceptions import NetworkError

from music_assistant.providers.kion_music.api_client import KionMusicClient


@pytest.fixture
def client() -> KionMusicClient:
    """Return a KionMusicClient with a fake token."""
    return KionMusicClient("fake_token")


async def test_get_liked_albums_batching(client: KionMusicClient) -> None:
    """Test that liked albums are fetched in batches of 50."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    # Create 60 likes so we get 2 batches
    likes = []
    for i in range(60):
        like = type("Like", (), {"album": type("Album", (), {"id": i + 1})()})()
        likes.append(like)

    mock_client.users_likes_albums = mock.AsyncMock(return_value=likes)

    batch1 = [type("Album", (), {"id": i + 1})() for i in range(50)]
    batch2 = [type("Album", (), {"id": i + 51})() for i in range(10)]
    mock_client.albums = mock.AsyncMock(side_effect=[batch1, batch2])

    result = await client.get_liked_albums()

    assert len(result) == 60
    assert mock_client.albums.call_count == 2


async def test_get_liked_albums_batch_fallback_on_network_error(
    client: KionMusicClient,
) -> None:
    """Test fallback to minimal data when batch fetch fails."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    album_obj = type("Album", (), {"id": 1})()
    likes = [type("Like", (), {"album": album_obj})()]

    mock_client.users_likes_albums = mock.AsyncMock(return_value=likes)
    mock_client.albums = mock.AsyncMock(side_effect=NetworkError("timeout"))

    result = await client.get_liked_albums()

    assert len(result) == 1
    assert result[0].id == 1
