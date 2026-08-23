"""Unit tests for the filesystem provider's sidecar change detection and refresh."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    SidecarIndex,
    SidecarReadError,
    get_folder_signature,
)

INSTANCE_ID = "filesystem_local--test"
EMPTY = get_folder_signature([])


def _provider() -> Any:
    """Create a bare provider with the per-sync sidecar state initialized."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider._active_sidecar_index = SidecarIndex()
    provider._sync_mapped_album_dirs = set()
    return provider


def _image(provider: str, path: str) -> MediaItemImage:
    """Build a thumbnail image with the given provenance."""
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider=provider, remotely_accessible=False
    )


def _fs_mapping(item_id: str, details: str | None = None) -> ProviderMapping:
    """Build this provider's mapping for an item, optionally carrying sidecar details."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="filesystem_local",
        provider_instance=INSTANCE_ID,
        details=details,
    )


def _stored_album(details: str | None) -> Album:
    """Build a library album enriched by another provider, carrying our mapping details."""
    album = Album(
        item_id="5",
        provider="library",
        name="Old Name",
        provider_mappings={_fs_mapping("Artist/Album", details)},
    )
    album.metadata.description = "theaudiodb biography"
    album.metadata.genres = {"Rock", "Electronic"}
    album.external_ids = {(ExternalID.MB_ALBUM, "old-nfo-mbid"), (ExternalID.BARCODE, "123")}
    album.metadata.images = UniqueList([_image("theaudiodb", "remote/art.jpg")])
    return album


def _fresh_album(
    provider: Any, snapshot: dict[str, Any], external_ids: set[tuple[ExternalID, str]]
) -> Album:
    """Build a freshly reparsed provider album carrying its current details snapshot."""
    album = Album(
        item_id="Artist/Album",
        provider=INSTANCE_ID,
        name="Tag Name",
        version="",
        provider_mappings={_fs_mapping("Artist/Album")},
    )
    album.year = 1999
    album.sort_name = "Tag Name"
    album.external_ids = external_ids
    provider._set_mapping_details(album, provider._build_sidecar_details("nfo2", "img2", snapshot))
    return album


# --- classification ---------------------------------------------------------


def test_classify_detects_nfo_and_image_changes() -> None:
    """The classifier distinguishes NFO changes, image-only changes and no change."""
    classify = LocalFileSystemProvider._classify_sidecar_change
    assert classify(("a", "x", {}), "b", "x") is True  # nfo changed
    assert classify(("a", "x", {}), "a", "y") is False  # image only
    assert classify(("a", "x", {}), "a", "x") is None  # unchanged
    assert classify(None, "a", EMPTY) is True  # no baseline, nfo exists
    assert classify(None, EMPTY, "x") is False  # no baseline, image only
    assert classify(None, EMPTY, EMPTY) is None  # no baseline, no sidecars


def test_sidecar_details_round_trip() -> None:
    """Details serialize to JSON and back, and collapse to None when there are no sidecars."""
    provider = _provider()
    snap = {"description": "bio", "genres": ["Rock"], "external_ids": [["barcode", "1"]]}
    details = provider._build_sidecar_details("nfo1", "img1", snap)
    assert provider._parse_sidecar_details(details) == ("nfo1", "img1", snap)
    assert provider._build_sidecar_details(EMPTY, EMPTY, None) is None
    assert provider._parse_sidecar_details(None) is None
    assert provider._parse_sidecar_details("not json") is None


# --- reconciliation ---------------------------------------------------------


async def test_edited_nfo_reconciles_scalars_and_keeps_other_providers() -> None:
    """An edited album.nfo swaps its own genre/mbid/description while others' data survives."""
    provider = _provider()
    prev_snap = {
        "description": None,
        "genres": ["Rock"],
        "external_ids": [["musicbrainz_albumid", "old-nfo-mbid"]],
    }
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(
        return_value=UniqueList([_image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=2")])
    )
    new_snap = {
        "description": "new nfo bio",
        "genres": ["Metal"],
        "external_ids": [["musicbrainz_albumid", "new-nfo-mbid"]],
    }
    fresh = _fresh_album(
        provider, new_snap, {(ExternalID.MB_ALBUM, "new-nfo-mbid"), (ExternalID.BARCODE, "123")}
    )
    provider._reparse_album_from_track = AsyncMock(return_value=fresh)

    ok = await provider._refresh_album_sidecars(
        "Artist/Album", True, "nfo2", "img2", ("nfo1", "img1", prev_snap)
    )
    assert ok is True
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.name == "Tag Name"  # identity reconstructed from the fresh parse
    assert saved.metadata.description == "new nfo bio"
    assert saved.metadata.genres == {"Electronic", "Metal"}  # Rock (ours) dropped, Electronic kept
    assert saved.external_ids == {
        (ExternalID.MB_ALBUM, "new-nfo-mbid"),
        (ExternalID.BARCODE, "123"),
    }
    assert {img.path for img in saved.metadata.images} == {
        "remote/art.jpg",
        "Artist/Album/folder.jpg?cs=2",
    }
    # details advanced to the fresh signature/snapshot
    assert provider._parse_sidecar_details(provider._mapping_details(saved)) == (
        "nfo2",
        "img2",
        new_snap,
    )


async def test_removed_nfo_reverts_identity_and_clears_only_our_values() -> None:
    """Removing album.nfo reverts tag identity and clears only what the NFO had contributed."""
    provider = _provider()
    prev_snap = {
        "description": "our old nfo bio",
        "genres": ["Rock"],
        "external_ids": [["musicbrainz_albumid", "old-nfo-mbid"]],
    }
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    stored.name = "NFO Title"
    stored.metadata.description = "our old nfo bio"
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(return_value=UniqueList())
    # NFO gone: fresh reflects tags only (no description/genres/nfo mbid)
    empty_snap: dict[str, Any] = {"description": None, "genres": [], "external_ids": []}
    fresh = _fresh_album(provider, empty_snap, {(ExternalID.BARCODE, "123")})
    provider._reparse_album_from_track = AsyncMock(return_value=fresh)

    await provider._refresh_album_sidecars(
        "Artist/Album", True, EMPTY, "img2", ("nfo1", "img1", prev_snap)
    )
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.name == "Tag Name"  # reverted to the tag baseline
    assert saved.year == 1999
    assert saved.metadata.description is None  # was ours, now cleared
    assert saved.metadata.genres == {"Electronic"}  # our Rock dropped, other provider's kept
    assert saved.external_ids == {(ExternalID.BARCODE, "123")}  # nfo mbid removed, tag barcode kept


async def test_removed_nfo_keeps_other_providers_description() -> None:
    """A description that came from another provider (never from our NFO) is not cleared."""
    provider = _provider()
    prev_snap: dict[str, Any] = {
        "description": None,
        "genres": [],
        "external_ids": [],
    }  # NFO never set a description
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(return_value=UniqueList())
    fresh = _fresh_album(
        provider,
        {"description": None, "genres": [], "external_ids": []},
        {(ExternalID.BARCODE, "123")},
    )
    provider._reparse_album_from_track = AsyncMock(return_value=fresh)

    await provider._refresh_album_sidecars(
        "Artist/Album", True, EMPTY, "img2", ("nfo1", "img1", prev_snap)
    )
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.metadata.description == "theaudiodb biography"


async def test_image_only_refresh_leaves_scalars_untouched() -> None:
    """An image-only change refreshes artwork but never reparses a track or touches scalars."""
    provider = _provider()
    prev_snap = {"description": None, "genres": ["Rock"], "external_ids": []}
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._reparse_album_from_track = AsyncMock()
    provider._collect_album_images = AsyncMock(
        return_value=UniqueList([_image(INSTANCE_ID, "Artist/Album/folder.jpg?cs=9")])
    )

    await provider._refresh_album_sidecars(
        "Artist/Album", False, "nfo1", "img2", ("nfo1", "img1", prev_snap)
    )
    provider._reparse_album_from_track.assert_not_awaited()
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert saved.metadata.description == "theaudiodb biography"
    assert saved.metadata.genres == {"Rock", "Electronic"}
    assert {img.path for img in saved.metadata.images} == {
        "remote/art.jpg",
        "Artist/Album/folder.jpg?cs=9",
    }
    assert provider._parse_sidecar_details(provider._mapping_details(saved)) == (
        "nfo1",
        "img2",
        prev_snap,
    )


async def test_transient_read_failure_defers_without_advancing_details() -> None:
    """A transient reparse failure leaves the item unchanged so it is retried next sync."""
    provider = _provider()
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", {}))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(return_value=UniqueList())
    provider._reparse_album_from_track = AsyncMock(side_effect=SidecarReadError("network blip"))

    ok = await provider._refresh_album_sidecars(
        "Artist/Album", True, "nfo2", "img1", ("nfo1", "img1", {})
    )
    assert ok is False
    provider.mass.music.albums.update_item_in_library.assert_not_awaited()


async def test_refresh_skips_unknown_library_item() -> None:
    """A changed sidecar for a folder with no library item does nothing (no auto-add)."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    ok = await provider._refresh_album_sidecars("Artist/Album", True, "nfo2", "img1", None)
    assert ok is True
    provider.mass.music.albums.update_item_in_library.assert_not_awaited()


# --- detection over the whole library --------------------------------------


async def test_refresh_changed_sidecars_targets_only_changed_items() -> None:
    """Only items whose stored details differ from the current signature are refreshed."""
    provider = _provider()
    index = provider._active_sidecar_index
    index.record(_fs_file("Artist/Changed/album.nfo", "22"))
    index.record_track_dir("Artist/Changed")
    index.record(_fs_file("Artist/Same/album.nfo", "1"))
    index.record_track_dir("Artist/Same")
    provider._sync_mapped_album_dirs = {"Artist/Changed", "Artist/Same"}

    same_nfo, same_img = index.album_signatures("Artist/Same", provider._sync_mapped_album_dirs)
    same_details = provider._build_sidecar_details(same_nfo, same_img, {})
    provider._query_mapping_details = AsyncMock(
        return_value=(
            {
                "Artist/Changed": provider._build_sidecar_details("stale", same_img, {}),
                "Artist/Same": same_details,
            },
            {},
        )
    )
    provider._refresh_album_sidecars = AsyncMock(return_value=True)
    provider._refresh_artist_sidecars = AsyncMock(return_value=True)

    await provider._refresh_changed_sidecars(index)
    provider._refresh_album_sidecars.assert_awaited_once()
    assert provider._refresh_album_sidecars.await_args.args[0] == "Artist/Changed"
    assert provider._refresh_album_sidecars.await_args.args[1] is True  # nfo changed


def _fs_file(relative_path: str, checksum: str) -> Any:
    """Build a FileSystemItem for the index."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
        file_size=10,
    )
