"""Test YouTube Music Provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ytmusicapi
from aiohttp import ClientError, ServerDisconnectedError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnplayableMediaError

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ytmusic import YoutubeMusicProvider
from music_assistant.providers.ytmusic.helpers import ping_po_token_server


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
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(response=response)
    )
    assert (
        await ping_po_token_server(provider.mass.http_session, provider._po_token_server_url)
        is True
    )


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
    assert (
        await ping_po_token_server(provider.mass.http_session, provider._po_token_server_url)
        is False
    )


@pytest.mark.parametrize(("format_id", "expected"), [("141", True), ("251", False)])
async def test_premium_check_reads_the_hq_format(
    provider: YoutubeMusicProvider, format_id: str, expected: bool
) -> None:
    """Only the premium-only HQ format of the test track proves a Premium subscription."""
    with patch.object(
        provider, "_get_stream_format", AsyncMock(return_value={"format_id": format_id})
    ):
        assert await provider._user_has_ytm_premium() is expected


async def test_premium_check_reports_a_failed_test_stream(provider: YoutubeMusicProvider) -> None:
    """A test stream that cannot be fetched is a retryable setup failure, not a login failure."""
    stream_error = UnplayableMediaError("Sign in to confirm you're not a bot")
    with (
        patch.object(provider, "_get_stream_format", AsyncMock(side_effect=stream_error)),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider._user_has_ytm_premium()
    assert exc_info.value.translation_key == "stream_check_failed"
    assert "not a bot" in str(exc_info.value)
    assert exc_info.value.__cause__ is stream_error


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
    provider._headers = {}
    provider._yt_user = None
    mock_ytm = MagicMock()
    mock_ytm.search.return_value = []
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await search(provider, "test", [MediaType.TRACK])

    assert mock_ytmusic.call_args.kwargs["language"] == "en"
