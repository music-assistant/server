"""Tests for Audiobookshelf provider initialization."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
import socketio
import socketio.exceptions
from aioaudiobookshelf.client import SocketClient
from aioaudiobookshelf.client.session_configuration import SessionConfiguration
from aioaudiobookshelf.exceptions import ServiceUnavailableError as AbsServiceUnavailableError
from aiohttp import ClientConnectionError, web
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import LibraryHelper


def test_supported_features_before_async_init(provider: Audiobookshelf) -> None:
    """Provider features are available before asynchronous initialization."""
    assert ProviderFeature.PLAYLIST_CREATE_AUDIOBOOKS in provider.supported_features


@pytest.mark.asyncio
async def test_sync_library_clears_stale_library_ids(provider: Audiobookshelf) -> None:
    """sync_library removes stale library IDs that no longer exist on ABS."""
    assert "lib1" in provider.libraries.audiobooks
    provider.libraries.podcasts["pod_stale"] = LibraryHelper(name="Stale Podcast")

    mock_library = Mock()
    mock_library.id_ = "pod_lib_1"
    mock_library.name = "Podcasts"
    mock_library.media_type = "podcast"
    provider._client.get_all_libraries = AsyncMock(return_value=[mock_library])  # type: ignore[method-assign]
    provider._client.get_library_narrators = AsyncMock(return_value=[])  # type: ignore[method-assign]
    provider._client.get_my_user = AsyncMock()  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=set())  # type: ignore[method-assign]

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await provider.sync_library(MediaType.AUDIOBOOK)

    assert len(provider.libraries.audiobooks) == 0
    # Only audiobooks are cleared — podcasts dict is untouched by an AUDIOBOOK sync
    assert "pod_stale" in provider.libraries.podcasts


@pytest.mark.asyncio
async def test_sync_library_keys_prunes_stale_and_adds_new(provider: Audiobookshelf) -> None:
    """_sync_library_keys removes stale keys and adds new ones, preserving item_ids."""
    provider.libraries.audiobooks["stale_book"] = LibraryHelper(name="Stale Book")
    provider.libraries.audiobooks["existing_book"] = LibraryHelper(
        name="Existing Book", item_ids={"id_a", "id_b"}
    )
    provider.libraries.podcasts["stale_pod"] = LibraryHelper(name="Stale Podcast")

    current_book = Mock()
    current_book.id_ = "existing_book"
    current_book.name = "Existing Book"
    current_book.media_type = "book"
    current_pod = Mock()
    current_pod.id_ = "new_pod"
    current_pod.name = "New Podcast"
    current_pod.media_type = "podcast"

    provider._sync_library_keys([current_book, current_pod])

    assert list(provider.libraries.audiobooks.keys()) == ["existing_book"]
    assert provider.libraries.audiobooks["existing_book"].item_ids == {"id_a", "id_b"}
    assert list(provider.libraries.podcasts.keys()) == ["new_pod"]


@pytest.mark.parametrize("api_token", [None, "key"])
@pytest.mark.asyncio
async def test_async_init_unreachable_server(
    provider: Audiobookshelf, api_token: str | None
) -> None:
    """An unreachable server raises an error mass retries the load on."""
    setup = {
        "url": "http://abs:13378",
        "username": "user",
        "password": "pw",
        "api_token": api_token,
    }
    provider.mass.http_session.post = AsyncMock(side_effect=ClientConnectionError)  # type: ignore[method-assign]
    with (
        patch.object(provider, "get_setup_value", side_effect=setup.get),
        pytest.raises(ProviderUnavailableError),
    ):
        await provider.handle_async_init()


@pytest.mark.parametrize(
    ("failing_call", "error"),
    [
        ("get_all_libraries", AbsServiceUnavailableError),
        ("get_my_user", AbsServiceUnavailableError),
        ("init_client", socketio.exceptions.ConnectionError),
    ],
)
@pytest.mark.asyncio
async def test_async_init_server_lost_after_login(
    provider: Audiobookshelf, failing_call: str, error: type[Exception]
) -> None:
    """A server lost right after login raises an error mass retries the load on."""
    client = Mock(get_all_libraries=AsyncMock(return_value=[]), get_my_user=AsyncMock())
    socket_client = Mock(init_client=AsyncMock())
    failing_client = socket_client if failing_call == "init_client" else client
    setattr(failing_client, failing_call, AsyncMock(side_effect=error))
    with (
        patch.object(provider, "get_setup_value", return_value=None),
        patch(
            "music_assistant.providers.audiobookshelf.aioabs.get_user_and_socket_client",
            AsyncMock(return_value=(client, socket_client)),
        ),
        pytest.raises(ProviderUnavailableError),
    ):
        await provider.handle_async_init()
    # a failed init is never unloaded, so the socket must not be connected yet
    if failing_call != "init_client":
        socket_client.init_client.assert_not_awaited()


@pytest.mark.parametrize("server_running", [True, False])
@pytest.mark.asyncio
async def test_failed_socket_connect_leaves_nothing_to_shut_down(server_running: bool) -> None:
    """A failed socket connect is neither connected nor reconnecting afterwards."""
    server = socketio.AsyncServer(async_mode="aiohttp")
    server.on("connect", handler=lambda *_: False)
    app = web.Application()
    server.attach(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    if not server_running:
        await runner.cleanup()
    socket_client = SocketClient(SessionConfiguration(session=Mock(), url=url))
    try:
        with pytest.raises(socketio.exceptions.ConnectionError):
            await socket_client.init_client()
    finally:
        await runner.cleanup()
    # the two states SocketClient.shutdown() would stop
    assert not socket_client.client.connected
    assert socket_client.client._reconnect_task is None
