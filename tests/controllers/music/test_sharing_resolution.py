"""Sharing resolves real catalogues without probing builtin audio URLs."""

import asyncio
from contextlib import nullcontext
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import ItemMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.base import MediaControllerBase
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.providers.apple_music.parsers import parse_artist
from music_assistant.providers.builtin import BuiltinProvider
from music_assistant.providers.smart_playlist import SmartPlaylistProvider
from music_assistant.providers.soundcloud import SoundcloudMusicProvider
from tests.controllers.music.test_sharing import (
    SPOTIFY_ID,
    USER_PATH,
    mapping,
    set_music_source_access,
)


@pytest.fixture
def resolution() -> tuple[Any, Any]:
    """Wire the real controller and base resolution with isolated storage boundaries."""
    controller: Any = MusicController.__new__(MusicController)
    controller.mass = Mock()
    controller.mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.create_task(coro)
    controller.mass.get_provider.return_value = None
    set_music_source_access(controller.mass, {"account": None})
    media: Any = TracksController.__new__(TracksController)
    media.mass = controller.mass
    media.get = MethodType(TracksController.get, media)
    media.mass.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    media.get_provider_item = MethodType(MediaControllerBase.get_provider_item, media)
    media.get_library_item_by_prov_id = AsyncMock(return_value=None)

    def get_controller(media_type: Any) -> Any:
        media.media_type = media_type
        media.get = MethodType(
            TracksController.get if media_type == MediaType.TRACK else MediaControllerBase.get,
            media,
        )
        return media

    controller.get_controller = Mock(side_effect=get_controller)
    controller.mass.cache.handle_refresh.return_value = nullcontext()
    return controller, media


async def test_offline_library_resolution(resolution: tuple[Any, Any]) -> None:
    """A provider URI still finds an existing library item while its source is offline."""
    controller, media = resolution
    item = Track(
        item_id="1",
        provider="library",
        name="Song",
        provider_mappings={mapping("spotify", SPOTIFY_ID)},
    )
    media.get_library_item_by_prov_id.return_value = item
    result = await controller.get_item_share_info("account://track/" + SPOTIFY_ID)
    assert result["links"][0]["url"].endswith(SPOTIFY_ID)
    controller.mass.metadata.schedule_update_metadata.assert_not_called()


async def test_offline_without_library(resolution: tuple[Any, Any]) -> None:
    """Missing offline items raise through the real provider resolution path."""
    controller, media = resolution
    with pytest.raises(ProviderUnavailableError):
        await controller.get_item_share_info("account://track/" + SPOTIFY_ID)
    media.get_library_item_by_prov_id.assert_awaited_once()


@pytest.mark.parametrize("uri", ["account://track/123", "spotify://track/123"])
async def test_hidden_before_library(resolution: tuple[Any, Any], uri: str) -> None:
    """Neither a hidden original instance nor hidden domain fallback may query storage."""
    controller, media = resolution
    set_music_source_access(
        controller.mass, {"account": ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)}
    )
    controller.mass.get_provider.return_value = Mock(
        instance_id="account", domain="spotify", type=ProviderType.MUSIC
    )
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with patch(USER_PATH, return_value=user), pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info(uri)
    media.get_library_item_by_prov_id.assert_not_awaited()


@pytest.mark.parametrize("domain", ["builtin", "smart_playlist"])
@pytest.mark.parametrize("library", [False, True])
async def test_playlist_catalogues(resolution: tuple[Any, Any], domain: str, library: bool) -> None:
    """Real builtin and smart playlist methods produce text-only sharing metadata."""
    controller, media = resolution
    set_music_source_access(controller.mass, {"account": None}, {"account": domain})
    kind = ProviderType.PLUGIN if domain == "smart_playlist" else ProviderType.MUSIC
    controller.mass.config.get("providers")["account"]["type"] = kind
    provider = Mock(instance_id="account", domain=domain, type=kind)
    if domain == "builtin":
        item_id = "recently_played"
        provider.get_playlist = MethodType(BuiltinProvider.get_playlist, provider)
    else:
        item_id = "custom"
        provider._resolve_rules_for_playlist_id = AsyncMock(
            return_value=(item_id, Mock(is_dynamic=True))
        )
        provider._names_store = {item_id: "My smart playlist"}
        provider._description_for.return_value = "Description"
        provider._images_for = AsyncMock(return_value=[])
        provider._build_playlist = MethodType(SmartPlaylistProvider._build_playlist, provider)
        provider.get_playlist = MethodType(SmartPlaylistProvider.get_playlist, provider)
    controller.mass.get_provider.return_value = provider
    if library:
        media.get_library_item_by_prov_id.return_value = await provider.get_playlist(item_id)
    result = await controller.get_item_share_info(
        f"{'library' if library else domain}://playlist/{item_id}"
    )
    assert result["links"] == []
    assert result["text"]


@pytest.mark.parametrize("source", ["builtin", "account"])
@pytest.mark.parametrize("kind", ["track", "radio"])
async def test_no_builtin_audio_probe(resolution: tuple[Any, Any], source: str, kind: str) -> None:
    """Builtin aliases are rejected before any catalogue or network-capable call."""
    controller, media = resolution
    controller.mass.get_provider.return_value = Mock(domain="builtin", instance_id="account")
    with pytest.raises(InvalidDataError):
        await controller.get_item_share_info(f"{source}://{kind}/https://example.org/audio")
    media.get_library_item_by_prov_id.assert_not_awaited()
    controller.mass.get_provider.return_value.get_track.assert_not_called()
    controller.mass.get_provider.return_value.get_radio.assert_not_called()


async def test_user_builtin_playlist(resolution: tuple[Any, Any], tmp_path: Any) -> None:
    """A user-created builtin playlist uses the real M3U metadata reader path."""
    controller, _ = resolution
    set_music_source_access(controller.mass, {"account": None}, {"account": "builtin"})
    playlist_file = tmp_path / "custom.m3u"
    playlist_file.write_text("#EXTM3U\n#PLAYLIST:My playlist\n")
    provider = Mock(instance_id="account", domain="builtin", type=ProviderType.MUSIC)
    provider._playlist_file.return_value = str(playlist_file)
    provider._read_m3u_file = AsyncMock(return_value=playlist_file.read_text())
    provider.get_playlist = MethodType(BuiltinProvider.get_playlist, provider)
    controller.mass.get_provider.return_value = provider
    result = await controller.get_item_share_info("builtin://playlist/custom")
    assert result == {"title": "My playlist", "text": "My playlist", "links": []}


@pytest.mark.parametrize("reason", ["private", "wrong_domain", "other_plugin", "wrong_type"])
async def test_plugin_source_boundaries(resolution: tuple[Any, Any], reason: str) -> None:
    """Plugin catalogue permission requires matching configuration and explicit access."""
    controller, media = resolution
    set_music_source_access(controller.mass, {"account": None}, {"account": "smart_playlist"})
    config = controller.mass.config.get("providers")["account"]
    config["type"] = ProviderType.PLUGIN
    domain = "smart_playlist"
    if reason == "private":
        config["access"] = ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE).to_dict()
    elif reason == "wrong_domain":
        config["domain"] = "other"
    elif reason == "other_plugin":
        domain = config["domain"] = "other"
    else:
        config["type"] = ProviderType.PLAYER
    controller.mass.get_provider.return_value = Mock(
        instance_id="account", domain=domain, type=ProviderType.PLUGIN
    )
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with patch(USER_PATH, return_value=user), pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info("account://playlist/custom")
    media.get_library_item_by_prov_id.assert_not_awaited()


@pytest.mark.parametrize("library_hit", [False, True])
async def test_hidden_fallback_for_visible_offline_source(
    resolution: tuple[Any, Any], library_hit: bool
) -> None:
    """An authorized offline instance cannot fall through to somebody else's account."""
    controller, media = resolution
    set_music_source_access(
        controller.mass,
        {
            "account": None,
            "hidden": ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE),
        },
    )
    controller.mass.get_provider.return_value = Mock(
        instance_id="hidden", domain="spotify", type=ProviderType.MUSIC
    )
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    if library_hit:
        media.get_library_item_by_prov_id.return_value = Track(
            item_id="1",
            provider="library",
            name="Song",
            provider_mappings={mapping("spotify", SPOTIFY_ID)},
        )
    with patch(USER_PATH, return_value=user):
        if library_hit:
            result = await controller.get_item_share_info("account://track/123")
            assert result["links"][0]["provider_instance"] == "account"
        else:
            with pytest.raises(MediaNotFoundError):
                await controller.get_item_share_info("account://track/123")
    media.get_library_item_by_prov_id.assert_awaited_once_with("123", "account")
    controller.mass.cache.handle_refresh.assert_not_called()
    controller.mass.get_provider.return_value.get_track.assert_not_called()
    controller.mass.metadata.schedule_update_metadata.assert_not_called()


@pytest.mark.parametrize("domain", ["apple_music", "soundcloud"])
async def test_offline_parser_domain_uri(resolution: tuple[Any, Any], domain: str) -> None:
    """Real parser domain identities find library metadata through configured accounts."""
    controller, media = resolution
    set_music_source_access(controller.mass, {"account": None}, {"account": domain})
    media.get = MethodType(MediaControllerBase.get, media)
    provider = Mock(instance_id="account", domain=domain)
    if domain == "apple_music":
        item = parse_artist(
            provider,
            {
                "id": "123",
                "attributes": {
                    "name": "Artist",
                    "url": "https://music.apple.com/us/artist/artist/123",
                },
            },
        )
    else:
        item = await SoundcloudMusicProvider._parse_artist(
            provider, {"id": 123, "username": "Artist", "permalink": "artist"}
        )
    assert item.provider == domain
    media.get_library_item_by_prov_id.return_value = item
    result = await controller.get_item_share_info(item.uri)
    assert result["title"] == "Artist"
    assert result["links"][0]["provider_instance"] == "account"
    media.get_library_item_by_prov_id.assert_awaited_once_with("123", "account")


@pytest.mark.parametrize("domain", ["apple_music", "soundcloud"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_offline_domain_access_boundaries(
    resolution: tuple[Any, Any], domain: str, fallback: bool
) -> None:
    """Domain lookups never query a private account or use a hidden live fallback."""
    controller, media = resolution
    access = ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE)
    set_music_source_access(
        controller.mass,
        {"account": None if fallback else access, "hidden": access},
        {"account": domain, "hidden": domain},
    )
    if fallback:
        controller.mass.get_provider.return_value = Mock(
            instance_id="hidden", domain=domain, type=ProviderType.MUSIC
        )
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with patch(USER_PATH, return_value=user), pytest.raises(MediaNotFoundError):
        await controller.get_item_share_info(f"{domain}://artist/123")
    assert all(
        call.args[1] == "account" for call in media.get_library_item_by_prov_id.await_args_list
    )
    if not fallback:
        media.get_library_item_by_prov_id.assert_not_awaited()
    if fallback:
        controller.mass.get_provider.return_value.get_artist.assert_not_called()


async def test_shallow_track_retains_fallback_labels(resolution: tuple[Any, Any]) -> None:
    """The real track controller keeps mapping labels without fetching nested endpoints."""
    controller, _ = resolution
    set_music_source_access(controller.mass, {"account": None}, {"account": "filesystem_local"})
    track = Track(
        item_id="track",
        provider="account",
        name="Song",
        provider_mappings={mapping("filesystem_local", "track")},
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST, item_id="artist", provider="account", name="Artist"
                )
            ]
        ),
        album=ItemMapping(
            media_type=MediaType.ALBUM, item_id="album", provider="account", name="Album"
        ),
    )
    provider = Mock(instance_id="account", domain="filesystem_local", type=ProviderType.MUSIC)
    provider.get_track = AsyncMock(return_value=track)
    provider.get_artist = AsyncMock(side_effect=ProviderUnavailableError("Artist offline"))
    provider.get_album = AsyncMock(side_effect=ProviderUnavailableError("Album offline"))
    controller.mass.get_provider.return_value = provider
    result = await controller.get_item_share_info(track.uri)
    assert result["links"] == []
    assert "Artist" in result["text"]
    assert "Album" in result["text"]
    provider.get_artist.assert_not_awaited()
    provider.get_album.assert_not_awaited()
