"""Tests that the provider mapping API only accepts mappings the provider resolves."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Playlist, ProviderMapping

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the server fixture below is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")

FS_INSTANCE = "filesystem_local--AbCd"


def _mapping(item_id: str, provider_domain: str = "filesystem_local") -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id, provider_domain=provider_domain, provider_instance=FS_INSTANCE
    )


def _playlist(item_id: str) -> Playlist:
    return Playlist(
        item_id=item_id,
        provider=FS_INSTANCE,
        name=item_id,
        provider_mappings={_mapping(item_id)},
        owner="Music Assistant",
    )


def _provider(playlist: Playlist | None) -> MagicMock:
    """Return a provider that resolves the given playlist, or none at all."""
    provider = MagicMock(instance_id=FS_INSTANCE, domain="filesystem_local", available=True)
    if playlist is None:
        provider.get_playlist = AsyncMock(side_effect=MediaNotFoundError("not a playlist"))
    else:
        provider.get_playlist = AsyncMock(return_value=playlist)
    return provider


async def test_mapping_the_provider_cannot_resolve_is_refused(
    music_mass_module: MusicAssistant,
) -> None:
    """A mapping is refused when the provider does not return it as this media type."""
    db_item = await music_mass_module.music.playlists.add_item_to_library(_playlist("party.m3u"))
    mapping = _mapping("pkg/__init__.py")

    with (
        patch.object(music_mass_module, "get_provider", return_value=_provider(None)),
        pytest.raises(MediaNotFoundError),
    ):
        await music_mass_module.music.add_provider_mapping(
            MediaType.PLAYLIST, db_item.item_id, mapping
        )

    db_item = await music_mass_module.music.playlists.get_library_item(db_item.item_id)
    assert mapping not in db_item.provider_mappings


async def test_mapping_the_provider_resolves_is_added(music_mass_module: MusicAssistant) -> None:
    """A mapping the provider returns as this media type is added to the library item."""
    db_item = await music_mass_module.music.playlists.add_item_to_library(_playlist("mix.m3u"))
    mapping = _mapping("other.m3u")

    with patch.object(
        music_mass_module, "get_provider", return_value=_provider(_playlist("other.m3u"))
    ):
        await music_mass_module.music.add_provider_mapping(
            MediaType.PLAYLIST, db_item.item_id, mapping
        )

    db_item = await music_mass_module.music.playlists.get_library_item(db_item.item_id)
    assert mapping in db_item.provider_mappings


async def test_mapping_with_another_providers_domain_is_refused(
    music_mass_module: MusicAssistant,
) -> None:
    """A mapping is refused when its domain is not the domain of the named instance."""
    db_item = await music_mass_module.music.playlists.add_item_to_library(_playlist("set.m3u"))
    mapping = _mapping("more.m3u", provider_domain="spotify")

    with (
        patch.object(
            music_mass_module, "get_provider", return_value=_provider(_playlist("more.m3u"))
        ),
        pytest.raises(InvalidDataError),
    ):
        await music_mass_module.music.add_provider_mapping(
            MediaType.PLAYLIST, db_item.item_id, mapping
        )

    db_item = await music_mass_module.music.playlists.get_library_item(db_item.item_id)
    assert mapping not in db_item.provider_mappings
