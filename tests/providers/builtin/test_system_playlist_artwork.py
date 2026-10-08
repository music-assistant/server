"""Tests for the artwork of the builtin system playlists."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import MediaItemImage, Playlist, UniqueList
from PIL import Image

from music_assistant.constants import RESOURCES_DIR
from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.providers.builtin import BuiltinProvider
from music_assistant.providers.builtin.constants import (
    BUILTIN_PLAYLISTS,
    RANDOM_ALBUM,
    RECENTLY_PLAYED,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant

# the server fixture of the sync test is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _image(image_type: ImageType, path: str) -> MediaItemImage:
    """Build a bundled image of the builtin provider."""
    return MediaItemImage(type=image_type, path=path, provider="builtin", remotely_accessible=False)


# the artwork older versions stored on the system playlist rows
OLD_ARTWORK = [_image(ImageType.THUMB, "logo.png"), _image(ImageType.FANART, "fanart.jpg")]


def _artwork(playlist_id: str) -> list[MediaItemImage]:
    """Return the artwork the given system playlist is expected to have."""
    return [
        _image(ImageType.THUMB, f"playlists/{playlist_id}.png"),
        _image(ImageType.FANART, "playlists/fanart.jpg"),
    ]


def _make_provider(
    mass: MusicAssistant | None = None, instance_id: str = "builtin"
) -> BuiltinProvider:
    """Return a BuiltinProvider instance with mocked collaborators."""
    provider = BuiltinProvider.__new__(BuiltinProvider)
    provider.mass = mass or MagicMock()
    provider.logger = MagicMock()
    provider.manifest = MagicMock(domain="builtin")
    provider.config = MagicMock(instance_id=instance_id, get_value=Mock(return_value=[]))
    return provider


def _raise(_media_type: MediaType, _item_ref: str | None, err: Exception) -> None:
    """Raise the error of a failed sync item, so it fails the test instead of being skipped."""
    raise err


async def _sync(provider: BuiltinProvider, prov_item: Playlist) -> Playlist:
    """Sync the given provider playlist into the library and return the library item."""

    async def get_library_playlists() -> AsyncGenerator[Playlist]:
        yield prov_item

    # the library sync runs without per-item events and provider write-backs
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        with patch.multiple(
            provider,
            get_library_playlists=get_library_playlists,
            _update_sync_task_item_status=Mock(),
            _handle_sync_item_failure=Mock(side_effect=_raise),
        ):
            await provider._sync_library_playlists()
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
    library_item = await provider.mass.music.playlists.get_library_item_by_prov_mappings(
        prov_item.provider_mappings
    )
    assert library_item is not None
    return library_item


@pytest.mark.parametrize("playlist_id", list(BUILTIN_PLAYLISTS))
async def test_system_playlist_has_its_own_artwork(playlist_id: str) -> None:
    """A system playlist reports its own thumb first, followed by the shared fanart."""
    playlist = await _make_provider().get_playlist(playlist_id)

    assert playlist.metadata.images == _artwork(playlist_id)


@pytest.mark.parametrize("playlist_id", list(BUILTIN_PLAYLISTS))
async def test_system_playlist_artwork_is_bundled(playlist_id: str) -> None:
    """The artwork of a system playlist resolves to a truecolour file bundled with the server."""
    provider = _make_provider()
    for image in _artwork(playlist_id):
        resolved = await provider.resolve_image(image.path)

        assert resolved == str(RESOURCES_DIR.joinpath(image.path))
        assert Path(resolved).is_file()
        # Pillow resizes palette images with nearest neighbour, which gives jagged thumbnails
        with Image.open(resolved) as img:
            assert img.mode in ("RGB", "RGBA")


@pytest.mark.parametrize(
    "path", ["playlists/unknown.png", "playlists/my_playlist.png", "playlists/../logo.png"]
)
async def test_resolve_image_refuses_other_playlist_paths(path: str) -> None:
    """Only the artwork of the system playlists is served from the playlists folder."""
    with pytest.raises(FileNotFoundError):
        await _make_provider().resolve_image(path)


# a legacy install runs the builtin provider under an instance id other than its domain
@pytest.mark.parametrize(
    ("instance_id", "playlist_id"),
    [("builtin", RECENTLY_PLAYED), ("builtin--legacy", RANDOM_ALBUM)],
)
async def test_library_sync_replaces_the_old_artwork(
    music_mass_module: MusicAssistant, instance_id: str, playlist_id: str
) -> None:
    """A system playlist stored with the old shared artwork gets its own artwork at the next sync."""
    provider = _make_provider(music_mass_module, instance_id)
    old_item = await provider.get_playlist(playlist_id)
    old_item.metadata.images = UniqueList(OLD_ARTWORK)
    library_item = await _sync(provider, old_item)
    assert library_item.metadata.images == OLD_ARTWORK

    library_item = await _sync(provider, await provider.get_playlist(playlist_id))

    assert library_item.metadata.images == _artwork(playlist_id)
