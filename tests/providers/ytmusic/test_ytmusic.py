"""Test YouTube Music Provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError, ServerDisconnectedError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed

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
