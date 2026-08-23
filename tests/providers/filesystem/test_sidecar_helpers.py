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
    reconcile_scalar,
)

LOG = logging.getLogger("test")


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
    assert is_sidecar_file(_file("Artist/artist.nfo"))
    assert is_sidecar_file(_file("Artist/Album/folder.jpg"))
    assert is_sidecar_file(_file("Artist/Album/cover.png"))
    assert is_sidecar_file(_file("Artist/Album/fanart.jpg"))
    # not recognized: a random image name, a track, an unrelated nfo
    assert not is_sidecar_file(_file("Artist/Album/booklet01.jpg"))
    assert not is_sidecar_file(_file("Artist/Album/track.mp3"))
    assert not is_sidecar_file(_file("Artist/Album/movie.nfo"))


def test_sidecar_index_records_only_sidecars() -> None:
    """record() keeps recognized sidecars grouped by directory and rejects other files."""
    index = SidecarIndex()
    assert index.record(_file("Artist/Album/album.nfo"))
    assert index.record(_file("Artist/Album/folder.jpg"))
    assert not index.record(_file("Artist/Album/track.mp3"))
    assert not index.record(_file("Artist/Album/booklet.jpg"))
    assert {item.filename for item in index.files("Artist/Album")} == {"album.nfo", "folder.jpg"}


def test_album_signature_uses_album_folder_nfo_only() -> None:
    """A disc-subfolder album.nfo never contributes to the album's NFO signature (Kodi layout)."""
    index = SidecarIndex()
    index.record(_file("Artist/Album/Disc 1/album.nfo"))
    index.record(_file("Artist/Album/Disc 1/folder.jpg"))
    nfo_sig, img_sig = index.album_signatures("Artist/Album")
    # album folder itself has no album.nfo -> empty nfo signature
    assert nfo_sig == get_folder_signature([])
    # but the disc image still counts towards the album artwork signature
    assert img_sig != get_folder_signature([])


def test_album_signature_includes_album_and_disc_images() -> None:
    """The album image signature spans the album folder and its immediate disc subfolders."""
    index = SidecarIndex()
    index.record(_file("Artist/Album/album.nfo"))
    index.record(_file("Artist/Album/folder.jpg"))
    index.record(_file("Artist/Album/Disc 1/cover.jpg"))
    nfo_sig, img_sig = index.album_signatures("Artist/Album")
    assert nfo_sig != get_folder_signature([])
    without_disc = get_folder_signature([_file("Artist/Album/folder.jpg")])
    assert img_sig != without_disc  # the disc image changed the signature


def test_album_signature_changes_when_nfo_content_changes() -> None:
    """Editing album.nfo (new mtime/size) changes only the NFO signature."""
    old = SidecarIndex()
    old.record(_file("Artist/Album/album.nfo", checksum="1"))
    old.record(_file("Artist/Album/folder.jpg", checksum="9"))
    new = SidecarIndex()
    new.record(_file("Artist/Album/album.nfo", checksum="2"))
    new.record(_file("Artist/Album/folder.jpg", checksum="9"))
    assert old.album_signatures("Artist/Album")[0] != new.album_signatures("Artist/Album")[0]
    assert old.album_signatures("Artist/Album")[1] == new.album_signatures("Artist/Album")[1]


def test_reconcile_scalar_prefers_fresh_then_provenance() -> None:
    """Fresh value wins; a removed value clears only when it still matches our last contribution."""
    # fresh value present -> used
    assert reconcile_scalar("stored", "fresh", "prev") == "fresh"
    # removed and still ours -> cleared
    assert reconcile_scalar("ours", None, "ours") is None
    # removed but another provider replaced it -> kept
    assert reconcile_scalar("theaudiodb bio", None, "our old bio") == "theaudiodb bio"
    # removed with no known baseline -> kept (conservative)
    assert reconcile_scalar("stored", None, None) == "stored"


def test_reconcile_images_replaces_own_keeps_others() -> None:
    """Our images are replaced by the fresh set while other providers' images survive."""
    stored = [_image("theaudiodb", "art/remote.jpg"), _image("filesystem--1", "Album/old.jpg")]
    fresh = [_image("filesystem--1", "Album/folder.jpg?cs=2")]
    result = reconcile_images(stored, fresh, "filesystem--1")
    paths = {img.path for img in result}
    assert paths == {"art/remote.jpg", "Album/folder.jpg?cs=2"}


def test_reconcile_images_removal_drops_our_images() -> None:
    """When the fresh set is empty, our images disappear but other providers' remain."""
    stored = [_image("theaudiodb", "art/remote.jpg"), _image("filesystem--1", "Album/old.jpg")]
    result = reconcile_images(stored, [], "filesystem--1")
    assert [img.path for img in result] == ["art/remote.jpg"]


def test_nfo_root_dict_accepts_valid_album() -> None:
    """A well-formed album NFO returns its root mapping."""
    info = nfo_root_dict("<album><title>Kind of Blue</title></album>", "album", "a.nfo", LOG)
    assert info is not None
    assert info["title"] == "Kind of Blue"


def test_nfo_root_dict_rejects_wrong_or_scalar_roots() -> None:
    """Malformed, empty, scalar and wrong-root NFO files are ignored rather than raising."""
    assert nfo_root_dict("<movie><title>x</title></movie>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<album/>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<album>just text</album>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("<not-closed>", "album", "a.nfo", LOG) is None
    assert nfo_root_dict("", "album", "a.nfo", LOG) is None
