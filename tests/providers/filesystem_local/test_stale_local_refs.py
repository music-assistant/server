"""Tests for pruning the links and images of albums and artists to local files that no longer exist."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
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


def _controller(
    provider_mappings: list[ProviderMapping], images: list[MediaItemImage] | None = None
) -> MagicMock:
    """Create a controller whose library item has the given mappings and images."""
    controller = MagicMock()
    controller.get_library_item = AsyncMock(
        return_value=MagicMock(
            provider_mappings=provider_mappings, metadata=MagicMock(images=images or [])
        )
    )
    controller.remove_provider_mapping = AsyncMock()
    controller.remove_provider_images = AsyncMock()
    return controller


async def test_prune_removes_only_the_dead_link_and_images(tmp_path: Path) -> None:
    """A surviving album keeps its live link and images and loses only the dead ones."""
    provider = _make_provider(str(tmp_path))
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "track1.mp3").write_text("x")
    (tmp_path / "Artist" / "Album" / "folder.jpg").write_text("x")
    controller = _controller(
        [
            _mapping("Artist/Album", "Artist/Album"),
            _mapping("Artist/OldAlbumFolder", "Artist/OldAlbumFolder"),
            _mapping("Someartist/Somealbum", None),
        ],
        [
            _image(INSTANCE_ID, "Artist/Album/deleted_track.mp3"),
            _image(INSTANCE_ID, "Artist/Album/track1.mp3"),
            _image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=abc"),
            _image(OTHER_PROVIDER, "Artist/Album/deleted_track.mp3"),
        ],
    )
    provider._storage_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]

    assert await provider._prune_missing_local_refs(controller, [1])

    controller.remove_provider_mapping.assert_awaited_once_with(
        1, INSTANCE_ID, "Artist/OldAlbumFolder"
    )
    controller.remove_provider_images.assert_awaited_once_with(
        1, INSTANCE_ID, {"Artist/Album/deleted_track.mp3"}
    )


async def test_prune_never_drops_the_last_link(tmp_path: Path) -> None:
    """A link to a missing folder is kept when it is the item's only one."""
    provider = _make_provider(str(tmp_path))
    controller = _controller([_mapping("Artist/OldAlbumFolder", "Artist/OldAlbumFolder")])

    assert await provider._prune_missing_local_refs(controller, [2])

    controller.remove_provider_mapping.assert_not_called()
    controller.remove_provider_images.assert_not_called()


async def test_prune_keeps_one_link_when_all_are_dead(tmp_path: Path) -> None:
    """When every link points at a missing folder, all but one are dropped."""
    provider = _make_provider(str(tmp_path))
    controller = _controller(
        [
            _mapping("Artist/OldFolder1", "Artist/OldFolder1"),
            _mapping("Artist/OldFolder2", "Artist/OldFolder2"),
        ]
    )
    provider._storage_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]

    assert await provider._prune_missing_local_refs(controller, [3])

    controller.remove_provider_mapping.assert_awaited_once_with(3, INSTANCE_ID, "Artist/OldFolder1")


async def test_prune_keeps_everything_when_the_storage_dropped(tmp_path: Path) -> None:
    """Storage that drops during the checks reads as missing folders, so nothing is removed."""
    provider = _make_provider(str(tmp_path))
    controller = _controller(
        [
            _mapping("Artist/Album", "Artist/Album"),
            _mapping("Artist/OldFolder", "Artist/OldFolder"),
        ]
    )
    provider._storage_reachable = AsyncMock(return_value=False)  # type: ignore[method-assign]

    assert not await provider._prune_missing_local_refs(controller, [4])

    controller.remove_provider_mapping.assert_not_called()


async def test_prune_treats_a_refused_path_as_missing(tmp_path: Path) -> None:
    """A link whose path the source refuses to read counts as missing."""
    provider = _make_provider(str(tmp_path))

    async def _exists(file_path: str) -> bool:
        if file_path == "Artist/Outside":
            raise MediaNotFoundError("outside")
        return True

    provider.exists = AsyncMock(side_effect=_exists)  # type: ignore[method-assign]
    provider._storage_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]
    controller = _controller(
        [
            _mapping("Artist/Album", "Artist/Album"),
            _mapping("Artist/Outside", "Artist/Outside"),
        ]
    )

    assert await provider._prune_missing_local_refs(controller, [6])

    controller.remove_provider_mapping.assert_awaited_once_with(6, INSTANCE_ID, "Artist/Outside")


async def test_prune_keeps_everything_when_a_probe_times_out(tmp_path: Path) -> None:
    """A remote storage that raises while being probed keeps every link."""
    provider = _make_provider(str(tmp_path))
    controller = _controller(
        [
            _mapping("Artist/Album", "Artist/Album"),
            _mapping("Artist/Old", "Artist/Old"),
        ]
    )
    provider.exists = AsyncMock(side_effect=ProviderUnavailableError("timeout"))  # type: ignore[method-assign]
    storage_reachable = AsyncMock()
    provider._storage_reachable = storage_reachable  # type: ignore[method-assign]

    assert not await provider._prune_missing_local_refs(controller, [6])

    controller.remove_provider_mapping.assert_not_called()
    storage_reachable.assert_not_awaited()


async def test_prune_without_missing_folders_skips_the_storage_check(tmp_path: Path) -> None:
    """Items whose links all exist do not probe the storage."""
    provider = _make_provider(str(tmp_path))
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    controller = _controller([_mapping("Artist/Album", "Artist/Album")])
    storage_reachable = AsyncMock(return_value=True)
    provider._storage_reachable = storage_reachable  # type: ignore[method-assign]

    assert await provider._prune_missing_local_refs(controller, [5])

    storage_reachable.assert_not_awaited()


async def test_storage_check_treats_a_raised_outage_as_unreachable(tmp_path: Path) -> None:
    """A reachability check that raises, as a cloud drive does on an outage, counts as unreachable."""
    provider = _make_provider(str(tmp_path))
    provider._is_reachable = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderUnavailableError("offline")
    )

    assert not await provider._storage_reachable()


async def test_prune_all_visits_every_album_and_artist_of_this_instance(tmp_path: Path) -> None:
    """Every album and artist linked to this instance gets its dead links pruned."""
    provider = _make_provider(str(tmp_path))
    mass = cast("MagicMock", provider.mass)
    get_rows = AsyncMock(side_effect=[[{"item_id": 1}, {"item_id": 2}], [{"item_id": 3}]])
    mass.music.database.get_rows_from_query = get_rows
    prune = AsyncMock(return_value=True)
    provider._prune_missing_local_refs = prune  # type: ignore[method-assign]

    assert await provider._prune_all_missing_local_refs()

    album_query, artist_query = (query_call.args[0] for query_call in get_rows.await_args_list)
    assert f"provider_instance = '{INSTANCE_ID}'" in album_query
    assert "media_type = 'album'" in album_query
    assert f"provider_instance = '{INSTANCE_ID}'" in artist_query
    assert "media_type = 'artist'" in artist_query
    assert prune.await_args_list == [
        call(mass.music.albums, [1, 2]),
        call(mass.music.artists, [3]),
    ]


async def test_prune_all_stops_when_the_album_check_could_not_run(tmp_path: Path) -> None:
    """A pass that kept album links because the storage dropped is not done."""
    provider = _make_provider(str(tmp_path))
    mass = cast("MagicMock", provider.mass)
    mass.music.database.get_rows_from_query = AsyncMock(
        side_effect=[[{"item_id": 1}], [{"item_id": 3}]]
    )
    prune = AsyncMock(return_value=False)
    provider._prune_missing_local_refs = prune  # type: ignore[method-assign]

    assert not await provider._prune_all_missing_local_refs()

    prune.assert_awaited_once_with(mass.music.albums, [1])
