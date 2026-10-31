"""Transport coverage for the item sharing contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.webserver import controller as webserver_module
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.api import APICommandHandler
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from typing import Any

    from music_assistant.controllers.webserver.controller import WebserverController


@pytest.mark.parametrize(
    ("role", "uri", "status"),
    [
        (None, "library://track/1", 401),
        ("unconfigured-role", "library://track/1", 403),
        (UserRole.GUEST, "library://track/1", 200),
        (UserRole.GUEST, "https://example.org/audio.mp3", 400),
    ],
)
async def test_sharing_transport(
    webserver: WebserverController, role: str | None, uri: str, status: int
) -> None:
    """Enforce auth/scope and serialize the real handler's plain sharing response."""
    music = MusicController.__new__(MusicController)
    music.mass = MagicMock()
    set_music_source_access(music.mass, {"spotify--fixture": None})
    music.get_item_by_uri = AsyncMock(  # type: ignore[method-assign]
        return_value=Track(
            item_id="1",
            provider="library",
            name="Example",
            provider_mappings={
                ProviderMapping(
                    item_id="4cOdK2wGLETKBW3PvgPWqT",
                    provider_domain="spotify",
                    provider_instance="spotify--fixture",
                )
            },
        )
    )
    webserver.auth = MagicMock(has_users=True)
    webserver.mass.translations.ensure_locale_loaded = AsyncMock()  # type: ignore[method-assign]
    handler: Any = music.get_item_share_info
    webserver.mass.command_handlers["music/share_item"] = APICommandHandler.parse(
        "music/share_item",
        handler,
        authenticated=handler.api_authenticated,
        required_scope=handler.api_required_scope,
    )
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        headers={},
        read=AsyncMock(
            return_value=json.dumps({"command": "music/share_item", "args": {"uri": uri}}).encode()
        ),
    )
    user = User(user_id="reader", username="reader", role=role) if role else None
    try:
        with patch.object(webserver_module, "get_authenticated_user", AsyncMock(return_value=user)):
            response = await webserver._handle_jsonrpc_api_command(request)
    finally:
        set_current_user(None)
    assert response.status == status
    if status == 200:
        assert json.loads(response.text or "") == {
            "title": "Example",
            "text": None,
            "links": [
                {
                    "provider_domain": "spotify",
                    "provider_instance": "spotify--fixture",
                    "url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
                }
            ],
        }
    else:
        music.get_item_by_uri.assert_not_awaited()
