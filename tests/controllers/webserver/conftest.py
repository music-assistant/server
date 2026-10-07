"""Shared fixtures and helpers for the webserver controller tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock Music Assistant instance."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    return mass


@pytest.fixture
async def webserver(mass_minimal: MusicAssistant) -> AsyncIterator[WebserverController]:
    """Return a WebserverController with stubbed serialization dependencies."""
    # stub the controllers referenced by the serialization resolvers
    # (mass_minimal does not set up metadata/translations/tasks)
    mass_minimal.metadata = SimpleNamespace(  # type: ignore[assignment]
        compute_image_id=lambda provider, path: f"{provider}--{path}"
    )
    mass_minimal.translations = SimpleNamespace(  # type: ignore[assignment]
        get_translation=lambda _key, **_kwargs: None
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    yield webserver
    for client in list(webserver.clients):
        client.cancel()
    await asyncio.gather(
        *(client._handle_task for client in webserver.clients if client._handle_task),
        return_exceptions=True,
    )


def _create_ws_client(
    webserver: WebserverController,
    *,
    user: User | None = None,
    token_id: str | None = None,
    current_token: str | None = None,
    webrtc_session_id: str | None = None,
    with_handle_task: bool = False,
) -> WebsocketClientHandler:
    """
    Create a registered websocket client handler backed by a mocked request.

    :param webserver: WebserverController the client registers with.
    :param user: Authenticated user to attach, or None for an unauthenticated client.
    :param token_id: Id of the token the session authenticated with.
    :param current_token: Raw access token the session holds.
    :param webrtc_session_id: WebRTC gateway session id carried in the request query.
    :param with_handle_task: Attach a long-running handle task that teardown can cancel.
    """
    query = f"?webrtc_session_id={webrtc_session_id}" if webrtc_session_id else ""
    request = make_mocked_request("GET", f"/ws{query}", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = user
    client._token_id = token_id
    client._current_token = current_token
    if with_handle_task:
        client._handle_task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    webserver.register_websocket_client(client)
    return client
