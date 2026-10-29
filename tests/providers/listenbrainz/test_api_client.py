"""Tests for the ListenBrainz API client boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.constants import CONF_USERNAME
from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.playlist import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

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


def _provider() -> ListenBrainzProvider:
    """Return a minimally initialized provider."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    provider.mass = cast("MusicAssistant", MagicMock())
    provider.playlists = ListenBrainzPlaylistManager(provider)
    return provider


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


async def test_provider_initializes_client_with_username() -> None:
    """Provider initialization creates the client and retains the configured username."""
    provider = _provider()
    get_value = cast("MagicMock", provider.config.get_value)
    get_value.return_value = "synthetic-user"

    await provider.handle_async_init()

    assert isinstance(provider.api, ListenBrainzAPIClient)
    assert provider.username == "synthetic-user"
    get_value.assert_called_once_with(CONF_USERNAME)


async def test_browse_delegates_to_api_client() -> None:
    """Browsing obtains recommendation playlists through the API client boundary."""
    provider = _provider()
    provider.username = "synthetic-user"
    provider.api = MagicMock()
    provider.api.get_recommendation_playlists = AsyncMock(return_value=[PLAYLIST_DATA["playlist"]])

    playlists = await provider.browse("listenbrainz://")

    provider.api.get_recommendation_playlists.assert_awaited_once_with("synthetic-user")
    assert [playlist.item_id for playlist in playlists] == [PLAYLIST_MBID]
