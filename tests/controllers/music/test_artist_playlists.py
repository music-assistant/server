"""Tests for the source-specific artist playlist endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder, ItemMapping, Playlist, ProviderMapping

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.soundcloud import SUPPORTED_FEATURES, SoundcloudMusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize(("available", "supported"), [(False, True), (True, False)])
async def test_unsupported_artist_playlists(
    music_mass_with_cache: MusicAssistant, available: bool, supported: bool
) -> None:
    """Unavailable and unsupported sources are never queried."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = available
    provider.supports_feature.return_value = supported
    with patch.object(music_mass_with_cache.music, "get_visible_provider", return_value=provider):
        assert await music_mass_with_cache.music.artists.playlists("123", "soundcloud") == []
    provider.get_artist_playlists.assert_not_awaited()


async def test_artist_playlists_passes_exact_source_id(
    music_mass_with_cache: MusicAssistant,
) -> None:
    """Library artist IDs are not substituted for provider user IDs."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = True
    provider.supports_feature.return_value = True
    playlist = Playlist(
        item_id="1",
        provider="soundcloud",
        name="Set",
        provider_mappings={
            ProviderMapping(
                item_id="1", provider_domain="soundcloud", provider_instance="soundcloud--1"
            )
        },
    )
    provider.get_artist_playlists.return_value = [playlist]
    with patch.object(music_mass_with_cache.music, "get_visible_provider", return_value=provider):
        assert await music_mass_with_cache.music.artists.playlists("123", "soundcloud--1") == [
            playlist
        ]
    provider.get_artist_playlists.assert_awaited_once_with("123")


@pytest.mark.parametrize("media_type", [MediaType.ARTIST, MediaType.TRACK, MediaType.PLAYLIST])
async def test_soundcloud_library_paging_and_search_beyond_500_items(
    music_mass_with_cache: MusicAssistant, media_type: MediaType
) -> None:
    """Artist-page support preserves paging and search over the full synced library."""
    mass = music_mass_with_cache
    manifest = MagicMock(domain="soundcloud")
    config = MagicMock(instance_id="soundcloud--test123")
    config.get_value.return_value = "GLOBAL"
    provider = SoundcloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    records = [
        ItemMapping(
            media_type=media_type, item_id=str(index), provider="library", name=f"Item {index:04d}"
        )
        for index in range(625)
    ]

    async def library_items(
        limit: int = 500, offset: int = 0, search: str | None = None, **_kwargs: Any
    ) -> list[ItemMapping]:
        matches = [item for item in records if search is None or search in item.name]
        return matches[offset : offset + limit] if limit else matches[offset:]

    controller = mass.music.get_controller(media_type)
    endpoint = AsyncMock(side_effect=library_items)
    path = f"{provider.instance_id}://{media_type.value}s"
    with (
        patch.object(mass, "get_provider", return_value=provider),
        patch.object(controller, "library_items", endpoint),
    ):
        first = await mass.music.browse(path, limit=500)
        second = await mass.music.browse(path, limit=500, offset=500)
        found = await mass.music.browse(path, search="Item 0624")
        everything = await mass.music.browse(path, limit=None)

    assert isinstance(first[0], BrowseFolder)
    assert [item.item_id for item in [*first[1:], *second]] == [str(index) for index in range(625)]
    assert [item.item_id for item in found[1:]] == ["624"]
    assert len(everything) == 626
    assert endpoint.await_args_list[1].kwargs["offset"] == 499
    assert endpoint.await_args_list[2].kwargs["search"] == "Item 0624"


async def test_artist_playlist_controller_caps_provider_results(
    music_mass_with_cache: MusicAssistant,
) -> None:
    """The public endpoint bounds a provider returning more than its advertised limit."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = True
    provider.supports_feature.return_value = True
    provider.get_artist_playlists.return_value = [
        Playlist(item_id=str(index), provider="soundcloud", name="Set", provider_mappings=set())
        for index in range(125)
    ]
    with patch.object(music_mass_with_cache.music, "get_visible_provider", return_value=provider):
        items = await music_mass_with_cache.music.artists.playlists("123", "soundcloud")
    assert len(items) == 100
    assert [item.item_id for item in items] == [str(index) for index in range(100)]
