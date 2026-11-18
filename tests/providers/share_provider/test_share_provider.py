"""Tests for share-provider API lifecycle and unsupported playlists."""

from __future__ import annotations

import base64
import json
import zlib
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.enums import ProviderSharing
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.providers.share_provider import ShareProvider

from .helpers import _mass, _provider, _track


async def test_share_page_address_is_an_advanced_setting() -> None:
    """The default share page works without exposing a technical configuration option."""
    entries = await _provider().get_config_entries()

    entry = next(entry for entry in entries if entry.key == "share_base_url")
    assert entry.advanced is True
    assert entry.default_value == "https://share.music-assistant.io"


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
    """Share commands are registered as read operations and removed on unload."""
    provider = _provider()
    unregister_generate = MagicMock()
    unregister_resolve = MagicMock()
    unregister_item = MagicMock()
    mass = _mass(provider)
    mass.register_api_command.side_effect = [
        unregister_generate,
        unregister_resolve,
        unregister_item,
    ]

    await provider.handle_async_init()
    await provider.unload()

    assert mass.register_api_command.call_args_list == [
        call("share/generate", provider.generate_share_link, required_scope=Scope.LIBRARY_READ),
        call("share/resolve", provider.resolve_share_link, required_scope=Scope.LIBRARY_READ),
        call("share/item", provider.get_item_share_info, required_scope=Scope.LIBRARY_READ),
    ]
    unregister_generate.assert_called_once_with()
    unregister_resolve.assert_called_once_with()
    unregister_item.assert_called_once_with()
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


async def test_item_share_info_returns_mapping_urls_and_filters_hidden_sources() -> None:
    """The API returns only supported URLs from visible provider instances."""
    provider = _provider()
    mass = _mass(provider)
    configs = {
        "spotify--visible": {"type": "music", "domain": "spotify"},
        "spotify--private": {
            "type": "music",
            "domain": "spotify",
            "access": {"owner": "other", "sharing": ProviderSharing.PRIVATE},
        },
    }
    mass.config.get.side_effect = lambda key, default=None: (
        configs if key == "providers" else configs.get(key.removeprefix("providers/"), default)
    )
    mappings = {
        ProviderMapping(
            item_id="1",
            provider_domain="spotify",
            provider_instance="spotify--visible",
            url="https://open.spotify.com/track/1",
        ),
        ProviderMapping(
            item_id="2",
            provider_domain="spotify",
            provider_instance="spotify--private",
            url="https://open.spotify.com/track/2",
        ),
    }
    mass.music.get_item_by_uri = AsyncMock(return_value=_track(provider_mappings=mappings))
    user = User(user_id="me", username="me", role=UserRole.USER)
    with patch("music_assistant.providers.share_provider.get_current_user", return_value=user):
        result = await provider.get_item_share_info("library://track/1")
    assert result.to_dict() == {
        "title": "Track",
        "text": None,
        "links": [
            {
                "provider_domain": "spotify",
                "provider_instance": "spotify--visible",
                "url": "https://open.spotify.com/track/1",
            }
        ],
    }


async def test_item_share_info_uses_text_for_unsupported_mapping_urls() -> None:
    """An unsupported mapping URL is never returned as a provider link."""
    provider = _provider()
    mass = _mass(provider)
    configs = {"local--1": {"type": "music", "domain": "filesystem_local"}}
    mass.config.get.side_effect = lambda key, default=None: (
        configs if key == "providers" else configs.get(key.removeprefix("providers/"), default)
    )
    track = _track(
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="filesystem_local",
                provider_instance="local--1",
                url="http://localhost/private",
            )
        }
    )
    mass.music.get_item_by_uri = AsyncMock(return_value=track)
    result = await provider.get_item_share_info("library://track/1")
    assert result.to_dict() == {"title": "Track", "text": "Track", "links": []}


@pytest.mark.parametrize(
    "config",
    [
        None,
        {"type": "player", "domain": "spotify"},
        {"type": "music", "domain": "tidal"},
    ],
    ids=["missing", "non-music", "different-domain"],
)
async def test_item_share_info_refuses_invalid_mapping_source(
    config: dict[str, str] | None,
) -> None:
    """Mappings must belong to a configured music provider of the stated domain."""
    provider = _provider()
    mass = _mass(provider)
    configs = {"spotify--1": config} if config is not None else {}
    mass.config.get.side_effect = lambda key, default=None: (
        configs if key == "providers" else configs.get(key.removeprefix("providers/"), default)
    )
    mass.music.get_item_by_uri = AsyncMock(
        return_value=_track(
            provider_mappings={
                ProviderMapping(
                    item_id="1",
                    provider_domain="spotify",
                    provider_instance="spotify--1",
                    url="https://open.spotify.com/track/1",
                )
            }
        )
    )

    with pytest.raises(MediaNotFoundError):
        await provider.get_item_share_info("library://track/1")


async def test_item_share_info_includes_disabled_visible_source() -> None:
    """A disabled music source remains visible when its access permits it."""
    provider = _provider()
    mass = _mass(provider)
    configs = {"spotify--1": {"type": "music", "domain": "spotify", "enabled": False}}
    mass.config.get.side_effect = lambda key, default=None: (
        configs if key == "providers" else configs.get(key.removeprefix("providers/"), default)
    )
    mass.music.get_item_by_uri = AsyncMock(
        return_value=_track(
            provider_mappings={
                ProviderMapping(
                    item_id="1",
                    provider_domain="spotify",
                    provider_instance="spotify--1",
                    url="https://open.spotify.com/track/1",
                )
            }
        )
    )

    result = await provider.get_item_share_info("library://track/1")
    assert [link.url for link in result.links] == ["https://open.spotify.com/track/1"]


async def test_item_share_info_refuses_hidden_item_before_resolving() -> None:
    """The requested provider must be visible to the current user."""
    provider = _provider()
    mass = _mass(provider)
    mass.config.get.return_value = {
        "spotify--private": {
            "type": "music",
            "domain": "spotify",
            "access": {"owner": "other", "sharing": ProviderSharing.PRIVATE},
        }
    }
    mass.music.get_item_by_uri = AsyncMock()
    user = User(user_id="me", username="me", role=UserRole.USER)
    with (
        patch("music_assistant.providers.share_provider.get_current_user", return_value=user),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_item_share_info("spotify--private://track/1")
    mass.music.get_item_by_uri.assert_not_awaited()
