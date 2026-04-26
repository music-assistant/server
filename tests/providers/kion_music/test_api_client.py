"""Unit tests for the KION Music API client."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from yandex_music.exceptions import NetworkError

from music_assistant.providers.kion_music.api_client import KionMusicClient
from music_assistant.providers.kion_music.constants import DEFAULT_BASE_URL


@pytest.fixture
def client() -> KionMusicClient:
    """Return a KionMusicClient with a fake token."""
    return KionMusicClient("fake_token")


async def test_connect_sets_base_url(client: KionMusicClient) -> None:
    """Verify connect() passes DEFAULT_BASE_URL to ClientAsync."""
    with mock.patch("music_assistant.providers.kion_music.api_client.ClientAsync") as mock_cls:
        mock_instance = mock.AsyncMock()
        mock_instance.me = type("Me", (), {"account": type("Account", (), {"uid": 42})()})()
        mock_instance.init = mock.AsyncMock(return_value=mock_instance)
        mock_cls.return_value = mock_instance

        result = await client.connect()

        assert result is True
        mock_cls.assert_called_once_with("fake_token", base_url=DEFAULT_BASE_URL)


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


async def test_get_tracks_retry_on_network_error_then_success(
    client: KionMusicClient,
) -> None:
    """Test that get_tracks retries once on NetworkError and succeeds."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    track = type("Track", (), {"id": 1})()
    mock_client.tracks = mock.AsyncMock(side_effect=[NetworkError("timeout"), [track]])

    result = await client.get_tracks(["1"])

    assert len(result) == 1
    assert mock_client.tracks.call_count == 2


async def test_get_tracks_retry_on_network_error_both_fail(
    client: KionMusicClient,
) -> None:
    """Test that get_tracks raises ResourceTemporarilyUnavailable when retry fails."""
    mock_client = mock.AsyncMock()
    client._client = mock_client
    client._user_id = 1

    mock_client.tracks = mock.AsyncMock(side_effect=NetworkError("timeout"))

    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_tracks(["1"])

    assert mock_client.tracks.call_count == 2
