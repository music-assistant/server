"""Tests for pruning stale local mappings/images of a surviving album or artist."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, ProviderMapping

from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--test"
OTHER_PROVIDER = "spotify--test"


def _make_provider(base_path: str) -> LocalFileSystemProvider:
    """Build a LocalFileSystemProvider with dependencies mocked."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.media_content_type = "music"
    provider.base_path = base_path
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    return provider


def _mapping(item_id: str, url: str | None) -> ProviderMapping:
    """Create a filesystem_local provider mapping."""
    return ProviderMapping(
        item_id=item_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID, url=url
    )


def _image(provider: str, path: str) -> MediaItemImage:
    """Create a MediaItemImage for the given provider/path."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


async def test_prune_removes_only_the_dead_mapping_and_images(tmp_path: Path) -> None:
    """A surviving album keeps its live mapping/images and loses only the dead ones."""
    provider = _make_provider(str(tmp_path))
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "track1.mp3").write_text("x")
    (tmp_path / "Artist" / "Album" / "folder.jpg").write_text("x")

    library_item = MagicMock(
        provider_mappings=[
            _mapping("Artist/Album", "Artist/Album"),
            _mapping("Artist/OldAlbumFolder", "Artist/OldAlbumFolder"),
            _mapping("Someartist/Somealbum", None),
        ],
        metadata=MagicMock(
            images=[
                _image(INSTANCE_ID, "Artist/Album/deleted_track.mp3"),
                _image(INSTANCE_ID, "Artist/Album/track1.mp3"),
                _image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=abc"),
                _image(OTHER_PROVIDER, "Artist/Album/deleted_track.mp3"),
            ]
        ),
    )
    controller = MagicMock()
    controller.get_library_item = AsyncMock(return_value=library_item)
    controller.remove_provider_mapping = AsyncMock()
    controller.remove_provider_images = AsyncMock()

    await provider._prune_missing_local_refs(controller, 1)

    controller.remove_provider_mapping.assert_awaited_once_with(
        1, INSTANCE_ID, "Artist/OldAlbumFolder"
    )
    controller.remove_provider_images.assert_awaited_once_with(
        1, INSTANCE_ID, {"Artist/Album/deleted_track.mp3"}
    )


async def test_prune_never_drops_the_last_mapping(tmp_path: Path) -> None:
    """A mapping that points at a missing folder is kept when it is the item's only one."""
    provider = _make_provider(str(tmp_path))
    library_item = MagicMock(
        provider_mappings=[_mapping("Artist/OldAlbumFolder", "Artist/OldAlbumFolder")],
        metadata=MagicMock(images=[]),
    )
    controller = MagicMock()
    controller.get_library_item = AsyncMock(return_value=library_item)
    controller.remove_provider_mapping = AsyncMock()
    controller.remove_provider_images = AsyncMock()

    await provider._prune_missing_local_refs(controller, 2)

    controller.remove_provider_mapping.assert_not_called()
    controller.remove_provider_images.assert_not_called()

