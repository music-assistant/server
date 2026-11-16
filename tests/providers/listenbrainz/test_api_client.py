"""Tests for the ListenBrainz API client boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.models import ListenBrainzPlaylist
from music_assistant.providers.musicbrainz.listenbrainz import ListenBrainzClient

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PLAYLIST_MBID = "11111111-1111-4111-8111-111111111111"
PLAYLIST_DATA: dict[str, Any] = {
    "playlist": {
        "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
        "title": "Synthetic Recommendations",
        "track": [
            {
                "identifier": [
                    "https://musicbrainz.org/recording/22222222-2222-4222-8222-222222222222"
                ],
                "title": "Synthetic Track",
            }
        ],
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
    """Return the provider-local API client backed by synthetic dependencies."""
    mass = MagicMock()
    mass.http_session.get.return_value.__aenter__.return_value = MagicMock()
    client = ListenBrainzAPIClient(mass)
    client.logger = MagicMock()
    return client


async def test_get_createdfor_playlists_uses_username_path() -> None:
    """Created-for playlists use the username-specific ListenBrainz endpoint."""
    client = _client()
    get_data = AsyncMock(return_value=PLAYLIST_LIST_DATA)
    client._get_data = get_data  # type: ignore[method-assign]

    playlists = await client.get_createdfor_playlists("synthetic-user")

    get_data.assert_awaited_once_with("/user/synthetic-user/playlists/createdfor")
    assert playlists == [
        ListenBrainzPlaylist(playlist_id=PLAYLIST_MBID, title="Synthetic Recommendations")
    ]
    assert playlists[0].tracks is None


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

    assert [playlist.title for playlist in playlists] == [
        "Synthetic Recommendations",
        "Second Synthetic Recommendations",
    ]
    get_data.assert_has_awaits(
        [
            call("/user/synthetic-user/playlists/createdfor"),
            call("/user/synthetic-user/playlists/createdfor?offset=1"),
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

    get_data.assert_awaited_once_with(f"/playlist/{PLAYLIST_MBID}")
    assert isinstance(playlist, ListenBrainzPlaylist)
    assert playlist.playlist_id == PLAYLIST_MBID
    assert playlist.tracks is not None
    assert playlist.tracks[0].title == "Synthetic Track"


async def test_playlist_401_is_not_treated_as_an_empty_popularity_list() -> None:
    """Only the popularity endpoint interprets a 401 as a missing list."""
    client = _client()
    client.throttler = ThrottlerManager(rate_limit=100, period=1, retry_attempts=1)
    response = cast("MagicMock", client.mass.http_session.get).return_value.__aenter__.return_value
    response.status = 401
    error = ClientResponseError(MagicMock(), (), status=401)
    response.raise_for_status.side_effect = error

    with pytest.raises(ClientResponseError) as raised:
        await client.get_playlist(PLAYLIST_MBID)

    assert raised.value is error


async def test_playlist_rate_limit_holds_back_popularity_client(
    mass_minimal: MusicAssistant,
) -> None:
    """A playlist rate limit applies to popularity requests from a separate client too."""
    await mass_minimal.cache._setup_database()
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = 429
    response.headers = {"X-RateLimit-Reset-In": "120"}
    mass_minimal._http_session = session

    with patch.object(
        ListenBrainzClient,
        "throttler",
        ThrottlerManager(rate_limit=100, period=1, retry_attempts=1),
    ):
        playlists = ListenBrainzAPIClient(mass_minimal)
        popularity = ListenBrainzClient(mass_minimal)
        assert playlists.throttler is popularity.throttler
        with pytest.raises(RetriesExhausted):
            await playlists.get_playlist(PLAYLIST_MBID)
        with pytest.raises(RetriesExhausted):
            await popularity.get_top_recordings("22222222-2222-4222-8222-222222222222")

    session.get.assert_called_once()
