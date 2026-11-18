"""Authentication compatibility tests for the Jellyfin provider."""

from typing import cast
from unittest import mock

import pytest
from aiohttp import ClientResponseError
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import CACHE_CATEGORY_PREV_LIBRARY_IDS
from music_assistant.providers.jellyfin import JellyfinProvider
from music_assistant.providers.jellyfin.const import (
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAM_TYPE,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_TYPE,
)


@pytest.fixture
async def jellyfin_provider(mass: MusicAssistant) -> JellyfinProvider:
    """Load a Jellyfin provider with a mocked aiojellyfin client."""
    client = mock.Mock()
    client._session.get_json = mock.AsyncMock()
    client.audio_url = mock.Mock()
    client._access_token = "CURRENT_TOKEN"
    client.base_url = "https://jellyfin.example.com"

    with mock.patch(
        "music_assistant.providers.jellyfin.authenticate_by_name",
        mock.AsyncMock(return_value=client),
    ):
        await mass.config._create_provider_instance(
            "jellyfin",
            {},
            setup_data=mass.config._encrypt_values(
                {
                    "url": "http://localhost",
                    "username": "username",
                    "password": "password",
                }
            ),
        )

    provider = mass.get_provider("jellyfin", return_unavailable=True)
    assert provider is not None
    assert isinstance(provider, JellyfinProvider)
    return provider


async def test_get_stream_details_normalizes_legacy_audio_url(
    jellyfin_provider: JellyfinProvider,
) -> None:
    """Return normalized stream details for legacy Jellyfin audio URLs."""
    client = cast("mock.Mock", jellyfin_provider._client)
    track = _track_payload("track-1")
    client._session.get_json.return_value = track
    client.audio_url.return_value = (
        "https://jellyfin.example.com/emby/Items/track-1/stream.mp3"
        "?static=true&api_key=a%2Fb%3D&foo=bar#cover"
    )

    details = await jellyfin_provider.get_stream_details("track-1", MediaType.TRACK)

    assert details.item_id == "track-1"
    assert details.path == (
        "https://jellyfin.example.com/emby/Items/track-1/stream.mp3"
        "?static=true&apiKey=a%2Fb%3D&foo=bar#cover"
    )
    assert details.stream_type is StreamType.HTTP
    assert details.can_seek is True
    assert details.allow_seek is True
    assert details.duration == 180


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?api_key=a%2Fb%3D&x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1&apiKey=CURRENT_TOKEN",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?foo&api_key=a%2Fb%3D&foo=bar&foo=",
            "https://jellyfin.example.com/Items/track-1/art.jpg?foo=&foo=bar&foo=&apiKey=CURRENT_TOKEN",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?apiKey=a%2Fb%3D&x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1&apiKey=CURRENT_TOKEN",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1&apiKey=CURRENT_TOKEN",
        ),
        (
            "https://jellyfin.example.com.evil.test/cover.png?api_key=a%2Fb%3D",
            "https://jellyfin.example.com.evil.test/cover.png",
        ),
    ],
)
async def test_resolve_image_normalizes_legacy_urls(
    jellyfin_provider: JellyfinProvider, raw_url: str, expected_url: str
) -> None:
    """Return artwork URLs authenticated with the provider's current access token."""
    assert await jellyfin_provider.resolve_image(raw_url) == expected_url


@pytest.mark.parametrize("favorite", [True, False, None])
@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_set_favorite(
    jellyfin_provider: JellyfinProvider, favorite: bool | None, verify_ssl: bool
) -> None:
    """Write likes and removals to this account, then invalidate only its cached reads."""
    client = cast("mock.Mock", jellyfin_provider._client)
    session = mock.Mock()
    response = mock.AsyncMock()
    session.request.return_value.__aenter__ = mock.AsyncMock(return_value=response)
    session.request.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    config = SessionConfiguration(
        session=session,
        url=client.base_url,
        app_name="Music Assistant",
        app_version="test",
        device_name="test",
        device_id="test",
        verify_ssl=verify_ssl,
    )
    client._session_config = config
    client._user_id = "connected-user"
    with mock.patch.object(jellyfin_provider.mass.cache, "delete", mock.AsyncMock()) as delete:
        await jellyfin_provider.set_favorite("track-1", MediaType.TRACK, favorite)

    session.request.assert_called_once_with(
        "POST" if favorite else "DELETE",
        "https://jellyfin.example.com/Users/connected-user/FavoriteItems/track-1",
        headers={
            "Authorization": config.authentication_header("CURRENT_TOKEN"),
            "User-Agent": config.user_agent,
        },
        ssl=verify_ssl,
        raise_for_status=True,
    )
    response.read.assert_awaited_once_with()
    delete.assert_awaited_once_with(None, category=0, provider=jellyfin_provider.instance_id)
    assert ProviderFeature.FAVORITE_TRACKS_EDIT in jellyfin_provider.supported_features


async def test_set_favorite_failure(jellyfin_provider: JellyfinProvider) -> None:
    """Propagate an unsuccessful write without invalidating cached reads."""
    client = cast("mock.Mock", jellyfin_provider._client)
    request = client._session_config.session.request
    request.side_effect = ClientResponseError(mock.Mock(), (), status=403)
    with (
        mock.patch.object(jellyfin_provider.mass.cache, "delete", mock.AsyncMock()) as delete,
        pytest.raises(ClientResponseError),
    ):
        await jellyfin_provider.set_favorite("track-1", MediaType.TRACK, True)
    delete.assert_not_awaited()


async def test_set_favorite_unsupported_media(jellyfin_provider: JellyfinProvider) -> None:
    """Do not write Favorite status for media types that are not advertised."""
    client = cast("mock.Mock", jellyfin_provider._client)
    await jellyfin_provider.set_favorite("album-1", MediaType.ALBUM, True)
    client._session_config.session.request.assert_not_called()


@pytest.mark.parametrize("favorite", [True, False, None])
async def test_set_favorite_preserves_sync_cache(
    jellyfin_provider: JellyfinProvider, favorite: bool | None
) -> None:
    """Invalidate Favorite reads while keeping sync IDs and another source's reads."""
    client = cast("mock.Mock", jellyfin_provider._client)
    client._session_config.session.request = mock.MagicMock()
    cache = jellyfin_provider.mass.cache
    provider = jellyfin_provider.instance_id
    await cache.set("playlist", {"favorite": False}, provider=provider, category=0)
    await cache.set(
        "tracks", ["removed-track"], provider=provider, category=CACHE_CATEGORY_PREV_LIBRARY_IDS
    )
    await cache.set("playlist", {"favorite": True}, provider="other-source", category=0)

    await jellyfin_provider.set_favorite("track-1", MediaType.TRACK, favorite)

    assert await cache.get("playlist", provider=provider, category=0) is None
    assert await cache.get(
        "tracks", provider=provider, category=CACHE_CATEGORY_PREV_LIBRARY_IDS
    ) == ["removed-track"]
    assert await cache.get("playlist", provider="other-source", category=0) == {"favorite": True}


def _track_payload(track_id: str) -> dict[str, object]:
    """Build a minimal Jellyfin track payload."""
    return {
        ITEM_KEY_ID: track_id,
        ITEM_KEY_TYPE: "Audio",
        ITEM_KEY_RUNTIME_TICKS: 1_800_000_000,
        ITEM_KEY_MEDIA_SOURCES: [{"Container": "mp3"}],
        ITEM_KEY_MEDIA_STREAMS: [
            {
                ITEM_KEY_MEDIA_STREAM_TYPE: "Audio",
                ITEM_KEY_MEDIA_CODEC: "mp3",
                ITEM_KEY_MEDIA_CHANNELS: 2,
                "SampleRate": 48_000,
                "BitDepth": 16,
            }
        ],
    }
