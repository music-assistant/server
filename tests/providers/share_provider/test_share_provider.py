"""Tests for share-provider API lifecycle and unsupported playlists."""

from __future__ import annotations

import base64
import json
import zlib
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Playlist

from music_assistant.providers.share_provider import ShareProvider

from .helpers import _mass, _provider


async def test_share_page_address_is_an_advanced_setting() -> None:
    """The default share page works without exposing a technical configuration option."""
    entries = await _provider().get_config_entries()

    entry = next(entry for entry in entries if entry.key == "share_base_url")
    assert entry.advanced is True
    assert entry.default_value == "https://app.music-assistant.io/share"


async def test_generate_share_link_rejects_playlist_without_fetching_tracks() -> None:
    """Direct API requests cannot generate a playlist share link."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_item_by_uri = AsyncMock(
        return_value=Playlist(item_id="1", provider="library", name="Mix", provider_mappings=set())
    )

    with pytest.raises(InvalidDataError, match="Sharing playlist items is not supported"):
        await provider.generate_share_link("library://playlist/1")

    mass.music.playlists.iter_tracks.assert_not_called()
    mass.music.playlists.import_playlist.assert_not_called()


@pytest.mark.parametrize("tracks", [[], [{"title": "Track", "artist": "Artist"}]])
async def test_resolve_share_link_rejects_playlists(tracks: list[dict[str, str]]) -> None:
    """Incoming playlist links never reach matching or change the library."""
    provider = _provider()
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = (
        compressor.compress(
            json.dumps({"type": "playlist", "title": "Mix", "tracks": tracks}).encode()
        )
        + compressor.flush()
    )
    encoded = "1" + base64.urlsafe_b64encode(compressed).decode().rstrip("=")

    with patch.object(ShareProvider, "_resolve_media_item", new_callable=AsyncMock) as resolve:
        assert await provider.resolve_share_link(encoded) is None

    resolve.assert_not_awaited()
    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_api_commands_require_library_read_and_unregister_on_unload() -> None:
    """Only generation and resolution are registered, both as read operations."""
    provider = _provider()
    unregister_generate = MagicMock()
    unregister_resolve = MagicMock()
    mass = _mass(provider)
    mass.register_api_command.side_effect = [unregister_generate, unregister_resolve]

    await provider.handle_async_init()
    await provider.unload()

    assert mass.register_api_command.call_args_list == [
        call("share/generate", provider.generate_share_link, required_scope=Scope.LIBRARY_READ),
        call("share/resolve", provider.resolve_share_link, required_scope=Scope.LIBRARY_READ),
    ]
    unregister_generate.assert_called_once_with()
    unregister_resolve.assert_called_once_with()
    mass.webserver.register_dynamic_route.assert_not_called()


async def test_api_registration_failure_unregisters_prior_commands() -> None:
    """A failed later registration removes commands already registered."""
    provider = _provider()
    unregister_generate = MagicMock()
    _mass(provider).register_api_command.side_effect = [
        unregister_generate,
        RuntimeError("command already registered"),
    ]

    with pytest.raises(RuntimeError, match="command already registered"):
        await provider.handle_async_init()

    unregister_generate.assert_called_once_with()
