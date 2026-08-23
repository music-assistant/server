"""Unit tests for the filesystem provider's sidecar change detection and refresh."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import Album, MediaItemImage, UniqueList

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    SidecarIndex,
    get_folder_signature,
)

INSTANCE_ID = "filesystem_local--test"


def _file(relative_path: str, checksum: str = "1") -> FileSystemItem:
    """Build a minimal file FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
        file_size=10,
    )


def _image(provider: str, path: str) -> MediaItemImage:
    """Build a thumbnail image with the given provenance."""
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider=provider, remotely_accessible=False
    )


def _provider() -> Any:
    """Create a bare provider with the per-sync sidecar state initialized."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider._active_sidecar_index = SidecarIndex()
    provider._sync_touched_items = set()
    provider._sync_album_scalars = {}
    provider._sync_artist_scalars = {}
    return provider


def _stored_album() -> Album:
    """Return a library album enriched by another metadata provider."""
    album = Album(item_id="5", provider="library", name="Old Name", provider_mappings=set())
    album.metadata.description = "theaudiodb biography"
    album.metadata.images = UniqueList([_image("theaudiodb", "remote/art.jpg")])
    return album


# --- change classification -------------------------------------------------


def test_classify_detects_nfo_and_image_changes() -> None:
    """The classifier distinguishes NFO changes, image-only changes and no change."""
    classify = LocalFileSystemProvider._classify_sidecar_change
    empty = get_folder_signature([])
    assert classify({"nfo": "a", "img": "x"}, "b", "x") is True  # nfo changed
    assert classify({"nfo": "a", "img": "x"}, "a", "y") is False  # image only
    assert classify({"nfo": "a", "img": "x"}, "a", "x") is None  # unchanged
    # missing baseline: refresh once when sidecars exist, otherwise skip
    assert classify(None, "a", empty) is True
    assert classify(None, empty, "x") is False
    assert classify(None, empty, empty) is None


# --- detection / dedup ------------------------------------------------------


async def test_refresh_targets_only_changed_items() -> None:
    """Only the album whose sidecar signature changed is refreshed; others are left alone."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/album.nfo", checksum="2"))
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album", "Artist/Other"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    changed_nfo = provider._active_sidecar_index.album_signatures("Artist/Album")[0]
    prev_state = {
        "albums": {
            "Artist/Album": {"nfo": "old", "img": get_folder_signature([])},
            "Artist/Other": {"nfo": get_folder_signature([]), "img": get_folder_signature([])},
        },
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    new_signatures = await provider._refresh_changed_sidecars(
        provider._active_sidecar_index, prev_state
    )

    provider._refresh_album_sidecars.assert_awaited_once_with("Artist/Album", True, prev_state)
    assert new_signatures["albums"]["Artist/Album"]["nfo"] == changed_nfo


async def test_unchanged_scan_refreshes_nothing() -> None:
    """A rescan with identical sidecar signatures performs no refresh."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/album.nfo", checksum="2"))
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    nfo_sig, img_sig = provider._active_sidecar_index.album_signatures("Artist/Album")
    prev_state = {
        "albums": {"Artist/Album": {"nfo": nfo_sig, "img": img_sig}},
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    await provider._refresh_changed_sidecars(provider._active_sidecar_index, prev_state)
    provider._refresh_album_sidecars.assert_not_awaited()


async def test_disc_folder_nfo_does_not_trigger_metadata_refresh() -> None:
    """album.nfo inside a disc folder is ignored, so it never triggers an album refresh."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/Disc 1/album.nfo", checksum="2"))
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    empty = get_folder_signature([])
    prev_state = {
        "albums": {"Artist/Album": {"nfo": empty, "img": empty}},
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    await provider._refresh_changed_sidecars(provider._active_sidecar_index, prev_state)
    provider._refresh_album_sidecars.assert_not_awaited()


async def test_disc_folder_image_refreshes_parent_album_images_only() -> None:
    """A disc-folder image change refreshes the parent album as an image-only change."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/Disc 1/cover.jpg", checksum="7"))
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    empty = get_folder_signature([])
    prev_state = {
        "albums": {"Artist/Album": {"nfo": empty, "img": empty}},
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    await provider._refresh_changed_sidecars(provider._active_sidecar_index, prev_state)
    provider._refresh_album_sidecars.assert_awaited_once_with("Artist/Album", False, prev_state)


async def test_touched_items_are_skipped() -> None:
    """An album already rebuilt by track processing this sync is not refreshed again."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/album.nfo", checksum="2"))
    provider._sync_touched_items.add("Artist/Album")
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    prev_state: dict[str, Any] = {
        "albums": {},
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    await provider._refresh_changed_sidecars(provider._active_sidecar_index, prev_state)
    provider._refresh_album_sidecars.assert_not_awaited()


async def test_missing_state_refreshes_only_items_with_sidecars() -> None:
    """With no saved baseline, only mapped items that actually have sidecars refresh once."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/album.nfo", checksum="2"))
    provider._load_mapped_dirs = AsyncMock(return_value=(["Artist/Album", "Artist/NoSidecar"], []))
    provider._refresh_album_sidecars = AsyncMock()
    provider._refresh_artist_sidecars = AsyncMock()

    prev_state: dict[str, Any] = {
        "albums": {},
        "artists": {},
        "album_scalars": {},
        "artist_scalars": {},
    }
    await provider._refresh_changed_sidecars(provider._active_sidecar_index, prev_state)
    provider._refresh_album_sidecars.assert_awaited_once_with("Artist/Album", True, prev_state)


# --- reconciliation ---------------------------------------------------------


async def test_edited_nfo_applies_metadata_and_keeps_other_provider_image() -> None:
    """An edited album.nfo updates identity/description and keeps another provider's image."""
    provider = _provider()
    stored = _stored_album()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()

    fresh = Album(item_id="p", provider=INSTANCE_ID, name="New Name", provider_mappings=set())
    fresh.metadata.description = "nfo review text"
    fresh.metadata.images = UniqueList([_image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=2")])
    provider._reparse_album_from_track = AsyncMock(return_value=fresh)

    prev_state: dict[str, Any] = {"album_scalars": {}}
    await provider._refresh_album_sidecars("Artist/Album", True, prev_state)

    provider.mass.music.albums.update_item_in_library.assert_awaited_once()
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.name == "New Name"
    assert saved.metadata.description == "nfo review text"
    assert {img.path for img in saved.metadata.images} == {
        "remote/art.jpg",
        "Artist/Album/folder.jpg?cs=2",
    }


async def test_removed_nfo_reverts_identity_and_clears_our_description() -> None:
    """Removing album.nfo reverts the tag baseline and clears the description we contributed."""
    provider = _provider()
    stored = _stored_album()
    stored.name = "NFO Title"
    stored.metadata.description = "our old nfo review"
    stored.metadata.images = UniqueList(
        [
            _image("theaudiodb", "remote/art.jpg"),
            _image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=1"),
        ]
    )
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()

    # representative track rebuild: tag-derived name, no NFO description or image
    reverted = Album(item_id="p", provider=INSTANCE_ID, name="Tag Title", provider_mappings=set())
    provider._reparse_album_from_track = AsyncMock(return_value=reverted)

    prev_state = {"album_scalars": {"Artist/Album": {"description": "our old nfo review"}}}
    await provider._refresh_album_sidecars("Artist/Album", True, prev_state)

    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.name == "Tag Title"
    assert saved.metadata.description is None  # was ours and is gone -> cleared
    assert [img.path for img in saved.metadata.images] == ["remote/art.jpg"]  # our image dropped


async def test_removed_nfo_keeps_other_providers_description() -> None:
    """Removing album.nfo does not erase a description another provider set."""
    provider = _provider()
    stored = _stored_album()  # description = "theaudiodb biography"
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    reverted = Album(item_id="p", provider=INSTANCE_ID, name="Tag Title", provider_mappings=set())
    provider._reparse_album_from_track = AsyncMock(return_value=reverted)

    prev_state = {"album_scalars": {"Artist/Album": {"description": "our old nfo review"}}}
    await provider._refresh_album_sidecars("Artist/Album", True, prev_state)

    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.metadata.description == "theaudiodb biography"


async def test_image_only_refresh_leaves_scalars_untouched() -> None:
    """An image-only refresh updates artwork but does not touch NFO-derived scalar metadata."""
    provider = _provider()
    stored = _stored_album()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._reparse_album_from_track = AsyncMock()
    provider._collect_album_images = AsyncMock(
        return_value=UniqueList([_image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=9")])
    )

    prev_state: dict[str, Any] = {"album_scalars": {}}
    await provider._refresh_album_sidecars("Artist/Album", False, prev_state)

    provider._reparse_album_from_track.assert_not_awaited()
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.metadata.description == "theaudiodb biography"  # untouched
    assert {img.path for img in saved.metadata.images} == {
        "remote/art.jpg",
        "Artist/Album/folder.jpg?cs=9",
    }


async def test_refresh_skips_unknown_library_item() -> None:
    """A changed sidecar for a folder with no library item does nothing (no auto-add)."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    await provider._refresh_album_sidecars("Artist/Album", True, {"album_scalars": {}})
    provider.mass.music.albums.update_item_in_library.assert_not_awaited()


# --- persisted state --------------------------------------------------------


async def test_load_sidecar_state_is_safe_when_cache_is_empty() -> None:
    """A missing or malformed persisted state loads as a well-formed empty structure."""
    provider = _provider()
    provider.mass.cache.get = AsyncMock(return_value=None)
    state = await provider._load_sidecar_state()
    assert state == {"albums": {}, "artists": {}, "album_scalars": {}, "artist_scalars": {}}

    provider.mass.cache.get = AsyncMock(return_value={"albums": {"a": {"nfo": "x", "img": "y"}}})
    state = await provider._load_sidecar_state()
    assert state["albums"] == {"a": {"nfo": "x", "img": "y"}}
    assert state["artists"] == {}
    assert state["album_scalars"] == {}


def test_merge_scalars_keeps_current_over_previous_and_drops_gone_items() -> None:
    """Persisted scalars keep the latest snapshot per still-known dir and drop removed items."""
    known = {"Artist/Album": {"nfo": "x", "img": "y"}}
    current = {"Artist/Album": {"description": "new"}}
    previous = {"Artist/Album": {"description": "old"}, "Artist/Gone": {"description": "stale"}}
    merged = LocalFileSystemProvider._merge_scalars(known, current, previous)
    assert merged == {"Artist/Album": {"description": "new"}}
