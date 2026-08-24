"""Tests for sidecar collection during traversal, image versioning, NFO reads, and CUE reparse."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.providers.filesystem_local import (
    _RERAISE_INVALID_NFO_TARGET,
    LocalFileSystemProvider,
)
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
from music_assistant.providers.filesystem_local.cue import make_cue_track_id
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    ScanErrors,
    SidecarIndex,
    SidecarInvalidError,
    SidecarReadError,
    strip_cache_buster,
)
from music_assistant.providers.webdav.provider import WebDAVFileSystemProvider

INSTANCE_ID = "filesystem_local--test"


def _file(relative_path: str, checksum: str = "1", mtime_ns: int | None = None) -> FileSystemItem:
    """Build a minimal file FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
        file_size=10,
        mtime_ns=mtime_ns,
    )


def _dir(relative_path: str) -> FileSystemItem:
    """Build a directory FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=True,
    )


def _provider() -> Any:
    """Create a bare music provider with a working folder-images cache mock."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.media_content_type = "music"
    provider.base_path = "/media"
    provider.cache = MagicMock()
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._active_sidecar_index = SidecarIndex()
    provider._sync_mapped_album_dirs = set()
    return provider


async def test_album_images_are_versioned_with_the_file_checksum() -> None:
    """Album folder images carry a ``?cs=`` suffix so replaced bytes bypass the image cache."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/folder.jpg", checksum="111"))
    images = await provider._get_local_images(
        "Artist/Album", extra_thumb_names=("album",), versioned=True
    )
    assert [img.path for img in images] == ["Artist/Album/folder.jpg?cs=111"]


async def test_album_image_url_uses_high_resolution_change_token() -> None:
    """The versioned image URL uses the nanosecond mtime so same-second edits bust the client cache."""
    provider = _provider()
    provider._active_sidecar_index.record(
        _file("Artist/Album/folder.jpg", checksum="1700000000", mtime_ns=1700000000_500000000)
    )
    images = await provider._get_local_images(
        "Artist/Album", extra_thumb_names=("album",), versioned=True
    )
    assert [img.path for img in images] == ["Artist/Album/folder.jpg?cs=1700000000500000000"]


def test_versioned_image_path_encodes_opaque_change_token() -> None:
    """A Base64 cloud change token with ``/ + =`` is encoded so the suffix stays strippable."""
    token = "aB3/xY+z=="  # OneDrive/cloud-style etag
    versioned = LocalFileSystemProvider._versioned_image_path("Artist/Album/folder.jpg", token)
    suffix = versioned.split("?cs=", 1)[1]
    assert "/" not in suffix  # no raw separators that would break stripping
    assert "?" not in suffix
    assert strip_cache_buster(versioned) == "Artist/Album/folder.jpg"


async def test_album_images_are_collected_in_deterministic_order() -> None:
    """Disc folders sort deterministically so the primary artwork is stable across runs."""
    provider = _provider()
    index = provider._active_sidecar_index
    index.record(_file("Artist/Album/folder.jpg", checksum="1"))
    # record discs out of order; collection must not depend on insertion/set order
    for disc in ("Disc 2", "Disc 1"):
        index.record_track_dir(f"Artist/Album/{disc}")
        index.record(_file(f"Artist/Album/{disc}/folder.jpg", checksum="1"))
    provider._sync_mapped_album_dirs = {"Artist/Album"}

    first = [img.path for img in await provider._collect_album_images("Artist/Album")]
    index._track_children = None  # bust the memoized order and recollect
    second = [img.path for img in await provider._collect_album_images("Artist/Album")]
    assert first == second
    # album folder first, then Disc 1 before Disc 2
    assert first == [
        "Artist/Album/folder.jpg?cs=1",
        "Artist/Album/Disc 1/folder.jpg?cs=1",
        "Artist/Album/Disc 2/folder.jpg?cs=1",
    ]


async def test_malformed_album_nfo_raises_invalid_and_leaves_album_untouched() -> None:
    """A scalar-root album.nfo raises SidecarInvalidError without mutating the album."""
    provider = _provider()
    provider._read_file = AsyncMock(return_value=b"<album>just text</album>")
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert album.name == "Keep"


async def test_invalid_field_album_nfo_is_atomic() -> None:
    """A late invalid field (bad year) aborts the whole NFO apply, leaving no partial mutation."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=(
            b"<album><title>New Title</title><review>bio</review>"
            b"<genre>Rock</genre><year>not-a-year</year></album>"
        )
    )
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    # nothing from the NFO was applied because a later field was invalid
    assert album.name == "Keep"
    assert album.metadata.description is None
    assert not album.metadata.genres


async def test_non_scalar_album_nfo_field_is_rejected() -> None:
    """A repeated element (a list value from xmltodict) is rejected without mutating the album."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=(
            b"<album><title>New Title</title><sortname>A</sortname><sortname>B</sortname></album>"
        )
    )
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    # the non-scalar sortname aborts the apply, so the valid title before it is not written either
    assert album.name == "Keep"


async def test_non_scalar_artist_nfo_field_is_rejected() -> None:
    """A repeated artist element is rejected without mutating the artist."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=(
            b"<artist><title>New</title><biography>a</biography><biography>b</biography></artist>"
        )
    )
    artist = Artist(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_artist_nfo(artist, _file("Artist/artist.nfo"))
    assert artist.name == "Keep"
    assert artist.metadata.description is None


async def test_repeated_genre_nfo_is_accepted() -> None:
    """Multiple <genre> tags parse to a list and are imported as separate genres."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=b"<album><title>X</title><genre>Rock</genre><genre>Pop</genre></album>"
    )
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert album.metadata.genres == {"Rock", "Pop"}


async def test_nested_genre_nfo_field_is_rejected() -> None:
    """A nested <genre> element (a mapping from xmltodict) is rejected as malformed."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=b"<album><title>X</title><genre><name>Rock</name></genre></album>"
    )
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert album.name == "Keep"
    assert not album.metadata.genres


async def test_repeated_mbid_nfo_field_is_rejected() -> None:
    """A repeated MusicBrainz id is rejected as malformed, not silently dropped as absent."""
    provider = _provider()
    provider._read_file = AsyncMock(
        return_value=(
            b"<album><title>X</title>"
            b"<musicbrainzalbumid>id-1</musicbrainzalbumid>"
            b"<musicbrainzalbumid>id-2</musicbrainzalbumid></album>"
        )
    )
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarInvalidError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert album.name == "Keep"


async def test_reraise_marker_is_task_local() -> None:
    """A parse in a sibling task never observes another task's invalid-NFO propagation marker."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._active_sidecar_index = None
    provider.cache.get = AsyncMock(return_value=None)
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._read_file = AsyncMock(return_value=b"<artist>just text</artist>")  # malformed
    provider._folder_sidecars = AsyncMock(return_value=[_file("Artist/artist.nfo")])

    async def _sibling() -> Any:
        # this task never set the marker, so the malformed NFO must degrade instead of raising
        return await provider._parse_artist("A", artist_path="Artist")

    # the sibling copies the current (marker-free) context at creation time
    sibling = asyncio.create_task(_sibling())
    token = _RERAISE_INVALID_NFO_TARGET.set(("Artist", "artist"))
    try:
        artist = await sibling
    finally:
        _RERAISE_INVALID_NFO_TARGET.reset(token)
    assert artist.name == "A"  # unaffected by our task's marker


async def test_transient_nfo_read_failure_raises_sidecar_read_error() -> None:
    """An IO/provider failure reading the NFO raises rather than looking like a removed NFO."""
    provider = _provider()
    provider._read_file = AsyncMock(side_effect=ProviderUnavailableError("network down"))
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarReadError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))


async def test_folder_sidecars_defers_on_transient_listing_failure_during_sync() -> None:
    """A transient listing failure during sync raises SidecarReadError so the item is deferred."""
    provider = _provider()
    provider._active_sidecar_index = None
    provider.sync_running = True
    provider._scandir = AsyncMock(side_effect=ProviderUnavailableError("cloud down"))
    with pytest.raises(SidecarReadError):
        await provider._folder_sidecars("Artist/Album")


async def test_folder_sidecars_degrades_on_transient_listing_failure_on_demand() -> None:
    """Off-sync a transient listing failure degrades to no sidecars rather than raising."""
    provider = _provider()
    provider._active_sidecar_index = None
    provider.sync_running = False
    provider._scandir = AsyncMock(side_effect=ProviderUnavailableError("cloud down"))
    assert await provider._folder_sidecars("Artist/Album") == []


async def test_folder_sidecars_returns_empty_for_missing_folder_during_sync() -> None:
    """A genuinely missing folder yields no sidecars, not a deferral, even during sync."""
    provider = _provider()
    provider._active_sidecar_index = None
    provider.sync_running = True
    provider._scandir = AsyncMock(side_effect=MediaNotFoundError("no such folder"))
    assert await provider._folder_sidecars("Artist/Album") == []


async def test_parse_artist_propagates_read_error_during_incomplete_sync() -> None:
    """During a sync with an unpublished index a failed NFO read must raise, not tag-only degrade."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._active_sidecar_index = None  # incomplete scan: index intentionally unpublished
    provider.cache.get = AsyncMock(return_value=None)
    provider._folder_sidecars = AsyncMock(return_value=[_file("Artist/artist.nfo")])
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._apply_artist_nfo = AsyncMock(side_effect=SidecarReadError("nfo unreadable"))

    provider.sync_running = True
    with pytest.raises(SidecarReadError):
        await provider._parse_artist("Artist", artist_path="Artist")

    # outside a sync there is no baseline to protect, so it degrades to a tag-only artist
    provider.sync_running = False
    artist = await provider._parse_artist("Artist", artist_path="Artist")
    assert artist.name == "Artist"


async def test_parse_artist_imports_tag_only_when_nfo_malformed() -> None:
    """A first import with a malformed artist.nfo degrades to a tag-only artist, never raising."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._active_sidecar_index = None
    provider.cache.get = AsyncMock(return_value=None)
    provider._folder_sidecars = AsyncMock(return_value=[_file("Artist/artist.nfo")])
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._read_file = AsyncMock(return_value=b"<artist>just text</artist>")  # malformed

    # malformed NFO must not raise even mid-sync: a new artist has no baseline to protect
    provider.sync_running = True
    artist = await provider._parse_artist("Tag Artist", artist_path="Artist")
    assert artist.name == "Tag Artist"
    assert artist.metadata.description is None


async def test_invalid_nfo_propagation_is_scoped_to_the_refreshed_item() -> None:
    """While refreshing one item, an unrelated item's malformed NFO degrades and never propagates."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._active_sidecar_index = None
    provider.cache.get = AsyncMock(return_value=None)
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._read_file = AsyncMock(return_value=b"<artist>just text</artist>")  # malformed
    # a refresh of the "Artist" artist is in progress
    token = _RERAISE_INVALID_NFO_TARGET.set(("Artist", "artist"))
    try:
        # the target item's malformed NFO propagates so its refresh keeps prior metadata
        provider._folder_sidecars = AsyncMock(return_value=[_file("Artist/artist.nfo")])
        with pytest.raises(SidecarInvalidError):
            await provider._parse_artist("A", artist_path="Artist")

        # a different item parsed in the same reparse degrades to tag-only instead of blocking it
        provider._folder_sidecars = AsyncMock(return_value=[_file("Other/artist.nfo")])
        other = await provider._parse_artist("B", artist_path="Other")
        assert other.name == "B"
    finally:
        _RERAISE_INVALID_NFO_TARGET.reset(token)


async def test_invalid_artist_nfo_does_not_block_album_refresh_in_same_folder() -> None:
    """A malformed artist.nfo must not defer a valid album refresh when both map to one folder."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._active_sidecar_index = None
    provider.cache.get = AsyncMock(return_value=None)
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider._read_file = AsyncMock(return_value=b"<artist>just text</artist>")  # malformed
    # an album refresh for folder "Music" is in progress; its album artist maps to the same folder
    token = _RERAISE_INVALID_NFO_TARGET.set(("Music", "album"))
    try:
        provider._folder_sidecars = AsyncMock(return_value=[_file("Music/artist.nfo")])
        # resolving the album artist parses the malformed artist.nfo, but the target is the album,
        # so it degrades to tag-only and never blocks the album refresh
        artist = await provider._parse_artist("Various", artist_path="Music")
        assert artist.name == "Various"
    finally:
        _RERAISE_INVALID_NFO_TARGET.reset(token)


async def test_local_walk_collects_sidecars_track_dirs_and_skips_strays() -> None:
    """The local walk records sidecars + track folders and drops stray images/nfo."""
    provider = _provider()
    provider.config.get_value = MagicMock(
        side_effect=lambda key: False if key == CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key else None
    )
    provider._classify_scan_item = MagicMock()
    index = SidecarIndex()
    walk_items = [
        _file("Artist/Album/track.mp3"),
        _file("Artist/Album/Disc 1/track.mp3"),
        _file("Artist/Album/album.nfo"),
        _file("Artist/Album/folder.jpg"),
        _file("Random/IMG_1234.jpg"),  # stray
        _file("Random/movie.nfo"),  # stray
    ]
    with patch(
        "music_assistant.providers.filesystem_local.recursive_iter", return_value=iter(walk_items)
    ):
        await provider._enumerate_files_for_sync(
            file_checksums={},
            cue_file_checksums={},
            cur_filenames=set(),
            items_to_process=[],
            unchanged_cue_items=[],
            cue_stems=set(),
            scan_errors=ScanErrors(),
            sidecar_index=index,
        )
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert index.track_dirs == {"Artist/Album", "Artist/Album/Disc 1"}
    assert index.files("Random") == []
    classified = [
        call.args[0].relative_path for call in provider._classify_scan_item.call_args_list
    ]
    assert classified == ["Artist/Album/track.mp3", "Artist/Album/Disc 1/track.mp3"]


async def test_virtual_walk_reuses_directory_listings_without_per_track_probes() -> None:
    """A WebDAV-style walk records sidecars + track folders from listings, one scan per dir."""
    with patch.object(WebDAVFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider: Any = WebDAVFileSystemProvider.__new__(WebDAVFileSystemProvider)
    provider.logger = MagicMock()
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=False)
    provider._classify_scan_item = MagicMock()
    listings = {
        "": [_dir("Artist")],
        "Artist": [_dir("Artist/Album")],
        "Artist/Album": [
            _file("Artist/Album/track.mp3"),
            _file("Artist/Album/album.nfo"),
            _file("Artist/Album/folder.jpg"),
        ],
    }
    scandir_calls: list[str] = []

    async def _scandir(path: str) -> list[FileSystemItem]:
        scandir_calls.append(path)
        return listings[path]

    provider._scandir = _scandir
    index = SidecarIndex()
    await provider._enumerate_files_for_sync(
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=set(),
        items_to_process=[],
        unchanged_cue_items=[],
        cue_stems=set(),
        scan_errors=ScanErrors(),
        sidecar_index=index,
    )
    assert scandir_calls == ["", "Artist", "Artist/Album"]
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert index.track_dirs == {"Artist/Album"}


async def test_reparse_album_from_cue_track_id() -> None:
    """A CUE-only album is reparsed via the CUE handler, not treated as unreadable."""
    provider = _provider()
    cue_id = make_cue_track_id("Artist/Album/album.cue", 1)
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id=cue_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID
            )
        },
    )
    provider.mass.music.albums.tracks = AsyncMock(return_value=[track])
    provider.resolve = AsyncMock(return_value=_file("Artist/Album/album.cue"))
    cue_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Cue Album", provider_mappings=set()
    )
    cue_track = MagicMock(spec=Track)
    cue_track.album = cue_album
    provider._cue = MagicMock()
    provider._cue.parse_tracks = AsyncMock(return_value=[cue_track])

    result = await provider._reparse_album_from_track("5", "Artist/Album")
    assert result is cue_album
    provider._cue.parse_tracks.assert_awaited_once()


async def test_reparse_artist_from_cue_track_id() -> None:
    """A CUE-only artist is rebuilt from the CUE tracks, matched by its mapping directory."""
    provider = _provider()
    cue_id = make_cue_track_id("Artist/Album/album.cue", 1)
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id=cue_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID
            )
        },
    )
    provider.mass.music.artists.tracks = AsyncMock(return_value=[track])
    provider.resolve = AsyncMock(return_value=_file("Artist/Album/album.cue"))
    cue_artist = Artist(
        item_id="Artist", provider=INSTANCE_ID, name="Cue Artist", provider_mappings=set()
    )
    cue_track = MagicMock(spec=Track)
    cue_track.artists = [cue_artist]
    cue_track.album = None
    provider._cue = MagicMock()
    provider._cue.parse_tracks = AsyncMock(return_value=[cue_track])

    result = await provider._reparse_artist_from_track("9", "Artist")
    assert result is cue_artist


async def test_reparse_artist_falls_back_to_album_tracks_for_album_only_artist() -> None:
    """An album-only ALBUMARTIST with no track-artist relationship is rebuilt via its albums."""
    provider = _provider()
    provider.mass.music.artists.tracks = AsyncMock(return_value=[])  # no track-artist rows at all
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id="Artist/Album/track.mp3",
                provider_domain="filesystem_local",
                provider_instance=INSTANCE_ID,
            )
        },
    )
    provider.mass.music.artists.get_library_artist_album_tracks = AsyncMock(return_value=[track])
    provider.resolve = AsyncMock(return_value=_file("Artist/Album/track.mp3"))
    album_artist = Artist(
        item_id="Artist", provider=INSTANCE_ID, name="Album Artist", provider_mappings=set()
    )
    parsed_track = MagicMock(spec=Track)
    parsed_track.artists = [album_artist]
    parsed_track.album = None
    provider._parse_track = AsyncMock(return_value=parsed_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider._reparse_artist_from_track("9", "Artist")

    assert result is album_artist
    provider.mass.music.artists.get_library_artist_album_tracks.assert_awaited_once_with(
        "9", provider_filter=INSTANCE_ID
    )


async def test_reparse_artist_album_fallback_requires_exact_mapping_path() -> None:
    """The album fallback still only matches a track living under the exact artist path."""
    provider = _provider()
    provider.mass.music.artists.tracks = AsyncMock(return_value=[])
    # this album track belongs to a different artist's mapping directory entirely
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id="Other Artist/Album/track.mp3",
                provider_domain="filesystem_local",
                provider_instance=INSTANCE_ID,
            )
        },
    )
    provider.mass.music.artists.get_library_artist_album_tracks = AsyncMock(return_value=[track])

    result = await provider._reparse_artist_from_track("9", "Artist")

    assert result is None


async def test_reparse_artist_album_fallback_propagates_transient_read_failure() -> None:
    """A representative found only through the album fallback still surfaces read failures."""
    provider = _provider()
    provider.mass.music.artists.tracks = AsyncMock(return_value=[])
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id="Artist/Album/track.mp3",
                provider_domain="filesystem_local",
                provider_instance=INSTANCE_ID,
            )
        },
    )
    provider.mass.music.artists.get_library_artist_album_tracks = AsyncMock(return_value=[track])
    provider.resolve = AsyncMock(side_effect=MediaNotFoundError("gone"))

    with pytest.raises(SidecarReadError):
        await provider._reparse_artist_from_track("9", "Artist")


async def test_music_sync_skips_sidecars_when_track_sync_disabled() -> None:
    """A playlist-only music sync must not build sidecar state or refresh albums/artists."""
    config_values = {
        CONF_ENTRY_CONTENT_TYPE.key: "music",
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: False,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: True,
    }
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider: Any = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))
    provider.media_content_type = "music"
    provider.sync_running = False
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    provider._process_deletions = AsyncMock()
    provider._process_orphaned_albums_and_artists = AsyncMock()
    provider._set_available = MagicMock()
    provider._query_mapping_details = AsyncMock(return_value=({}, {}))
    provider._refresh_changed_sidecars = AsyncMock()

    captured: dict[str, Any] = {}

    async def _enumerate(**kwargs: Any) -> None:
        captured["sidecar_index"] = kwargs["sidecar_index"]

    provider._enumerate_files_for_sync = _enumerate
    await provider.sync_library(MediaType.TRACK)

    assert captured["sidecar_index"] is None
    provider._refresh_changed_sidecars.assert_not_awaited()
    provider._query_mapping_details.assert_not_awaited()


async def test_incomplete_scan_does_not_publish_index_or_reconcile() -> None:
    """An incomplete scan keeps the index unpublished so changed tracks fall back to folder reads."""
    config_values = {
        CONF_ENTRY_CONTENT_TYPE.key: "music",
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: True,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: False,
    }
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider: Any = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))
    provider.media_content_type = "music"
    provider.sync_running = False
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    provider._process_deletions = AsyncMock()
    provider._process_orphaned_albums_and_artists = AsyncMock()
    provider._set_available = MagicMock()
    provider._query_mapping_details = AsyncMock(return_value=({}, {}))
    provider._refresh_changed_sidecars = AsyncMock()
    provider._cue = MagicMock()

    published: dict[str, Any] = {}

    async def _enumerate(**kwargs: Any) -> None:
        # collection still happens (index passed in) but a folder failed to read
        published["index_arg"] = kwargs["sidecar_index"]
        kwargs["scan_errors"].failed_dirs = 1

    provider._enumerate_files_for_sync = _enumerate
    with patch("music_assistant.providers.filesystem_local.report_current_task_failure"):
        await provider.sync_library(MediaType.TRACK)

    assert published["index_arg"] is not None  # sidecars were still collected during the walk
    assert provider._active_sidecar_index is None  # but never published for indexed parsing
    provider._refresh_changed_sidecars.assert_not_awaited()
    provider._query_mapping_details.assert_not_awaited()


async def test_changed_track_with_unreadable_nfo_retains_existing_item() -> None:
    """A transient NFO failure while parsing a changed track keeps the library item untouched."""
    provider = _provider()
    provider._sync_tracks = True
    provider._parse_track = AsyncMock(side_effect=SidecarReadError("nfo unreadable"))
    provider.mass.music.tracks.add_item_to_library = AsyncMock()
    provider.mass.metadata.invalidate_image_cache = AsyncMock()
    provider._versioned_image_path = MagicMock(return_value="Artist/Album/track.mp3?cs=old")
    cur_filenames: set[str] = set()
    track = _file("Artist/Album/track.mp3")
    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider._process_item_async(track, "old", cur_filenames, set(), set())

    assert result is False
    provider.mass.music.tracks.add_item_to_library.assert_not_awaited()  # existing item untouched
    assert "Artist/Album/track.mp3" in cur_filenames  # kept so deletion never removes it
