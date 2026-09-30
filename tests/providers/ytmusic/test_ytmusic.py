"""Test YouTube Music Provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ytmusicapi
from aiohttp import ClientError, ServerDisconnectedError
from music_assistant_models.enums import AlbumType, MediaType
from music_assistant_models.errors import LoginFailed, SetupFailedError
from ytmusicapi import LikeStatus

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ytmusic import YoutubeMusicProvider


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._po_token_server_url = "http://localhost:4416"
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


def _ping_context_manager(
    *, response: MagicMock | None = None, exc: Exception | None = None
) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.get()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response, side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_verify_po_token_url_success(provider: YoutubeMusicProvider) -> None:
    """A healthy PO Token server (HTTP 200) verifies successfully."""
    response = MagicMock()
    response.status = 200
    response.raise_for_status = MagicMock()
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(response=response)
    )
    assert await provider._verify_po_token_url() is True


@pytest.mark.parametrize(
    "exc",
    [
        # boot race: the POT container's port accepts TCP before the server is serving,
        # so the ping fails with ServerDisconnectedError (a ClientError, but NOT a
        # ClientConnectorError, which is all the provider used to catch).
        ServerDisconnectedError(),
        # connection refused / DNS failure etc. (the originally-handled family).
        ClientError("connection error"),
        # an explicit/implicit request timeout.
        TimeoutError(),
    ],
)
async def test_verify_po_token_url_transient_failure(
    provider: YoutubeMusicProvider, exc: Exception
) -> None:
    """Transient PO Token server errors return False instead of escaping uncaught."""
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(exc=exc)
    )
    assert await provider._verify_po_token_url() is False


async def test_init_unreachable_po_token_server_is_a_retried_setup_failure(
    provider: YoutubeMusicProvider,
) -> None:
    """
    An unreachable PO Token server at load is a setup failure the core retries, not a login one.

    The PO Token server is a separate add-on/container that routinely comes up after
    Music Assistant on a host reboot. A LoginFailed is never retried (it waits for the
    user to fix their credentials), which left the provider dead until a manual reload.
    """
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(exc=ClientError("connection refused"))
    )
    with (
        patch.object(provider, "_install_packages", AsyncMock()),
        patch.object(provider, "get_setup_value", return_value=""),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert not isinstance(exc_info.value, LoginFailed)
    assert exc_info.value.translation_key == "po_token_server_unreachable"
    assert exc_info.value.translation_owner == "provider.ytmusic"


async def test_sync_library_unloads_on_invalid_session(provider: YoutubeMusicProvider) -> None:
    """A library sync that hits an invalid session unloads the provider for re-auth."""
    provider.available = True
    provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    err = LoginFailed("Your YouTube Music session is no longer valid.")
    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=err)),
        pytest.raises(LoginFailed),
    ):
        await provider.sync_library(MediaType.PLAYLIST)
    provider.unload_with_error.assert_called_once_with(err)


async def test_sync_library_keeps_other_errors_silent(provider: YoutubeMusicProvider) -> None:
    """Any other sync failure must not unload the provider."""
    provider.available = True
    provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=KeyError("boom"))),
        pytest.raises(KeyError),
    ):
        await provider.sync_library(MediaType.PLAYLIST)
    provider.unload_with_error.assert_not_called()


def test_parse_owned_playlist_is_editable_without_privacy(
    provider: YoutubeMusicProvider,
) -> None:
    """An owned playlist is editable even when the library response omits privacy."""
    playlist = provider._parse_playlist(
        {
            "id": "PL_owned",
            "title": "Owned playlist",
            "owned": True,
        }
    )

    assert playlist.is_editable is True


async def test_search_is_not_translated(provider: YoutubeMusicProvider) -> None:
    """A search must run in English, whatever language the server is set to."""
    # ytmusicapi matches the (translated) result shelf title against the English filter
    # name, so a filtered search silently returns nothing in most other languages.
    provider.language = "cs"
    mock_ytm = MagicMock()
    mock_ytm.search.return_value = []
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await search(provider, "test", [MediaType.TRACK])

    assert mock_ytmusic.call_args.kwargs["language"] == "en"


async def test_album_versions_with_versions(provider: YoutubeMusicProvider) -> None:
    """get_album_versions method of the YTM provider should return other album versions if any exist."""
    album_with_versions = {
        "title": "All Stand Together",
        "other_versions": [
            {"browseId": "MPREb_LzqETWfppYZ", "title": "All Stand Together (Deluxe)"}
        ],
    }
    with patch(
        "music_assistant.providers.ytmusic.get_album", AsyncMock(return_value=album_with_versions)
    ):
        # call the undecorated function so the @use_cache wrapper stays out of the test
        get_album_versions = cast("Any", YoutubeMusicProvider.get_album_versions).__wrapped__
        albums = await get_album_versions(provider, "_")

    assert albums[0].item_id == "MPREb_LzqETWfppYZ"
    assert albums[0].name == "All Stand Together"
    assert albums[0].version == "Deluxe"


async def test_album_versions_without_versions(provider: YoutubeMusicProvider) -> None:
    """get_album_versions method of the YTM provider should return nothing if there are no other versions."""
    album_without_versions = {
        "title": "All Stand Together",
    }
    with patch(
        "music_assistant.providers.ytmusic.get_album",
        AsyncMock(return_value=album_without_versions),
    ):
        # call the undecorated function so the @use_cache wrapper stays out of the test
        get_album_versions = cast("Any", YoutubeMusicProvider.get_album_versions).__wrapped__
        albums = await get_album_versions(provider, "_")

    assert len(albums) == 0


@pytest.mark.parametrize(
    ("favorite", "rating"),
    [
        (True, LikeStatus.LIKE),
        (False, LikeStatus.DISLIKE),
        (None, LikeStatus.INDIFFERENT),
    ],
)
async def test_set_favorite_rates_track(
    provider: YoutubeMusicProvider, favorite: bool | None, rating: LikeStatus
) -> None:
    """A like, dislike or unset on a track is written to the account as its rating."""
    provider._yt_user = "123"
    with patch("music_assistant.providers.ytmusic.rate_track", AsyncMock()) as mock_rate:
        await provider.set_favorite("video", MediaType.TRACK, favorite)
    mock_rate.assert_awaited_once_with(
        headers=provider._headers, prov_track_id="video", rating=rating, user="123"
    )


async def test_set_favorite_ignores_other_media_types(provider: YoutubeMusicProvider) -> None:
    """Only tracks carry a rating on YouTube Music, anything else is left alone."""
    with patch("music_assistant.providers.ytmusic.rate_track", AsyncMock()) as mock_rate:
        await provider.set_favorite("album", MediaType.ALBUM, True)
    mock_rate.assert_not_awaited()


@pytest.mark.parametrize(
    ("language", "type_label", "album_type"),
    [
        ("en", "Single", AlbumType.SINGLE),
        ("en", "EP", AlbumType.EP),
        ("en", "Album", AlbumType.ALBUM),
        ("cs", "Singl", AlbumType.SINGLE),  # codespell:ignore
        ("cs", "EP", AlbumType.EP),
        ("nl", "Ep", AlbumType.EP),
        ("es", "Álbum", AlbumType.UNKNOWN),
        ("de", "Playlist", AlbumType.UNKNOWN),
    ],
)
def test_parse_album_type_is_language_independent(
    provider: YoutubeMusicProvider, language: str, type_label: str, album_type: AlbumType
) -> None:
    """The album type YouTube Music reports in the account's language maps to the same type."""
    provider.language = language
    album = provider._parse_album({"title": "Test", "type": type_label}, "MPREb_test")
    assert album.album_type == album_type


def test_parse_thumbnails_skips_zero_height(provider: YoutubeMusicProvider) -> None:
    """A thumbnail reporting a zero height is skipped instead of crashing the parse."""
    thumbnails = [
        {"url": "https://lh3.googleusercontent.com/bad=w544-h544", "width": 544, "height": 0},
        {"url": "https://lh3.googleusercontent.com/good=w544-h544", "width": 544, "height": 544},
    ]
    images = provider._parse_thumbnails(thumbnails)
    assert [img.path for img in images] == ["https://lh3.googleusercontent.com/good=w600-h600-p"]
