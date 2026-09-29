"""Tests for the filesystem provider's deletion pass."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.filesystem_local import LocalFileSystemProvider


def _create_provider() -> tuple[LocalFileSystemProvider, dict[MediaType, MagicMock]]:
    """Create a music LocalFileSystemProvider with a mocked controller per media type."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.media_content_type = "music"
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    controllers: dict[MediaType, MagicMock] = {}
    for media_type in (MediaType.TRACK, MediaType.PLAYLIST):
        controller = MagicMock()
        controller.get_library_item_by_prov_id = AsyncMock(return_value=MagicMock(item_id="1"))
        controller.remove_item_from_library = AsyncMock()
        controllers[media_type] = controller
    provider.mass.music.get_controller = MagicMock(side_effect=controllers.__getitem__)
    return provider, controllers


@pytest.mark.parametrize(
    ("file_path", "media_type"),
    [
        ("Artist/Album/01 - Track.mp3", MediaType.TRACK),
        ("Artist/Album/08 - Track.Mp3", MediaType.TRACK),
        ("Artist/Album/40 - Track.MP3", MediaType.TRACK),
        ("Artist/Album/01 - Track.FLAC", MediaType.TRACK),
        ("Playlists/Mix.M3U", MediaType.PLAYLIST),
    ],
)
async def test_deleted_file_removed_regardless_of_extension_case(
    file_path: str, media_type: MediaType
) -> None:
    """A deleted file is removed from the library whatever the case of its extension."""
    provider, controllers = _create_provider()

    await provider._process_deletions({file_path})

    controller = controllers[media_type]
    controller.get_library_item_by_prov_id.assert_awaited_once_with(
        file_path, "filesystem_local--test"
    )
    controller.remove_item_from_library.assert_awaited_once_with("1")


async def test_unsupported_extension_is_skipped() -> None:
    """A deleted file with an unsupported extension is left alone."""
    provider, controllers = _create_provider()

    await provider._process_deletions({"Artist/Album/cover.JPG"})

    for controller in controllers.values():
        controller.get_library_item_by_prov_id.assert_not_called()
