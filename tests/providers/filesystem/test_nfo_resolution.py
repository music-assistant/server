"""Tests for the bounded, validated NFO-based folder resolution fallback."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Album

from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.filesystem_local import _ONDEMAND_NFO_ITEMS, LocalFileSystemProvider
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

INSTANCE_ID = "filesystem_local--test"
ALBUM_MBID = "11111111-1111-1111-1111-111111111111"
OTHER_MBID = "22222222-2222-2222-2222-222222222222"
ARTIST_MBID = "33333333-3333-3333-3333-333333333333"


def _provider() -> Any:
    """Create a bare provider with a mocked cache, outside a sync (on-demand resolution)."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.media_content_type = "music"
    provider.cache = MagicMock()
    provider.sync_running = False
    provider._sync_nfo_by_dir = {}
    provider._sync_nfo_index_ready = False
    provider._cue = MagicMock()
    return provider


def _item(relative_path: str) -> FileSystemItem:
    """Build a minimal FileSystemItem for a resolved NFO file."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum="1",
    )


def _mock_single_file(provider: Any, path: str, data: bytes) -> None:
    """Make exactly one NFO file exist, discoverable via a folder listing (on demand)."""
    folder, _sep, _name = path.rpartition("/")
    item = _item(path)

    async def _scandir(scan_folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        return [item] if scan_folder == folder else []

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(return_value=data)


def _async_iter(items: list[Any]) -> Any:
    """Build an async-generator stand-in for a controller's `iter_library_items`."""

    async def _iter(*_args: object, **_kwargs: object) -> Any:
        for item in items:
            yield item

    return _iter


def _tags(
    album: str | None = "My Album",
    album_id: str | None = None,
    rg_id: str | None = None,
    album_artist_ids: tuple[str, ...] = (),
) -> Any:
    """Build minimal audio tags for album resolution."""
    return MagicMock(
        album=album,
        filename="track.flac",
        musicbrainz_albumid=album_id,
        musicbrainz_releasegroupid=rg_id,
        musicbrainz_albumartistids=album_artist_ids,
    )


# --- album.nfo resolution ----------------------------------------------------------------


async def test_album_resolves_via_parent_nfo_with_matching_mbid() -> None:
    """A recognized disc subfolder's own NFO is never tried; only the parent's matching NFO is."""
    provider = _provider()
    track_dir = "Artist/Album/Disc 1"
    _mock_single_file(
        provider,
        "Artist/Album/album.nfo",
        f"<album><title>Other Title</title>"
        f"<musicbrainzalbumid>{ALBUM_MBID}</musicbrainzalbumid></album>".encode(),
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags(album_id=ALBUM_MBID))
    assert result is not None
    album_dir, nfo_item, root = result
    assert album_dir == "Artist/Album"
    assert nfo_item.relative_path == "Artist/Album/album.nfo"
    assert root["title"] == "Other Title"


async def test_album_resolves_via_title_match_when_no_mbid() -> None:
    """A candidate's album.nfo title, matched against the track's album tag, is sufficient."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"  # a catalogue-number folder that folder matching cannot resolve
    _mock_single_file(
        provider, "Artist/CAT-1234/album.nfo", b"<album><title>My Album</title></album>"
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is not None
    assert result[0] == track_dir


async def test_album_title_match_ignores_edition_suffix_on_both_sides() -> None:
    """An NFO title's own edition suffix is stripped the same way the tag's album name was."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider,
        "Artist/CAT-1234/album.nfo",
        b"<album><title>My Album (Deluxe Edition)</title></album>",
    )
    result = await provider._resolve_album_dir_via_nfo(
        track_dir, _tags(album="My Album (Deluxe Edition)")
    )
    assert result is not None
    assert result[0] == track_dir


async def test_album_conflicting_mbid_is_rejected_even_with_matching_title() -> None:
    """A conflicting MusicBrainz album id is rejected outright, never falling back to title."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider,
        "Artist/CAT-1234/album.nfo",
        f"<album><title>My Album</title>"
        f"<musicbrainzalbumid>{OTHER_MBID}</musicbrainzalbumid></album>".encode(),
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags(album_id=ALBUM_MBID))
    assert result is None


async def test_album_matching_id_still_rejected_on_conflicting_second_id() -> None:
    """A matching album id does not short-circuit a conflicting release-group id."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    other_rg_mbid = "44444444-4444-4444-4444-444444444444"
    _mock_single_file(
        provider,
        "Artist/CAT-1234/album.nfo",
        f"<album><title>My Album</title>"
        f"<musicbrainzalbumid>{ALBUM_MBID}</musicbrainzalbumid>"
        f"<musicbrainzreleasegroupid>{other_rg_mbid}</musicbrainzreleasegroupid></album>".encode(),
    )
    result = await provider._resolve_album_dir_via_nfo(
        track_dir, _tags(album_id=ALBUM_MBID, rg_id="55555555-5555-5555-5555-555555555555")
    )
    assert result is None


async def test_album_falls_through_to_track_dir_when_parent_has_no_match() -> None:
    """The track directory itself is tried when the parent has no (or no matching) album.nfo."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider, "Artist/CAT-1234/album.nfo", b"<album><title>My Album</title></album>"
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is not None
    assert result[0] == "Artist/CAT-1234"


async def test_album_own_directory_nfo_takes_precedence_over_parent() -> None:
    """
    The track's own directory is tried before its parent, being the nearer, more specific one.

    Otherwise a same-title album.nfo one level up (e.g. a stray leftover from a prior, flatter
    single-album layout) could outrank the track's own, definitively correct album.nfo.
    """
    provider = _provider()
    track_dir = "Artist/CAT-1234"

    async def _scandir(scan_folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        if scan_folder in ("Artist", "Artist/CAT-1234"):
            return [_item(f"{scan_folder}/album.nfo")]
        return []

    async def _read_file(path: str) -> bytes:
        return (
            b"<album><title>My Album</title></album>"
            if path == "Artist/CAT-1234/album.nfo"
            else b"<album><title>My Album</title><year>1999</year></album>"
        )

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(side_effect=_read_file)

    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is not None
    assert result[0] == "Artist/CAT-1234"


async def test_album_matches_nfo_regardless_of_filename_case() -> None:
    """A case-insensitive filesystem's ALBUM.NFO resolves the same as a lowercase album.nfo."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider, "Artist/CAT-1234/ALBUM.NFO", b"<album><title>My Album</title></album>"
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is not None
    assert result[0] == track_dir


async def test_album_malformed_nfo_stays_unresolved() -> None:
    """Malformed (unparsable) NFO content leaves the album synthetic, not a failure."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(provider, "Artist/CAT-1234/album.nfo", b"not xml at all <<<")
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is None


async def test_album_invalid_field_leaves_item_unresolved() -> None:
    """A matching title with a later invalid field (bad year) does not resolve the folder."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider,
        "Artist/CAT-1234/album.nfo",
        b"<album><title>My Album</title><year>not-a-year</year></album>",
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags())
    assert result is None


async def test_album_no_candidate_nfo_returns_none() -> None:
    """No album.nfo at either candidate directory resolves to nothing, not an error."""
    provider = _provider()
    provider._scandir = AsyncMock(return_value=[])
    result = await provider._resolve_album_dir_via_nfo("Artist/CAT-1234", _tags())
    assert result is None


async def test_album_resolution_never_uses_provider_root_as_identity() -> None:
    """
    A valid album.nfo living at the provider's own root is never trusted as identity.

    Like the normal, non-NFO folder match, the provider root cannot identify one specific
    album out of the many it may contain, even when a track happens to sit directly in it.
    """
    provider = _provider()
    track_dir = ""  # the track file lives directly in the provider's configured root
    _mock_single_file(
        provider,
        "album.nfo",
        f"<album><title>My Album</title>"
        f"<musicbrainzalbumid>{ALBUM_MBID}</musicbrainzalbumid></album>".encode(),
    )
    result = await provider._resolve_album_dir_via_nfo(track_dir, _tags(album_id=ALBUM_MBID))
    assert result is None


async def test_album_transient_read_failure_propagates() -> None:
    """A genuine read failure (not malformed content) propagates so the sync can defer/retry."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(provider, "Artist/CAT-1234/album.nfo", b"irrelevant")
    provider._read_file = AsyncMock(side_effect=OSError("network hiccup"))
    with pytest.raises(OSError, match="network hiccup"):
        await provider._resolve_album_dir_via_nfo(track_dir, _tags())


async def test_nfo_listing_bypasses_cache_during_a_forced_refresh() -> None:
    """
    A manual "Refresh item" must see an NFO just added, not a stale cloud directory listing.

    `_scandir`'s `use_cache` is a cloud-backed provider's own separate, short-lived listing
    cache (unrelated to the core cache controller's `self.cache`); it must be bypassed the same
    way the core cache is, via the same `BYPASS_CACHE` context, or an NFO added to disk right
    before a refresh could still be missed for up to that cache's own TTL.
    """
    provider = _provider()
    provider._scandir = AsyncMock(return_value=[])

    token = BYPASS_CACHE.set(True)
    try:
        await provider._list_nfo_candidates("Artist/Album")
    finally:
        BYPASS_CACHE.reset(token)

    provider._scandir.assert_awaited_once_with("Artist/Album", use_cache=False)


async def test_nfo_listing_uses_cache_outside_a_forced_refresh() -> None:
    """Outside an explicit refresh, the cloud provider's own listing cache is left in play."""
    provider = _provider()
    provider._scandir = AsyncMock(return_value=[])

    await provider._list_nfo_candidates("Artist/Album")

    provider._scandir.assert_awaited_once_with("Artist/Album", use_cache=True)


# --- artist.nfo resolution ---------------------------------------------------------------


async def test_artist_resolves_via_ancestor_nfo_by_name() -> None:
    """The nearest ancestor's artist.nfo, matched by name, resolves the artist's folder."""
    provider = _provider()
    album_dir = "Music/The Artist/Album"
    _mock_single_file(
        provider, "Music/The Artist/artist.nfo", b"<artist><title>The Artist</title></artist>"
    )
    result = await provider._resolve_artist_dir_via_nfo(album_dir, "The Artist", None)
    assert result is not None
    assert result[0] == "Music/The Artist"


async def test_artist_resolves_via_ancestor_nfo_by_mbid() -> None:
    """A matching MusicBrainz artist id resolves even when the name in the NFO differs."""
    provider = _provider()
    album_dir = "Music/Weird Folder Name/Album"
    _mock_single_file(
        provider,
        "Music/Weird Folder Name/artist.nfo",
        f"<artist><title>Some Other Name</title>"
        f"<musicbrainzartistid>{ARTIST_MBID}</musicbrainzartistid></artist>".encode(),
    )
    result = await provider._resolve_artist_dir_via_nfo(album_dir, "The Artist", ARTIST_MBID)
    assert result is not None
    assert result[0] == "Music/Weird Folder Name"


async def test_artist_marker_only_nfo_is_never_accepted() -> None:
    """An artist.nfo with no id or name/title is never trusted as identity (no marker mode)."""
    provider = _provider()
    album_dir = "Music/Weird Folder Name/Album"
    _mock_single_file(
        provider, "Music/Weird Folder Name/artist.nfo", b"<artist><genre>Rock</genre></artist>"
    )
    result = await provider._resolve_artist_dir_via_nfo(album_dir, "The Artist", None)
    assert result is None


async def test_artist_resolution_bounded_to_three_ancestor_levels() -> None:
    """A matching artist.nfo four levels up is never found (mirrors the normal lookup's bound)."""
    provider = _provider()
    album_dir = "A/B/C/D/Album"
    _mock_single_file(provider, "A/artist.nfo", b"<artist><title>The Artist</title></artist>")
    result = await provider._resolve_artist_dir_via_nfo(album_dir, "The Artist", None)
    assert result is None


async def test_artist_no_matching_ancestor_returns_none() -> None:
    """No artist.nfo anywhere within the bound resolves to nothing, not an error."""
    provider = _provider()
    provider._scandir = AsyncMock(return_value=[])
    result = await provider._resolve_artist_dir_via_nfo("Music/Artist/Album", "The Artist", None)
    assert result is None


async def test_artist_resolution_never_walks_into_provider_root() -> None:
    """
    A valid artist.nfo living at the provider's own root is never trusted as identity.

    Like the normal, non-NFO folder match, the provider root cannot identify one specific
    artist out of the many it may contain, so the ancestor walk stops one level short of it.
    """
    provider = _provider()
    album_dir = "The Artist/Album"  # "The Artist" is a top-level folder; its parent is the root
    _mock_single_file(provider, "artist.nfo", b"<artist><title>The Artist</title></artist>")
    result = await provider._resolve_artist_dir_via_nfo(album_dir, "The Artist", None)
    assert result is None


# --- payload validation -------------------------------------------------------------------


def test_nfo_applies_cleanly_rejects_invalid_field() -> None:
    """A field that would raise while applying to a scratch item fails validation."""
    provider = _provider()
    assert provider._nfo_applies_cleanly({"title": "Album", "year": "1999"}, "album") is True
    assert provider._nfo_applies_cleanly({"title": "Album", "year": "not-a-year"}, "album") is False


def test_nfo_applies_cleanly_rejects_non_scalar_field() -> None:
    """A nested (dict-shaped) field, e.g. a malformed <genre> element, fails validation."""
    provider = _provider()
    non_scalar_genre = {"title": "Album", "genre": {"name": "Rock"}}
    assert provider._nfo_applies_cleanly(non_scalar_genre, "album") is False


def test_nfo_applies_cleanly_accepts_repeated_genre_list() -> None:
    """A repeated <genre> element (xmltodict yields a list) is valid, split_items accepts it."""
    provider = _provider()
    repeated_genre = {"title": "Album", "genre": ["Rock", "Pop"]}
    assert provider._nfo_applies_cleanly(repeated_genre, "album") is True


def test_nfo_applies_cleanly_rejects_non_scalar_title() -> None:
    """A non-scalar title (a plain assignment target, not a raising helper) also fails."""
    provider = _provider()
    non_scalar_title = {"title": {"name": "Album"}}
    assert provider._nfo_applies_cleanly(non_scalar_title, "album") is False


def test_nfo_applies_cleanly_rejects_malformed_mbid() -> None:
    """A present but malformed MusicBrainz id fails validation, not just a missing one."""
    provider = _provider()
    assert (
        provider._nfo_applies_cleanly(
            {"title": "Album", "musicbrainzalbumid": "not-a-valid-uuid"}, "album"
        )
        is False
    )
    assert (
        provider._nfo_applies_cleanly({"title": "Album", "musicbrainzalbumid": ALBUM_MBID}, "album")
        is True
    )


def test_album_nfo_matches_absent_ids_falls_back_to_title() -> None:
    """With no MusicBrainz ids at all, a title match is the deciding factor."""
    root = {"title": "My Album"}
    assert LocalFileSystemProvider._album_nfo_matches(root, None, None, "My Album") is True
    assert LocalFileSystemProvider._album_nfo_matches(root, None, None, "Other Album") is False


def test_album_nfo_matches_rejects_near_but_different_title() -> None:
    """Title matching must be strict: a near-miss like 'Album 1' vs 'Album 2' is not a match."""
    root = {"title": "Album 1"}
    assert LocalFileSystemProvider._album_nfo_matches(root, None, None, "Album 2") is False


def test_album_nfo_matches_rejects_conflicting_album_artist_mbid() -> None:
    """A same-title NFO for a different album artist mbid must not resolve the folder."""
    root = {"title": "Greatest Hits", "musicbrainzalbumartistid": OTHER_MBID}
    assert (
        LocalFileSystemProvider._album_nfo_matches(
            root, None, None, "Greatest Hits", (ARTIST_MBID,)
        )
        is False
    )
    # a matching (or absent) album artist mbid still resolves via title
    assert (
        LocalFileSystemProvider._album_nfo_matches(root, None, None, "Greatest Hits", (OTHER_MBID,))
        is True
    )


def test_album_nfo_matches_rejects_a_conflicting_edition_despite_identical_base_title() -> None:
    """
    Stripping each side's own edition suffix must not make two different editions equal.

    "Album (Live)" and "Album (Remix)" both reduce to the base title "Album", but they name
    two different, incompatible releases - the NFO must not resolve the folder for either.
    """
    root = {"title": "Album (Remix)"}
    album_name, album_version = parse_title_and_version("Album (Live)")
    assert (
        LocalFileSystemProvider._album_nfo_matches(
            root, None, None, album_name, album_version=album_version
        )
        is False
    )


def test_album_nfo_matches_absent_edition_stays_inconclusive() -> None:
    """A plain, edition-less title on either side never blocks an otherwise matching title."""
    root = {"title": "Album (Live)"}
    # the track's own tag carries no edition of its own: still resolves via the title
    assert LocalFileSystemProvider._album_nfo_matches(root, None, None, "Album") is True
    # the NFO's edition still applies to the resolved album regardless
    root = {"title": "Album"}
    album_name, album_version = parse_title_and_version("Album (Live)")
    assert (
        LocalFileSystemProvider._album_nfo_matches(
            root, None, None, album_name, album_version=album_version
        )
        is True
    )


async def test_album_resolves_via_title_match_despite_differently_cased_album_artist_mbid() -> None:
    """The album-artist mbid conflict check must canonicalize both sides before comparing."""
    provider = _provider()
    track_dir = "Artist/CAT-1234"
    _mock_single_file(
        provider,
        "Artist/CAT-1234/album.nfo",
        f"<album><title>My Album</title>"
        f"<musicbrainzalbumartistid>{ARTIST_MBID}</musicbrainzalbumartistid></album>".encode(),
    )
    result = await provider._resolve_album_dir_via_nfo(
        track_dir, _tags(album_artist_ids=(ARTIST_MBID.upper(),))
    )
    assert result is not None
    assert result[0] == track_dir


def test_artist_nfo_matches_prefers_mbid_over_name() -> None:
    """A present, matching artist mbid is sufficient even without checking the name."""
    root = {"musicbrainzartistid": ARTIST_MBID}
    assert LocalFileSystemProvider._artist_nfo_matches(root, "Anything", ARTIST_MBID) is True
    assert LocalFileSystemProvider._artist_nfo_matches(root, "Anything", OTHER_MBID) is False


def test_artist_nfo_matches_rejects_near_but_different_name() -> None:
    """Name matching must be strict: a near-miss like 'Artist 1' vs 'Artist 2' is not a match."""
    root = {"title": "Artist 1"}
    assert LocalFileSystemProvider._artist_nfo_matches(root, "Artist 2", None) is False


# --- get_artist refresh reaches the NFO fallback for a synthetic (no-path) artist ---------


async def test_get_artist_refresh_anchors_on_representative_track_for_synthetic_artist() -> None:
    """Refreshing a synthetic (path-less) artist still attempts resolution via its own track."""
    provider = _provider()
    db_artist = MagicMock(
        item_id="1",
        name="The Artist",
        sort_name=None,
        mbid=None,
        provider_mappings=[MagicMock(provider_instance=INSTANCE_ID, url=None)],
    )
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=db_artist)
    provider.exists = AsyncMock(return_value=False)
    provider._resolve_artist_representative_track = AsyncMock(
        return_value="Music/The Artist/Album/track.mp3"
    )
    parsed_artist = MagicMock()
    provider._parse_artist = AsyncMock(return_value=parsed_artist)

    result = await provider.get_artist("The Artist")

    assert result is parsed_artist
    provider._parse_artist.assert_awaited_once()
    _args, kwargs = provider._parse_artist.await_args
    assert kwargs["album_dir"] == "Music/The Artist/Album"
    assert kwargs["representative_track"] == "Music/The Artist/Album/track.mp3"


async def test_get_artist_refresh_returns_db_artist_with_no_track_to_anchor_on() -> None:
    """A synthetic artist with no track of its own at all still just returns the db item."""
    provider = _provider()
    db_artist = MagicMock(
        item_id="1",
        name="The Artist",
        provider_mappings=[MagicMock(provider_instance=INSTANCE_ID, url=None)],
    )
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=db_artist)
    provider.exists = AsyncMock(return_value=False)
    provider._resolve_artist_representative_track = AsyncMock(return_value=None)
    provider._parse_artist = AsyncMock()

    result = await provider.get_artist("The Artist")

    assert result is db_artist


async def test_get_artist_mappingless_refetch_uses_folder_basename_as_name() -> None:
    """
    The stateless second fetch of a just-resolved artist never leaks the full path as name.

    A manual "Refresh item" re-fetches the artist by its new (resolved) id before that mapping
    is persisted, so this id-only lookup finds no db item yet. With no NFO mbid and no matching
    library artist to recover identity from, the folder's basename (not its full, possibly
    nested, path) is the display name until an artist.nfo title (if any) overrides it inside
    ``_parse_artist``.
    """
    provider = _provider()
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[])  # no artist.nfo to recover an mbid from
    provider.mass.music.artists.iter_library_items = _async_iter([])
    parsed_artist = MagicMock()
    provider._parse_artist = AsyncMock(return_value=parsed_artist)

    result = await provider.get_artist("Various Artists/CAT-1234")

    assert result is parsed_artist
    args, kwargs = provider._parse_artist.await_args
    assert args[0] == "CAT-1234"
    assert kwargs["artist_path"] == "Various Artists/CAT-1234"


async def test_get_artist_mappingless_refetch_recovers_identity_from_mbid_only_nfo() -> None:
    """
    An mbid-only artist.nfo must recover the real name, not leak the folder basename.

    This is the same stateless second fetch as above, but the resolved folder's own
    artist.nfo carries only a `musicbrainzartistid` (no title/name), which `parse_artist_nfo`
    can't use to fix up a wrong name after the fact. The already-known library artist behind
    that mbid is looked up instead, so its real name/sort_name survive the refetch.
    """
    provider = _provider()
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[_item("CAT-1234/artist.nfo")])
    provider._read_file = AsyncMock(
        return_value=f"<artist><musicbrainzartistid>{ARTIST_MBID}</musicbrainzartistid></artist>".encode()
    )
    library_artist = MagicMock(sort_name="Real Artist, The")
    library_artist.name = "The Real Artist"
    provider.mass.music.artists.get_library_item_by_external_id = AsyncMock(
        return_value=library_artist
    )
    parsed_artist = MagicMock()
    provider._parse_artist = AsyncMock(return_value=parsed_artist)

    result = await provider.get_artist("CAT-1234")

    assert result is parsed_artist
    provider.mass.music.artists.get_library_item_by_external_id.assert_awaited_once_with(
        ARTIST_MBID, ExternalID.MB_ARTIST
    )
    _args, kwargs = provider._parse_artist.await_args
    assert kwargs == {
        "sort_name": "Real Artist, The",
        "mbid": ARTIST_MBID,
        "artist_path": "CAT-1234",
    }
    assert provider._parse_artist.await_args.args == ("The Real Artist",)


async def test_get_artist_mappingless_refetch_recovers_identity_via_sort_name_alias() -> None:
    """
    A sort-name-alias folder match must recover the real name, not the folder's own basename.

    A normal folder/sort-name-alias match (not an artist.nfo) carries no mbid to recover
    identity from; the one already-known library artist whose name or sort-name matches this
    folder is looked up instead, so the second, not-yet-persisted fetch doesn't rename it.
    """
    provider = _provider()
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[])  # no artist.nfo in this folder
    library_artist = MagicMock(sort_name="Beatles, The", mbid=None)
    library_artist.name = "The Beatles"
    provider.mass.music.artists.iter_library_items = _async_iter([library_artist])
    parsed_artist = MagicMock()
    provider._parse_artist = AsyncMock(return_value=parsed_artist)

    result = await provider.get_artist("Music/Beatles, The")

    assert result is parsed_artist
    _args, kwargs = provider._parse_artist.await_args
    assert kwargs == {
        "sort_name": "Beatles, The",
        "mbid": None,
        "artist_path": "Music/Beatles, The",
    }
    assert provider._parse_artist.await_args.args == ("The Beatles",)


async def test_get_artist_mappingless_refetch_ignores_ambiguous_folder_name_match() -> None:
    """Two library artists matching the same folder name is never a safe positive identity."""
    provider = _provider()
    provider.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[])
    ambiguous_a = MagicMock(sort_name=None)
    ambiguous_a.name = "Beatles, The"
    ambiguous_b = MagicMock(sort_name=None)
    ambiguous_b.name = "Beatles, The"
    provider.mass.music.artists.iter_library_items = _async_iter([ambiguous_a, ambiguous_b])
    parsed_artist = MagicMock()
    provider._parse_artist = AsyncMock(return_value=parsed_artist)

    result = await provider.get_artist("Music/Beatles, The")

    assert result is parsed_artist
    _args, kwargs = provider._parse_artist.await_args
    assert kwargs["sort_name"] is None
    assert kwargs["mbid"] is None
    assert provider._parse_artist.await_args.args == ("Beatles, The",)


# --- sync-index lookup vs on-demand ------------------------------------------------------


async def test_nfo_item_for_uses_sync_index_during_a_sync() -> None:
    """Once the sync's NFO index is ready, the lookup is pure, never a filesystem probe."""
    provider = _provider()
    provider.sync_running = True
    provider._sync_nfo_index_ready = True
    nfo_item = _item("Artist/Album/album.nfo")
    provider._sync_nfo_by_dir = {"Artist/Album": {"album.nfo": nfo_item}}
    provider._scandir = AsyncMock(side_effect=AssertionError("must not touch the filesystem"))
    result = await provider._nfo_item_for("Artist/Album", "album.nfo")
    assert result is nfo_item
    result = await provider._nfo_item_for("Artist/Other", "album.nfo")
    assert result is None


async def test_nfo_item_for_bypasses_a_concurrent_syncs_index_during_a_forced_refresh() -> None:
    """
    A forced refresh never joins a concurrently running sync's provider-wide index.

    `_sync_nfo_index_ready` is set once for the whole provider, not scoped to one request; a
    manual "Refresh item" racing an unrelated background sync must still see a live listing
    instead of that sync's own (possibly already-stale) snapshot.
    """
    provider = _provider()
    provider.sync_running = True
    provider._sync_nfo_index_ready = True
    provider._sync_nfo_by_dir = {"Artist/Album": {"album.nfo": _item("Artist/Album/stale.nfo")}}
    fresh_item = _item("Artist/Album/album.nfo")
    provider._scandir = AsyncMock(return_value=[fresh_item])

    token = BYPASS_CACHE.set(True)
    try:
        result = await provider._nfo_item_for("Artist/Album", "album.nfo")
    finally:
        BYPASS_CACHE.reset(token)

    assert result is fresh_item


async def test_ondemand_listing_scope_memoizes_during_a_forced_refresh_racing_a_sync() -> None:
    """
    A forced refresh gets its own memo even while a concurrent sync's index is ready.

    Otherwise a refresh touching several candidate folders (e.g. an artist's ancestor levels)
    would repeat every listing once per lookup instead of once for the whole parse, since
    `_nfo_item_for` never takes the sync's index shortcut during a refresh in the first place.
    """
    provider = _provider()
    provider.sync_running = True
    provider._sync_nfo_index_ready = True
    nfo_item = _item("Artist/Album/album.nfo")
    provider._scandir = AsyncMock(return_value=[nfo_item])

    token = BYPASS_CACHE.set(True)
    try:
        with provider._ondemand_listing_scope():
            first = await provider._nfo_item_for("Artist/Album", "album.nfo")
            second = await provider._nfo_item_for("Artist/Album", "artist.nfo")
    finally:
        BYPASS_CACHE.reset(token)

    assert first is nfo_item
    assert second is None
    provider._scandir.assert_awaited_once()


async def test_nfo_item_for_reuses_the_sync_batch_scope_while_the_index_is_unready() -> None:
    """
    While the sync index isn't trusted, a folder shared by several tracks is listed once.

    This is the fallback used before the walk completes (or, permanently for that sync,
    after an incomplete scan): `sync_library` wraps its whole track-processing batch in one
    shared `_ondemand_listing_scope`, so this reuses the same per-parse memo used outside a
    sync, rather than a fresh listing per track sharing the folder.
    """
    provider = _provider()
    provider.sync_running = True
    provider._sync_nfo_index_ready = False
    nfo_item = _item("Artist/Album/album.nfo")
    provider._scandir = AsyncMock(return_value=[nfo_item])

    with provider._ondemand_listing_scope():
        first = await provider._nfo_item_for("Artist/Album", "album.nfo")
        second = await provider._nfo_item_for("Artist/Album", "artist.nfo")

    assert first is nfo_item
    assert second is None
    provider._scandir.assert_awaited_once()


async def test_nfo_item_for_memoizes_on_demand_lookups() -> None:
    """Outside a sync, repeated lookups for the same candidate folder list it only once."""
    provider = _provider()
    nfo_item = _item("Artist/Album/album.nfo")
    provider._scandir = AsyncMock(return_value=[nfo_item])
    with provider._ondemand_listing_scope():
        first = await provider._nfo_item_for("Artist/Album", "album.nfo")
        second = await provider._nfo_item_for("Artist/Album", "artist.nfo")
    assert first is nfo_item
    assert second is None
    provider._scandir.assert_awaited_once()


# --- refresh reachability after an id-changing resolution ---------------------------------


async def test_get_album_tracks_falls_back_to_folder_scan_without_a_db_mapping() -> None:
    """
    A folder not yet mapped in the library is scanned directly for tracks.

    This is the second, id-changed fetch of a manual "Refresh item" that has just resolved a
    previously synthetic album onto its real folder, before that mapping is persisted.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])
    parsed_track = MagicMock(
        album=Album(
            item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
        )
    )
    provider._parse_track = AsyncMock(return_value=parsed_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [parsed_track]


async def test_get_album_tracks_sorts_a_mappingless_result_by_disc_and_track_number() -> None:
    """
    A mappingless result is sorted, since a WebDAV/cloud folder listing order isn't guaranteed.

    The albums controller returns this provider's list unchanged when there is no library
    album yet, so an out-of-order (or reversed) listing must be sorted here.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    track_items = [_item(f"Artist/Album/{n:02d} Track.flac") for n in (3, 1, 2)]
    provider._scandir = AsyncMock(return_value=track_items)
    parsed_tracks = {
        3: MagicMock(album=good_album, disc_number=1, track_number=3),
        1: MagicMock(album=good_album, disc_number=1, track_number=1),
        2: MagicMock(album=good_album, disc_number=1, track_number=2),
    }

    async def _parse_track_side_effect(item: FileSystemItem, _tags: Any) -> Any:
        track_number = int(item.filename.split(" ", 1)[0])
        return parsed_tracks[track_number]

    provider._parse_track = AsyncMock(side_effect=_parse_track_side_effect)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert [track.track_number for track in result] == [1, 2, 3]


async def test_get_album_tracks_raises_when_folder_is_genuinely_missing() -> None:
    """A folder that has no library mapping and does not exist on disk still raises."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=False)

    with pytest.raises(MediaNotFoundError, match="Album not found"):
        await provider.get_album_tracks("Artist/Album")


async def test_parse_artist_enrichment_matches_nfo_case_insensitively() -> None:
    """Direct-path artist parsing (bypassing resolution) still finds a case-variant NFO."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[_item("Artist/ARTIST.NFO")])
    provider._read_file = AsyncMock(return_value=b"<artist><title>The Artist</title></artist>")
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    artist = await provider._parse_artist("The Artist", artist_path="Artist")

    assert artist.name == "The Artist"


async def test_parse_artist_ancestor_plain_name_outranks_a_root_sort_name_alias() -> None:
    """
    A root-level sort-name folder must not outrank a nearer ancestor plain-name match.

    The plain name is tried at every location (root, then ancestor) before the sort-name
    alias is tried anywhere, mirroring the precedence already enforced within a single
    location by `get_artist_dir`/`get_album_dir`.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # "Beatles, The" (the sort-name alias) exists at the provider root; "The Beatles" (the
    # plain, exact name) does not exist at the root, but does exist as the real ancestor
    # folder one level up from the album
    provider.exists = AsyncMock(side_effect=lambda path: path == "Beatles, The")
    provider._scandir = AsyncMock(return_value=[])
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    artist = await provider._parse_artist(
        "The Beatles",
        sort_name="Beatles, The",
        album_dir="Beatles, The/The Beatles/Album",
    )

    assert artist.item_id == "Beatles, The/The Beatles"


async def test_parse_artist_validated_nfo_outranks_sort_name_alias_normal_match() -> None:
    """
    A validated artist.nfo now outranks a sort-name alias found through ordinary matching.

    An exact (normalized) plain-name match is tried first; once that finds nothing, the
    bounded artist.nfo fallback is attempted before any relaxed heuristic - including the
    sort-name alias, itself a relaxed/fuzzy guess - so a folder found only via the alias
    must not win over a validated NFO elsewhere.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # "Beatles, The" (the sort-name alias) exists at the provider root; the album lives
    # elsewhere, under an ancestor whose own artist.nfo identifies "The Beatles" by plain
    # name - that validated NFO now wins over the alias's ordinary (non-NFO) root match
    provider.exists = AsyncMock(side_effect=lambda path: path == "Beatles, The")
    _mock_single_file(
        provider, "Various/artist.nfo", b"<artist><title>The Beatles</title></artist>"
    )
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    artist = await provider._parse_artist(
        "The Beatles",
        sort_name="Beatles, The",
        album_dir="Various/Album",
    )

    assert artist.item_id == "Various"
    provider._read_file.assert_awaited_once()


async def test_parse_artist_relaxed_match_never_trusts_a_folder_the_nfo_tier_rejected() -> None:
    """
    A relaxed match landing on a folder the NFO tier already rejected must not trust it.

    The bounded validated artist.nfo fallback reads and rejects "Music/Beatles, The"'s own
    artist.nfo (it names a different artist entirely). The sort-name alias then matches that
    exact same folder through ordinary (non-NFO) matching - the rejected file must not be
    silently re-applied during enrichment just because the folder matched some other way.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # only the ancestor folder itself exists (the one the NFO tier rejects and the sort-name
    # alias later matches); no root-level shortcut for either candidate name
    provider.exists = AsyncMock(side_effect=lambda path: path == "Music/Beatles, The")
    _mock_single_file(
        provider, "Music/Beatles, The/artist.nfo", b"<artist><title>Someone Else</title></artist>"
    )
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    async def _empty_iter(*_args: object, **_kwargs: object) -> Any:
        return
        yield  # pragma: no cover - makes this an async generator

    provider.mass.music.artists.iter_library_items = _empty_iter

    artist = await provider._parse_artist(
        "The Beatles",
        sort_name="Beatles, The",
        album_dir="Music/Beatles, The/Album",
    )

    # resolved via the sort-name alias, but never renamed from the rejected NFO's own title
    assert artist.item_id == "Music/Beatles, The"
    assert artist.name == "The Beatles"


async def test_parse_artist_exact_plain_name_match_skips_nfo_resolution() -> None:
    """An exact (normalized) plain-name folder match wins outright; NFO is never consulted."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # only the true ancestor folder "Music/The Artist" exists at all
    provider.exists = AsyncMock(side_effect=lambda path: path == "Music/The Artist")
    provider._scandir = AsyncMock(return_value=[])
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artist_dir_via_nfo = AsyncMock(
        return_value=(
            "Music/Someone Else",
            _item("Music/Someone Else/artist.nfo"),
            {"title": "The Artist"},
        )
    )

    artist = await provider._parse_artist("The Artist", album_dir="Music/The Artist/Album")

    assert artist.item_id == "Music/The Artist"
    provider._resolve_artist_dir_via_nfo.assert_not_awaited()


async def test_parse_artist_malformed_nfo_falls_through_to_relaxed_sort_name_match() -> None:
    """A malformed/non-matching artist.nfo leaves the sort-name alias as the last resort."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # "Beatles, The" (the sort-name alias) exists at the provider root; the album's ancestor
    # has its own artist.nfo, but it names a different artist entirely
    provider.exists = AsyncMock(side_effect=lambda path: path == "Beatles, The")
    _mock_single_file(
        provider, "Various/artist.nfo", b"<artist><title>Somebody Else</title></artist>"
    )
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    artist = await provider._parse_artist(
        "The Beatles",
        sort_name="Beatles, The",
        album_dir="Various/Album",
    )

    # the mismatching NFO was rejected; the sort-name alias resolved it instead
    assert artist.item_id == "Beatles, The"
    # tried once against each candidate (name, then sort-name alias); no root cache to
    # single-flight the second attempt against the same file
    assert provider._read_file.await_count == 2


async def test_get_album_tracks_skips_a_malformed_sibling_track() -> None:
    """One sibling file whose tags cannot be read is skipped, not fatal to the folder scan."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    good_item = _item("Artist/Album/01 Track.flac")
    bad_item = _item("Artist/Album/02 Track.flac")
    provider._scandir = AsyncMock(return_value=[bad_item, good_item])
    parsed_track = MagicMock(
        album=Album(
            item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
        )
    )
    provider._parse_track = AsyncMock(return_value=parsed_track)

    async def _parse_tags_side_effect(absolute_path: str, _file_size: int) -> Any:
        if absolute_path == bad_item.absolute_path:
            raise InvalidDataError("corrupt file")
        return MagicMock()

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(side_effect=_parse_tags_side_effect),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [parsed_track]


def test_build_nfo_index_skips_folders_with_only_non_nfo_metadata_files() -> None:
    """
    An image-only folder must not get an (empty) entry in the sync's NFO index.

    ``metadata_files`` also carries folder artwork; a folder with cover art but no
    album.nfo/artist.nfo must be absent from the index entirely, not present with an empty
    per-directory map, or a large image-heavy library would retain a library-sized index.
    """
    nfo_item = _item("Artist/Album/album.nfo")
    image_item = _item("Artist/Album/folder.jpg")
    image_only_item = _item("Artist/OtherAlbum/cover.jpg")

    index = LocalFileSystemProvider._build_nfo_index([nfo_item, image_item, image_only_item])

    assert index == {"Artist/Album": {"album.nfo": nfo_item}}
    assert "Artist/OtherAlbum" not in index


async def test_get_album_reuses_the_already_parsed_album_from_the_folder_scan() -> None:
    """The mappingless refresh path never re-resolves/re-parses the same representative file."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])
    parsed_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="My Album", provider_mappings=set()
    )
    parsed_track = MagicMock(album=parsed_album)
    provider._parse_track = AsyncMock(return_value=parsed_track)
    provider.resolve = AsyncMock(side_effect=AssertionError("must not re-resolve the same file"))

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album("Artist/Album")

    assert result is parsed_album
    provider._parse_track.assert_awaited_once()


async def test_get_album_closes_the_track_scan_deterministically_on_early_return() -> None:
    """
    Returning early from `get_album` still closes its underlying scan generator right away.

    Otherwise the generator's `_ondemand_listing_scope()` cleanup (a ContextVar reset) is left
    to whenever the event loop's async-generator finalizer happens to run, instead of
    deterministically, right when `get_album` returns.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])
    parsed_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="My Album", provider_mappings=set()
    )
    provider._parse_track = AsyncMock(return_value=MagicMock(album=parsed_album))

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        await provider.get_album("Artist/Album")

    # the on-demand memo must already be torn down, synchronously, not left for GC to close
    assert _ONDEMAND_NFO_ITEMS.get() is None


async def test_get_album_tracks_skips_a_leading_track_without_an_album() -> None:
    """A parseable file with no album tag is skipped, not returned as a false representative."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    no_album_item = _item("Artist/Album/00 Intro.flac")
    good_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[no_album_item, good_item])
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    no_album_track = MagicMock(album=None)
    good_track = MagicMock(album=good_album)

    async def _parse_track_side_effect(item: FileSystemItem, _tags: Any) -> Any:
        return no_album_track if item is no_album_item else good_track

    provider._parse_track = AsyncMock(side_effect=_parse_track_side_effect)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_tracks_propagates_a_transient_failure_from_parse_track() -> None:
    """A transient failure while building the full track (e.g. NFO I/O) propagates for retry."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])
    provider._parse_track = AsyncMock(side_effect=OSError("network hiccup"))

    with (
        patch(
            "music_assistant.providers.filesystem_local.async_parse_tags",
            AsyncMock(return_value=MagicMock()),
        ),
        pytest.raises(OSError, match="network hiccup"),
    ):
        await provider.get_album_tracks("Artist/Album")


async def test_get_album_tracks_ignores_a_track_resolving_to_a_different_folder() -> None:
    """A stray/mis-tagged file whose own identity resolves elsewhere is never mistaken for this folder."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    stray_item = _item("Artist/Album/00 Stray.flac")
    good_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[stray_item, good_item])
    other_album = Album(
        item_id="Other/Folder", provider=INSTANCE_ID, name="Other", provider_mappings=set()
    )
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    stray_track = MagicMock(album=other_album)
    good_track = MagicMock(album=good_album)

    async def _parse_track_side_effect(item: FileSystemItem, _tags: Any) -> Any:
        return stray_track if item is stray_item else good_track

    provider._parse_track = AsyncMock(side_effect=_parse_track_side_effect)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_tracks_tolerates_an_invalid_cue_sheet() -> None:
    """An empty/invalid CUE sheet is skipped, not fatal to the rest of the folder scan."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    bad_cue = _item("Artist/Album/bad.cue")
    good_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[bad_cue, good_item])
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    good_track = MagicMock(album=good_album)
    provider._parse_track = AsyncMock(return_value=good_track)

    async def _cue_parse_tracks_side_effect(item: FileSystemItem) -> Any:
        if item is bad_cue:
            raise InvalidDataError("CUE sheet has no tracks")
        return []

    provider._cue.parse_tracks = AsyncMock(side_effect=_cue_parse_tracks_side_effect)
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path=None))
    provider._cue.find_audio_file = AsyncMock(return_value="Artist/Album/audio.flac")

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_tracks_propagates_a_transient_cue_album_resolution_failure() -> None:
    """A transient failure while resolving a CUE track's album (e.g. NFO I/O) propagates."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    provider._scandir = AsyncMock(return_value=[cue_item])
    provider._cue.parse_tracks = AsyncMock(side_effect=OSError("network hiccup"))
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path=None))
    provider._cue.find_audio_file = AsyncMock(return_value="Artist/Album/audio.flac")

    with pytest.raises(OSError, match="network hiccup"):
        await provider.get_album_tracks("Artist/Album")


async def test_get_album_tracks_propagates_media_not_found_from_cue_album_construction() -> None:
    """
    A `MediaNotFoundError` while constructing a CUE track's album must propagate too.

    A cloud/WebDAV provider's own `_read_file` raises `MediaNotFoundError` for any failed
    read, not only a genuinely missing file, so this specific error type must not be treated
    as "this CUE is unreadable" once its companion audio file is already confirmed present.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    provider._scandir = AsyncMock(return_value=[cue_item])
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path="album.flac"))
    provider._cue.find_audio_file = AsyncMock(return_value="Artist/Album/album.flac")
    provider._cue.parse_tracks = AsyncMock(
        side_effect=MediaNotFoundError("transient NFO file read failure")
    )

    with pytest.raises(MediaNotFoundError, match="transient NFO file read failure"):
        await provider.get_album_tracks("Artist/Album")


async def test_get_album_tracks_propagates_a_non_tag_error_from_a_plain_track() -> None:
    """
    A transient storage failure while reading a plain track's tags is not "unreadable tags".

    Only `InvalidDataError` (genuinely malformed tags) is treated as "skip this track";
    anything else - e.g. a cloud/WebDAV provider's own transient read failure - must
    propagate so the sync can retry instead of silently dropping the track.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])

    with (
        patch(
            "music_assistant.providers.filesystem_local.async_parse_tags",
            AsyncMock(side_effect=MediaNotFoundError("transient read failure")),
        ),
        pytest.raises(MediaNotFoundError, match="transient read failure"),
    ):
        await provider.get_album_tracks("Artist/Album")


async def test_get_album_tracks_propagates_an_os_error_from_a_plain_track() -> None:
    """An `OSError` (e.g. ffprobe failing to launch) must propagate, not mark a track unreadable."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[track_item])

    with (
        patch(
            "music_assistant.providers.filesystem_local.async_parse_tags",
            AsyncMock(side_effect=OSError("ffprobe not found")),
        ),
        pytest.raises(OSError, match="ffprobe not found"),
    ):
        await provider.get_album_tracks("Artist/Album")


async def test_get_album_tracks_skips_a_cue_with_missing_companion_audio() -> None:
    """A CUE sheet whose companion audio file cannot be found is skipped, not fatal."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    good_item = _item("Artist/Album/01 Track.flac")
    provider._scandir = AsyncMock(return_value=[cue_item, good_item])
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path="missing.flac"))
    provider._cue.find_audio_file = AsyncMock(return_value=None)
    provider._cue.parse_tracks = AsyncMock(
        side_effect=AssertionError("must not attempt to fully parse a CUE with no companion")
    )
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    good_track = MagicMock(album=good_album)
    provider._parse_track = AsyncMock(return_value=good_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_tracks_excludes_cue_companion_audio() -> None:
    """A CUE's companion audio file is never yielded as its own unsegmented track."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    companion_item = _item("Artist/Album/album.flac")
    provider._scandir = AsyncMock(return_value=[cue_item, companion_item])
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    cue_track = MagicMock(album=good_album)
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path="album.flac"))
    provider._cue.find_audio_file = AsyncMock(return_value="Artist/Album/album.flac")
    provider._cue.parse_tracks = AsyncMock(return_value=[cue_track])
    provider._parse_track = AsyncMock(
        side_effect=AssertionError("the companion audio must not be parsed as its own track")
    )

    result = await provider.get_album_tracks("Artist/Album")

    assert result == [cue_track]


async def test_get_album_tracks_processes_the_companion_of_a_track_less_cue_sheet() -> None:
    """
    A CUE sheet that parses cleanly but names no tracks must not exclude its companion.

    `load_cue_sheet` never raises for malformed/truncated content with no TRACK lines - it
    just returns a track-less sheet - so the companion audio file must still be processed as
    a normal, standalone track instead of silently disappearing.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    companion_item = _item("Artist/Album/album.flac")
    provider._scandir = AsyncMock(return_value=[cue_item, companion_item])
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    good_track = MagicMock(album=good_album)
    provider._cue.load_cue_sheet = AsyncMock(
        return_value=MagicMock(file_path="album.flac", tracks=[])
    )
    provider._cue.parse_tracks = AsyncMock(
        side_effect=AssertionError("a track-less CUE sheet must not be parsed for tracks")
    )
    provider._parse_track = AsyncMock(return_value=good_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_tracks_excludes_a_cross_directory_cue_companion() -> None:
    """
    A CUE's companion audio is excluded even when it lives in a different subfolder.

    One CUE at the album's top level can cover a companion audio file split across a "Disc 1"
    subfolder; that companion must still be recognized as absorbed once the subfolder itself
    is scanned, not parsed again there as an unsegmented duplicate track.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    cue_item = _item("Artist/Album/album.cue")
    disc_dir = _item("Artist/Album/Disc 1")
    disc_dir.is_dir = True
    companion_item = _item("Artist/Album/Disc 1/album.flac")
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    cue_track = MagicMock(album=good_album)
    provider._cue.load_cue_sheet = AsyncMock(return_value=MagicMock(file_path="Disc 1/album.flac"))
    provider._cue.find_audio_file = AsyncMock(return_value="Artist/Album/Disc 1/album.flac")
    provider._cue.parse_tracks = AsyncMock(return_value=[cue_track])
    provider._parse_track = AsyncMock(
        side_effect=AssertionError(
            "the cross-directory companion audio must not be parsed as its own track"
        )
    )

    async def _scandir_side_effect(scan_folder: str) -> list[FileSystemItem]:
        if scan_folder == "Artist/Album":
            return [cue_item, disc_dir]
        if scan_folder == "Artist/Album/Disc 1":
            return [companion_item]
        return []

    provider._scandir = AsyncMock(side_effect=_scandir_side_effect)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(
            side_effect=AssertionError(
                "the cross-directory companion audio must not be tag-parsed either"
            )
        ),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [cue_track]


async def test_get_album_tracks_scans_arbitrarily_named_subfolder() -> None:
    """
    An arbitrarily named subfolder (not a regex-recognized disc dir) is still scanned.

    An album resolved via NFO onto an oddly named parent folder may have all of its tracks in a
    subfolder that normal disc-folder detection cannot recognize (e.g. "weird-disc-name").
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    subfolder = _item("Artist/Album/weird-disc-name")
    subfolder.is_dir = True
    track_item = _item("Artist/Album/weird-disc-name/01 Track.flac")
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    good_track = MagicMock(album=good_album)

    async def _scandir_side_effect(scan_folder: str) -> list[FileSystemItem]:
        if scan_folder == "Artist/Album":
            return [subfolder]
        if scan_folder == "Artist/Album/weird-disc-name":
            return [track_item]
        return []

    provider._scandir = AsyncMock(side_effect=_scandir_side_effect)
    provider._parse_track = AsyncMock(return_value=good_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert result == [good_track]


async def test_get_album_never_lists_a_subfolder_when_the_root_already_satisfies_it() -> None:
    """
    `get_album` (needing just one track) must not pay for a subfolder listing it never uses.

    Each folder listing is a remote round trip for a cloud/WebDAv-backed provider, so a caller
    that stops as soon as it finds a usable track in the root should never cause a subfolder to
    be listed at all.
    """
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    root_track_item = _item("Artist/Album/01 Track.flac")
    subfolder = _item("Artist/Album/Disc 2")
    subfolder.is_dir = True
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )
    good_track = MagicMock(album=good_album)

    async def _scandir_side_effect(scan_folder: str) -> list[FileSystemItem]:
        if scan_folder == "Artist/Album":
            return [root_track_item, subfolder]
        raise AssertionError(f"must not list the subfolder {scan_folder!r}")

    provider._scandir = AsyncMock(side_effect=_scandir_side_effect)
    provider._parse_track = AsyncMock(return_value=good_track)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album("Artist/Album")

    assert result is good_album


async def test_get_album_tracks_shares_one_listing_memo_across_the_whole_scan() -> None:
    """A mappingless multi-track album lists each candidate folder once, not once per track."""
    provider = _provider()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.exists = AsyncMock(return_value=True)
    track_items = [_item(f"Artist/Album/{n:02d} Track.flac") for n in range(1, 4)]
    provider._scandir = AsyncMock(return_value=list(track_items))
    good_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Album", provider_mappings=set()
    )

    async def _parse_track_side_effect(_item: FileSystemItem, _tags: Any) -> Any:
        # exercise the real _ondemand_listing_scope-driven lookup path for each track
        nfo_item = await provider._nfo_item_for("Artist", "artist.nfo")
        assert nfo_item is None  # no artist.nfo in this fixture; only the call count matters
        return MagicMock(album=good_album, disc_number=1, track_number=1)

    provider._parse_track = AsyncMock(side_effect=_parse_track_side_effect)

    with patch(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await provider.get_album_tracks("Artist/Album")

    assert len(result) == 3
    # "Artist" is listed once for the whole scan, not once per track
    listed_folders = [call.args[0] for call in provider._scandir.await_args_list]
    assert listed_folders.count("Artist") == 1


async def test_parse_album_scans_folder_once_when_track_dir_equals_album_dir() -> None:
    """When NFO resolution resolves album_dir onto track_dir itself, it is only processed once."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    nfo_item = _item("Artist/CAT-1234/album.nfo")
    provider._scandir = AsyncMock(return_value=[nfo_item])
    provider._read_file = AsyncMock(return_value=b"<album><title>My Album</title></album>")
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[])
    provider.config.get_value = MagicMock(return_value="various_artists")

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=[],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    await provider._parse_album(track_path="Artist/CAT-1234/t1.mp3", track_tags=tags)

    # the resolved album folder ("Artist/CAT-1234") must be scanned for images only once,
    # even though it is both the track's own directory and the resolved album directory
    album_folder_calls = [
        call
        for call in provider._get_local_images.await_args_list
        if call.args[0] == "Artist/CAT-1234"
    ]
    assert len(album_folder_calls) == 1


async def test_parse_album_never_enriches_from_the_losing_candidate_folders_own_nfo() -> None:
    """
    Only the validated, winning NFO applies - a rejected candidate's own NFO must not too.

    The track's own directory ("Artist/CAT-1234") has its own album.nfo, but it names a
    different album and is rejected during resolution; the parent's album.nfo wins instead.
    Both folders are still visited by the enrichment loop (`dict.fromkeys((track_dir,
    album_dir))`), so the losing folder's unvalidated album.nfo must not leak its own genre
    into the resolved album (the winning NFO here sets no genre of its own, so a leaked value
    would otherwise survive even though the losing folder is processed before the winner).
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[])
    provider.config.get_value = MagicMock(return_value="various_artists")

    async def _scandir(folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        return [_item(f"{folder}/album.nfo")]

    async def _read_file(path: str) -> bytes:
        if path == "Artist/CAT-1234/album.nfo":
            return b"<album><title>Wrong Album</title><genre>Jazz</genre></album>"
        return b"<album><title>My Album</title></album>"

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(side_effect=_read_file)

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=[],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(track_path="Artist/CAT-1234/t1.mp3", track_tags=tags)

    assert album.name == "My Album"
    assert not album.metadata.genres


async def test_parse_album_artist_resolves_from_ancestor_nfo_while_album_stays_synthetic() -> None:
    """
    The artist's own ancestor resolution must not depend on the album resolving too.

    The track's directory ("Artist/CAT-1234") matches neither the album name nor any
    album.nfo there, so the album stays synthetic (tag-only, no folder). The artist must
    still be looked up starting from that same directory (not only when an album folder was
    found), so its ancestor artist.nfo one level up ("Artist") still resolves it to a real
    folder.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # deliberately not the root-level "The Artist" folder itself: forces the resolution to
    # rely on the ancestor artist.nfo, not a root-level literal name match
    provider.exists = AsyncMock(return_value=False)
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(
        return_value=[("The Artist", ARTIST_MBID, None)]
    )

    async def _scandir(folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        if folder == "Artist":
            return [_item("Artist/artist.nfo")]
        return []  # no album.nfo anywhere: the album can never resolve to a folder

    async def _read_file(path: str) -> bytes:
        assert path == "Artist/artist.nfo"
        return f"<artist><musicbrainzartistid>{ARTIST_MBID}</musicbrainzartistid></artist>".encode()

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(side_effect=_read_file)

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(track_path="Artist/CAT-1234/t1.mp3", track_tags=tags)

    # the album itself never resolved to a folder: a synthetic, name-based identity
    assert album.provider_mappings
    album_mapping = next(iter(album.provider_mappings))
    assert album_mapping.url is None
    assert album_mapping.item_id == "The Artist" + os.sep + "My Album"
    # the artist resolved to its real ancestor folder regardless
    assert album.artists[0].item_id == "Artist"


async def test_parse_album_artist_resolves_from_ancestor_name_while_album_stays_synthetic() -> None:
    """The same anchoring also applies to a normal (non-NFO) ancestor name match."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    # only the true ancestor folder "Music/The Artist" exists; a root-level "The Artist" (or
    # its filesystem-safe variant) does not, so a false-positive root-level shortcut can't mask
    # whether the ancestor walk itself is actually anchored on the track's own directory
    provider.exists = AsyncMock(side_effect=lambda path: path == "Music/The Artist")
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[("The Artist", None, None)])
    provider._scandir = AsyncMock(return_value=[])  # no NFOs anywhere

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(
        track_path="Music/The Artist/CAT-1234/t1.mp3", track_tags=tags
    )

    assert album.provider_mappings
    album_mapping = next(iter(album.provider_mappings))
    assert album_mapping.url is None
    # the artist resolved by ordinary folder-name matching, anchored on the track's own
    # directory rather than a (never-resolved) album directory
    assert album.artists[0].item_id == "Music/The Artist"


# --- three-tier precedence: exact folder match, then validated NFO, then relaxed match ----


async def test_parse_album_exact_folder_match_skips_nfo_resolution() -> None:
    """An exact (normalized) folder match wins outright; a validated album.nfo is never tried."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._scandir = AsyncMock(return_value=[])
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[("The Artist", None, None)])
    provider._resolve_album_dir_via_nfo = AsyncMock(
        return_value=("Artist", _item("Artist/album.nfo"), {"title": "My Album"})
    )

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(track_path="Artist/My Album/t1.mp3", track_tags=tags)

    album_mapping = next(iter(album.provider_mappings))
    assert album_mapping.url == "Artist/My Album"
    provider._resolve_album_dir_via_nfo.assert_not_awaited()


async def test_parse_album_validated_nfo_outranks_a_relaxed_date_prefix_match() -> None:
    """
    A validated album.nfo at the parent outranks a relaxed date-prefix match at track_dir.

    The track's own directory is date-prefixed ("2025-03-14 My Album") and would match the
    album by the new relaxed date-prefix heuristic alone; but its true parent has its own
    validated album.nfo, and the bounded NFO fallback is tried before any relaxed heuristic.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[("The Artist", None, None)])

    async def _scandir(folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        if folder == "Artist/RealAlbumFolder":
            return [_item("Artist/RealAlbumFolder/album.nfo")]
        return []  # the date-prefixed track_dir itself has no album.nfo of its own

    async def _read_file(path: str) -> bytes:
        assert path == "Artist/RealAlbumFolder/album.nfo"
        return b"<album><title>My Album</title></album>"

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(side_effect=_read_file)

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(
        track_path="Artist/RealAlbumFolder/2025-03-14 My Album/t1.mp3", track_tags=tags
    )

    album_mapping = next(iter(album.provider_mappings))
    # the validated parent NFO won, not the date-prefixed track_dir a relaxed match would pick
    assert album_mapping.url == "Artist/RealAlbumFolder"


async def test_parse_album_malformed_nfo_falls_through_to_relaxed_date_prefix_match() -> None:
    """A malformed/non-matching album.nfo leaves the relaxed heuristic as the last resort."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[("The Artist", None, None)])

    async def _scandir(folder: str, use_cache: bool = True) -> list[FileSystemItem]:  # noqa: ARG001
        if folder == "Artist":
            return [_item("Artist/album.nfo")]
        return []

    async def _read_file(path: str) -> bytes:
        assert path == "Artist/album.nfo"
        # names a different album entirely: never a positive identity match
        return b"<album><title>Somebody Else's Album</title></album>"

    provider._scandir = AsyncMock(side_effect=_scandir)
    provider._read_file = AsyncMock(side_effect=_read_file)

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(
        track_path="Artist/2025-03-14 My Album/t1.mp3", track_tags=tags
    )

    album_mapping = next(iter(album.provider_mappings))
    # the mismatching NFO was rejected; the relaxed date-prefix match resolved it instead
    assert album_mapping.url == "Artist/2025-03-14 My Album"


async def test_parse_album_relaxed_match_never_trusts_a_folder_the_nfo_tier_rejected() -> None:
    """
    A relaxed match landing on a folder the NFO tier already rejected must not trust it.

    The bounded validated album.nfo fallback reads and rejects the track's own directory's
    album.nfo (it names a different album entirely). The relaxed date-prefix heuristic then
    matches that exact same folder through ordinary (non-NFO) matching - the rejected file
    must not be silently re-applied during enrichment just because the folder matched some
    other way.
    """
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._get_local_images = AsyncMock(return_value=[])
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[("The Artist", None, None)])
    _mock_single_file(
        provider,
        "Artist/2025-03-14 My Album/album.nfo",
        b"<album><title>Somebody Else's Album</title></album>",
    )

    tags = MagicMock(
        album="My Album",
        album_sort=None,
        album_artists=["The Artist"],
        barcode=None,
        musicbrainz_albumid=None,
        musicbrainz_releasegroupid=None,
        year=None,
        album_type=AlbumType.ALBUM,
        filename="track.mp3",
    )
    album = await provider._parse_album(
        track_path="Artist/2025-03-14 My Album/t1.mp3", track_tags=tags
    )

    # resolved via the relaxed date-prefix match, but never renamed from the rejected NFO
    album_mapping = next(iter(album.provider_mappings))
    assert album_mapping.url == "Artist/2025-03-14 My Album"
    assert album.name == "My Album"
