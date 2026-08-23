"""Unit tests for the filesystem sidecar helpers (signatures, reconciliation, NFO validation)."""

from __future__ import annotations

import logging

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage

from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    SidecarIndex,
    get_folder_signature,
    is_sidecar_file,
    nfo_root_dict,
    reconcile_images,
    reconcile_provenance_set,
    reconcile_scalar,
)

LOG = logging.getLogger("test")
EMPTY = get_folder_signature([])


def _file(relative_path: str, checksum: str = "1", size: int = 10) -> FileSystemItem:
    """Build a minimal file FileSystemItem for the given relative path."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
        file_size=size,
    )


def _image(provider: str, path: str) -> MediaItemImage:
    """Build a thumbnail image with the given provenance."""
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider=provider, remotely_accessible=False
    )


def test_is_sidecar_file_recognizes_nfo_and_named_images() -> None:
    """Only album.nfo/artist.nfo and recognized image stems are treated as sidecars."""
    assert is_sidecar_file(_file("Artist/Album/album.nfo"))
    assert is_sidecar_file(_file("Artist/Album/folder.jpg"))
    assert is_sidecar_file(_file("Artist/Album/fanart.jpg"))
    assert not is_sidecar_file(_file("Artist/Album/booklet01.jpg"))
    assert not is_sidecar_file(_file("Artist/Album/track.mp3"))
    assert not is_sidecar_file(_file("Artist/Album/movie.nfo"))


def test_album_signature_uses_album_folder_nfo_only() -> None:
    """A disc-subfolder album.nfo never contributes to the album's NFO signature (Kodi layout)."""
    index = SidecarIndex()
    index.record(_file("Artist/Album/Disc 1/album.nfo"))
    index.record(_file("Artist/Album/Disc 1/folder.jpg"))
    index.record_track_dir("Artist/Album/Disc 1")
    nfo_sig, img_sig = index.album_signatures("Artist/Album", set())
    assert nfo_sig == EMPTY  # album folder has no album.nfo of its own
    assert img_sig != EMPTY  # the disc image still counts towards artwork


def test_album_image_dirs_include_only_track_containing_discs() -> None:
    """A subfolder must actually hold tracks to contribute artwork (excludes a Scans/ folder)."""
    index = SidecarIndex()
    index.record(_file("Artist/Album/folder.jpg"))
    index.record(_file("Artist/Album/Disc 1/cover.jpg"))
    index.record(_file("Artist/Album/Scans/cover.jpg"))  # not a track folder
    index.record_track_dir("Artist/Album/Disc 1")
    dirs = index.album_image_dirs("Artist/Album", set())
    assert "Artist/Album" in dirs
    assert "Artist/Album/Disc 1" in dirs
    assert "Artist/Album/Scans" not in dirs


def test_album_image_dirs_exclude_nested_mapped_albums() -> None:
    """A track-containing child that is itself a mapped album is not treated as a disc folder."""
    index = SidecarIndex()
    index.record(_file("Compilation/folder.jpg"))
    index.record(_file("Compilation/Sub Album/cover.jpg"))
    index.record_track_dir("Compilation/Sub Album")
    dirs = index.album_image_dirs("Compilation", {"Compilation/Sub Album"})
    assert dirs == ["Compilation"]


def test_album_signature_changes_when_nfo_content_changes() -> None:
    """Editing album.nfo (new mtime) changes only the NFO signature."""
    old = SidecarIndex()
    old.record(_file("Artist/Album/album.nfo", checksum="1"))
    old.record(_file("Artist/Album/folder.jpg", checksum="9"))
    new = SidecarIndex()
    new.record(_file("Artist/Album/album.nfo", checksum="2"))
    new.record(_file("Artist/Album/folder.jpg", checksum="9"))
    old_nfo, old_img = old.album_signatures("Artist/Album", set())
    new_nfo, new_img = new.album_signatures("Artist/Album", set())
    assert old_nfo != new_nfo
    assert old_img == new_img


def test_reconcile_scalar_prefers_fresh_then_provenance() -> None:
    """Fresh value wins; a removed value clears only when it still matches our last contribution."""
    assert reconcile_scalar("stored", "fresh", "prev") == "fresh"
    assert reconcile_scalar("ours", None, "ours") is None
    assert reconcile_scalar("theaudiodb bio", None, "our old bio") == "theaudiodb bio"
    assert reconcile_scalar("stored", None, None) == "stored"


def test_reconcile_provenance_set_removes_only_our_contribution() -> None:
    """Removing one NFO value preserves values from tags or other providers."""
    stored = {"Rock", "Jazz", "Blues"}  # Rock+Jazz from NFO, Blues from another provider
    assert reconcile_provenance_set(stored, {"Rock"}, {"Rock", "Jazz"}) == {"Rock", "Blues"}
    assert reconcile_provenance_set(stored, set(), {"Rock", "Jazz"}) == {"Blues"}
    assert reconcile_provenance_set(stored, {"Pop"}, set()) == {"Rock", "Jazz", "Blues", "Pop"}


def test_reconcile_images_replaces_own_keeps_others() -> None:
    """Our images are replaced by the fresh set while other providers' images survive."""
    stored = [_image("theaudiodb", "art/remote.jpg"), _image("filesystem--1", "Album/old.jpg")]
    fresh = [_image("filesystem--1", "Album/folder.jpg?cs=2")]
    result = reconcile_images(stored, fresh, "filesystem--1")
    assert {img.path for img in result} == {"art/remote.jpg", "Album/folder.jpg?cs=2"}


def test_reconcile_images_keeps_embedded_art_when_no_folder_image() -> None:
    """Embedded audio-file cover art survives as a fallback when no folder image replaces it."""
    stored = [_image("filesystem--1", "Album/track01.mp3")]  # embedded album art
    # no folder image parsed now: the embedded fallback must remain
    assert [img.path for img in reconcile_images(stored, [], "filesystem--1")] == [
        "Album/track01.mp3"
    ]
    # a folder image takes over, so the embedded fallback is dropped in its favor
    fresh = [_image("filesystem--1", "Album/folder.jpg?cs=1")]
    assert [img.path for img in reconcile_images(stored, fresh, "filesystem--1")] == [
        "Album/folder.jpg?cs=1"
    ]


def test_folder_signature_detects_same_second_same_size_edit() -> None:
    """A local same-second, same-size replacement is detected via the nanosecond mtime token."""
    before = FileSystemItem(
        filename="album.nfo",
        relative_path="Artist/Album/album.nfo",
        absolute_path="/media/Artist/Album/album.nfo",
        is_dir=False,
        checksum="1700000000",
        file_size=200,
        mtime_ns=1700000000_100000000,
    )
    after = FileSystemItem(
        filename="album.nfo",
        relative_path="Artist/Album/album.nfo",
        absolute_path="/media/Artist/Album/album.nfo",
        is_dir=False,
        checksum="1700000000",  # identical whole-second mtime and size
        file_size=200,
        mtime_ns=1700000000_900000000,
    )
    assert get_folder_signature([before]) != get_folder_signature([after])


def test_nfo_root_dict_accepts_valid_and_rejects_invalid() -> None:
    """A well-formed NFO returns its root; malformed/scalar/wrong roots are ignored."""
    info = nfo_root_dict("<album><title>Kind of Blue</title></album>", "album", "a.nfo", LOG)
    assert info is not None
    assert info["title"] == "Kind of Blue"
    assert nfo_root_dict("<movie><title>x</title></movie>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<album/>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<album>just text</album>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<not-closed>", "album", "a.nfo", LOG) is None
