"""Unit tests for the filesystem provider's sidecar change detection and refresh."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
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
    provider.cache = MagicMock()
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._active_sidecar_index = SidecarIndex()
    provider._sync_mapped_album_dirs = set()
    provider._pre_scan_album_details = {}
    provider._pre_scan_artist_details = {}
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


def _stored_artist(details: str | None) -> Artist:
    """Build a library artist enriched by another provider, carrying our mapping details."""
    artist = Artist(
        item_id="7",
        provider="library",
        name="NFO Artist",
        provider_mappings={_fs_mapping("Artist", details)},
    )
    artist.sort_name = "NFO Artist"
    artist.external_ids = {(ExternalID.MB_ARTIST, "old-nfo-artist-mbid")}
    artist.metadata.genres = {"Jazz", "Ambient"}
    return artist


def _fresh_artist(
    provider: Any, snapshot: dict[str, Any], external_ids: set[tuple[ExternalID, str]]
) -> Artist:
    """Build a freshly reparsed provider artist carrying its current details snapshot."""
    artist = Artist(
        item_id="Artist",
        provider=INSTANCE_ID,
        name="Tag Artist",
        provider_mappings={_fs_mapping("Artist")},
    )
    artist.sort_name = "Tag Artist"
    artist.external_ids = external_ids
    provider._set_mapping_details(artist, provider._build_sidecar_details("nfo2", "img2", snapshot))
    return artist


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


async def test_removed_nfo_drops_nfo_only_album_artist() -> None:
    """Album artists are restored from the fresh parse, so an NFO-only album artist disappears."""
    provider = _provider()
    prev_snap: dict[str, Any] = {"description": None, "genres": [], "external_ids": []}
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    stored.artists = UniqueList(
        [
            ItemMapping(
                media_type=stored.media_type, item_id="nfo-only", provider="library", name="X"
            )
        ]
    )
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(return_value=UniqueList())
    fresh = _fresh_album(provider, prev_snap, {(ExternalID.BARCODE, "123")})
    tag_artist = Artist(
        item_id="Artist", provider=INSTANCE_ID, name="Tag Artist", provider_mappings=set()
    )
    fresh.artists = UniqueList([tag_artist])
    provider._reparse_album_from_track = AsyncMock(return_value=fresh)

    await provider._refresh_album_sidecars(
        "Artist/Album", True, "nfo2", "img1", ("nfo1", "img1", prev_snap)
    )
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    assert [a.name for a in saved.artists] == ["Tag Artist"]  # NFO-only album artist gone


async def test_removed_artist_nfo_reverts_sort_keeps_mbid() -> None:
    """Removing artist.nfo reverts sort/genres but keeps the sticky artist MBID."""
    provider = _provider()
    prev_snap: dict[str, Any] = {
        "description": "our nfo bio",
        "genres": ["Jazz"],
        "external_ids": [["musicbrainz_artistid", "old-nfo-artist-mbid"]],
    }
    stored = _stored_artist(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.artists.update_item_in_library = AsyncMock()
    provider._invalidate_artist_caches = AsyncMock()
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    fresh = _fresh_artist(provider, {"description": None, "genres": [], "external_ids": []}, set())
    provider._reparse_artist_from_track = AsyncMock(return_value=fresh)

    ok = await provider._refresh_artist_sidecars(
        "Artist", True, EMPTY, "img2", ("nfo1", "img1", prev_snap)
    )
    assert ok is True
    saved = provider.mass.music.artists.update_item_in_library.await_args.args[1]
    assert saved.sort_name == "Tag Artist"  # reverted to the tag baseline
    assert saved.mbid == "old-nfo-artist-mbid"  # identity id is sticky, not cleared
    assert saved.metadata.genres == {"Ambient"}  # our Jazz dropped, other provider's kept
    assert saved.metadata.description is None  # our bio cleared


async def test_collect_album_images_spans_all_disc_folders() -> None:
    """The complete album image set includes every real disc folder, not just one track's disc."""
    provider = _provider()
    index = provider._active_sidecar_index
    for disc in ("Disc 1", "Disc 2"):
        index.record(_fs_file(f"Artist/Album/{disc}/folder.jpg", "1"))
        index.record_track_dir(f"Artist/Album/{disc}")
    provider._sync_mapped_album_dirs = {"Artist/Album"}

    images = await provider._collect_album_images("Artist/Album")
    assert {img.path for img in images} == {
        "Artist/Album/Disc 1/folder.jpg?cs=1",
        "Artist/Album/Disc 2/folder.jpg?cs=1",
    }


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


async def test_artist_transient_read_failure_defers() -> None:
    """A transient artist reparse failure leaves the artist unchanged and retries next sync."""
    provider = _provider()
    stored = _stored_artist(provider._build_sidecar_details("nfo1", "img1", {}))
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.artists.update_item_in_library = AsyncMock()
    provider._invalidate_artist_caches = AsyncMock()
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._reparse_artist_from_track = AsyncMock(side_effect=SidecarReadError("blip"))

    ok = await provider._refresh_artist_sidecars(
        "Artist", True, "nfo2", "img1", ("nfo1", "img1", {})
    )
    assert ok is False
    provider.mass.music.artists.update_item_in_library.assert_not_awaited()


async def test_artist_image_only_removal_clears_art() -> None:
    """Removing the last artist folder image clears our artwork without reparsing a track."""
    provider = _provider()
    prev_snap: dict[str, Any] = {"description": None, "genres": [], "external_ids": []}
    stored = _stored_artist(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    stored.metadata.images = UniqueList([_image(INSTANCE_ID, "Artist/folder.jpg?cs=1")])
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.artists.update_item_in_library = AsyncMock()
    provider._invalidate_artist_caches = AsyncMock()
    provider._reparse_artist_from_track = AsyncMock()
    provider._get_local_images = AsyncMock(return_value=UniqueList())  # image removed

    await provider._refresh_artist_sidecars(
        "Artist", False, "nfo1", EMPTY, ("nfo1", "img1", prev_snap)
    )
    provider._reparse_artist_from_track.assert_not_awaited()
    saved = provider.mass.music.artists.update_item_in_library.await_args.args[1]
    assert not saved.metadata.images  # our only image cleared
    # a provenance baseline exists, so the clear is persisted authoritatively
    assert (
        provider.mass.music.artists.update_item_in_library.await_args.kwargs["full_replace"] is True
    )


async def test_refresh_skips_unknown_library_item() -> None:
    """A changed sidecar for a folder with no library item does nothing (no auto-add)."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    ok = await provider._refresh_album_sidecars("Artist/Album", True, "nfo2", "img1", None)
    assert ok is True
    provider.mass.music.albums.update_item_in_library.assert_not_awaited()


async def test_album_refresh_keeps_snapshot_when_no_representative_track() -> None:
    """A changed NFO with no readable representative track keeps the NFO ownership snapshot."""
    provider = _provider()
    prev_snap = {"description": "our nfo bio", "genres": ["Rock"], "external_ids": []}
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._collect_album_images = AsyncMock(return_value=UniqueList())
    provider._reparse_album_from_track = AsyncMock(
        return_value=None
    )  # no filesystem track to reparse

    ok = await provider._refresh_album_sidecars(
        "Artist/Album", True, "nfo2", "img2", ("nfo1", "img1", prev_snap)
    )
    assert ok is True
    saved = provider.mass.music.albums.update_item_in_library.await_args.args[1]
    # signatures advance, but the ownership snapshot is retained so a later removal can still clear
    assert provider._parse_sidecar_details(provider._mapping_details(saved)) == (
        "nfo2",
        "img2",
        prev_snap,
    )
    # scalars we could not reparse are left untouched (not wiped)
    assert saved.metadata.description == "theaudiodb biography"
    assert saved.metadata.genres == {"Rock", "Electronic"}


async def test_artist_refresh_keeps_snapshot_when_no_representative_track() -> None:
    """A changed artist NFO with no readable representative track keeps the ownership snapshot."""
    provider = _provider()
    prev_snap = {"description": "our bio", "genres": ["Jazz"], "external_ids": []}
    stored = _stored_artist(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.artists.update_item_in_library = AsyncMock()
    provider._invalidate_artist_caches = AsyncMock()
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._reparse_artist_from_track = AsyncMock(return_value=None)

    ok = await provider._refresh_artist_sidecars(
        "Artist", True, "nfo2", "img2", ("nfo1", "img1", prev_snap)
    )
    assert ok is True
    saved = provider.mass.music.artists.update_item_in_library.await_args.args[1]
    assert provider._parse_sidecar_details(provider._mapping_details(saved)) == (
        "nfo2",
        "img2",
        prev_snap,
    )
    assert saved.metadata.genres == {"Jazz", "Ambient"}


async def test_album_refresh_keeps_prior_metadata_when_nfo_malformed() -> None:
    """A valid->malformed album.nfo edit keeps the prior metadata instead of wiping it."""
    provider = _provider()
    provider._active_sidecar_index.record(_fs_file("Artist/Album/album.nfo", "2"))
    provider._read_file = AsyncMock(return_value=b"<album>just text</album>")  # malformed now
    prev_snap = {"description": "our nfo bio", "genres": ["Rock"], "external_ids": []}
    stored = _stored_album(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.albums.update_item_in_library = AsyncMock()
    provider._invalidate_album_caches = AsyncMock()
    provider._reparse_album_from_track = AsyncMock()

    ok = await provider._refresh_album_sidecars(
        "Artist/Album", True, "nfo2", "img1", ("nfo1", "img1", prev_snap)
    )
    assert ok is False  # deferred, non-destructive
    provider._reparse_album_from_track.assert_not_awaited()  # never reparsed against the bad NFO
    provider.mass.music.albums.update_item_in_library.assert_not_awaited()  # prior metadata kept


async def test_artist_refresh_keeps_prior_metadata_when_nfo_malformed() -> None:
    """A valid->malformed artist.nfo edit keeps the prior metadata instead of wiping it."""
    provider = _provider()
    provider._active_sidecar_index.record(_fs_file("Artist/artist.nfo", "2"))
    provider._read_file = AsyncMock(return_value=b"<artist>just text</artist>")  # malformed now
    prev_snap = {"description": "our bio", "genres": ["Jazz"], "external_ids": []}
    stored = _stored_artist(provider._build_sidecar_details("nfo1", "img1", prev_snap))
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    provider.mass.music.artists.update_item_in_library = AsyncMock()
    provider._invalidate_artist_caches = AsyncMock()
    provider._reparse_artist_from_track = AsyncMock()

    ok = await provider._refresh_artist_sidecars(
        "Artist", True, "nfo2", "img1", ("nfo1", "img1", prev_snap)
    )
    assert ok is False
    provider._reparse_artist_from_track.assert_not_awaited()
    provider.mass.music.artists.update_item_in_library.assert_not_awaited()


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


async def test_refresh_excludes_first_sync_nested_album_from_parent_artwork() -> None:
    """A nested album discovered this sync is refreshed into the mapped set before signatures."""
    provider = _provider()
    index = provider._active_sidecar_index
    index.record(_fs_file("Artist/Album/folder.jpg", "1"))
    index.record_track_dir("Artist/Album")
    index.record(_fs_file("Artist/Album/Nested/folder.jpg", "2"))
    index.record_track_dir("Artist/Album/Nested")
    # the pre-scan set is empty: both albums were created during this very sync
    provider._sync_mapped_album_dirs = set()
    provider._query_mapping_details = AsyncMock(
        return_value=(
            {
                "Artist/Album": provider._build_sidecar_details("x", "y", {}),
                "Artist/Album/Nested": None,
            },
            {},
        )
    )
    captured: dict[str, str] = {}

    async def _capture(
        album_dir: str, _changed: bool, _nfo: str, img_sig: str, _prev: object
    ) -> bool:
        captured[album_dir] = img_sig
        return True

    provider._refresh_album_sidecars = _capture
    await provider._refresh_changed_sidecars(index)

    # the nested album is now known, so it is excluded from the parent's disc artwork
    assert provider._sync_mapped_album_dirs == {"Artist/Album", "Artist/Album/Nested"}
    parent_only = index.album_signatures("Artist/Album", {"Artist/Album", "Artist/Album/Nested"})[1]
    assert captured["Artist/Album"] == parent_only


async def test_refresh_classifies_existing_mapping_against_pre_scan_baseline() -> None:
    """A same-sync audio change that cleared an item's details cannot hide a removed sidecar."""
    provider = _provider()
    index = provider._active_sidecar_index
    index.record(_fs_file("Artist/Album/folder.jpg", "1"))  # image unchanged; album.nfo is gone
    index.record_track_dir("Artist/Album")
    provider._sync_mapped_album_dirs = {"Artist/Album"}
    nfo_sig, img_sig = index.album_signatures("Artist/Album", {"Artist/Album"})
    assert nfo_sig == EMPTY  # the NFO was removed this sync
    baseline_snap = {"description": "old", "genres": ["Rock"], "external_ids": []}
    # pre-scan the album carried an NFO; this-sync track processing overwrote its details to None
    provider._pre_scan_album_details = {
        "Artist/Album": provider._build_sidecar_details("nfo-old", img_sig, baseline_snap)
    }
    provider._query_mapping_details = AsyncMock(return_value=({"Artist/Album": None}, {}))
    captured: dict[str, Any] = {}

    async def _capture(_album_dir: str, changed: bool, _nfo: str, _img: str, prev: Any) -> bool:
        captured["changed"] = changed
        captured["prev"] = prev
        return True

    provider._refresh_album_sidecars = _capture
    await provider._refresh_changed_sidecars(index)

    # detected as an NFO change against the pre-scan baseline, with that baseline as prev
    assert captured["changed"] is True
    assert captured["prev"] == ("nfo-old", img_sig, baseline_snap)


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
