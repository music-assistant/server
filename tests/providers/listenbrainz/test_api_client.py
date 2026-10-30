"""Tests for the ListenBrainz API client boundary."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

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
    "count": 25,
    "offset": 0,
    "playlist_count": 1,
    "playlists": [
        {
            "playlist": {
                "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
                "title": "Synthetic Recommendations",
            }
        }
    ],
}


def _client() -> ListenBrainzAPIClient:
    """Return an API client backed by a synthetic provider."""
    provider = MagicMock()
    provider.mass.http_session = MagicMock()
    return ListenBrainzAPIClient(provider)


async def test_get_createdfor_playlists_uses_username_path() -> None:
    """Created-for playlists use the username-specific ListenBrainz endpoint."""
    client = _client()
    get_data = AsyncMock(return_value=PLAYLIST_LIST_DATA)
    client._get_data = get_data  # type: ignore[method-assign]

    playlists = await client.get_createdfor_playlists("synthetic-user")

    get_data.assert_awaited_once_with("/1/user/synthetic-user/playlists/createdfor")
    assert playlists == [
        {
            "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
            "title": "Synthetic Recommendations",
        }
    ]


async def test_get_createdfor_playlists_fetches_all_pages() -> None:
    """Created-for playlist retrieval continues until the reported total is reached."""
    client = _client()
    second_playlist_mbid = "22222222-2222-4222-8222-222222222222"
    get_data = AsyncMock(
        side_effect=[
            {
                "count": 1,
                "offset": 0,
                "playlist_count": 2,
                "playlists": PLAYLIST_LIST_DATA["playlists"],
            },
            {
                "count": 1,
                "offset": 1,
                "playlist_count": 2,
                "playlists": [
                    {
                        "playlist": {
                            "identifier": (
                                f"https://listenbrainz.org/playlist/{second_playlist_mbid}"
                            ),
                            "title": "Second Synthetic Recommendations",
                        }
                    }
                ],
            },
        ]
    )
    client._get_data = get_data  # type: ignore[method-assign]

    playlists = await client.get_createdfor_playlists("synthetic-user")

    assert [playlist["title"] for playlist in playlists] == [
        "Synthetic Recommendations",
        "Second Synthetic Recommendations",
    ]
    get_data.assert_has_awaits(
        [
            call("/1/user/synthetic-user/playlists/createdfor"),
            call("/1/user/synthetic-user/playlists/createdfor?offset=1"),
        ]
    )
    assert get_data.await_count == 2
    cast("MagicMock", client.logger.debug).assert_has_calls(
        [
            call("Fetched %d ListenBrainz playlists at offset %d (%d of %d)", 1, 0, 1, 2),
            call("Fetched %d ListenBrainz playlists at offset %d (%d of %d)", 1, 1, 2, 2),
        ]
    )


async def test_get_playlist_uses_playlist_path() -> None:
    """A playlist lookup uses its MBID-specific ListenBrainz endpoint."""
    client = _client()
    get_data = AsyncMock(return_value=PLAYLIST_DATA)
    client._get_data = get_data  # type: ignore[method-assign]

    playlist = await client.get_playlist(PLAYLIST_MBID)

    get_data.assert_awaited_once_with(f"/1/playlist/{PLAYLIST_MBID}")
    assert playlist == PLAYLIST_DATA["playlist"]
