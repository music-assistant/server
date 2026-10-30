"""Tests for the ListenBrainz API client boundary."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient

PLAYLIST_MBID = "11111111-1111-4111-8111-111111111111"
PLAYLIST_DATA: dict[str, Any] = {
    "playlist": {
        "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
        "title": "Synthetic Recommendations",
        "track": [{"title": "Synthetic Track"}],
    }
}
PLAYLIST_LIST_DATA: dict[str, Any] = {
    "playlists": [
        {
            "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
            "title": "Synthetic Recommendations",
        }
    ]
}


def _client() -> ListenBrainzAPIClient:
    """Return an API client backed by a synthetic provider."""
    provider = MagicMock()
    provider.mass.http_session = MagicMock()
    return ListenBrainzAPIClient(provider)


async def test_get_recommendation_playlists_uses_username_path() -> None:
    """Recommendation playlists use the username-specific ListenBrainz endpoint."""
    client = _client()
    get_data = AsyncMock(return_value=PLAYLIST_LIST_DATA)
    client._get_data = get_data  # type: ignore[method-assign]

    playlists = await client.get_recommendation_playlists("synthetic-user")

    get_data.assert_awaited_once_with("/1/user/synthetic-user/playlists/recommendations")
    assert playlists == [
        {
            "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
            "title": "Synthetic Recommendations",
        }
    ]


async def test_get_playlist_uses_playlist_path() -> None:
    """A playlist lookup uses its MBID-specific ListenBrainz endpoint."""
    client = _client()
    get_data = AsyncMock(return_value=PLAYLIST_DATA)
    client._get_data = get_data  # type: ignore[method-assign]

    playlist = await client.get_playlist(PLAYLIST_MBID)

    get_data.assert_awaited_once_with(f"/1/playlist/{PLAYLIST_MBID}")
    assert playlist == PLAYLIST_DATA["playlist"]
