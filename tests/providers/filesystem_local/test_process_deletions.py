"""Tests for the filesystem provider's deletion pass."""

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.streams.constants import AA_TABLE_ANALYSIS
from music_assistant.mass import MusicAssistant
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
    for media_type in (MediaType.TRACK, MediaType.PLAYLIST, MediaType.AUDIOBOOK):
        controller = MagicMock()
        controller.get_library_item_by_prov_id = AsyncMock(return_value=MagicMock(item_id="1"))
        controller.remove_provider_mapping = AsyncMock()
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
    """A deleted file's mapping is removed whatever the case of its extension."""
    provider, controllers = _create_provider()

    await provider._process_deletions({file_path})

    controller = controllers[media_type]
    controller.get_library_item_by_prov_id.assert_awaited_once_with(
        file_path, "filesystem_local--test"
    )
    controller.remove_provider_mapping.assert_awaited_once_with(
        "1", "filesystem_local--test", file_path
    )


@pytest.mark.parametrize(
    ("content_type", "media_type"),
    [
        ("music", MediaType.TRACK),
        ("audiobooks", MediaType.AUDIOBOOK),
    ],
)
async def test_folder_id_removed_as_main_media_type(
    content_type: str, media_type: MediaType
) -> None:
    """A stored id without a file extension is removed as the source's main media type."""
    provider, controllers = _create_provider()
    provider.media_content_type = content_type

    await provider._process_deletions({"Music"})

    controller = controllers[media_type]
    controller.get_library_item_by_prov_id.assert_awaited_once_with(
        "Music", "filesystem_local--test"
    )
    controller.remove_provider_mapping.assert_awaited_once_with(
        "1", "filesystem_local--test", "Music"
    )


async def test_empty_id_is_skipped() -> None:
    """A stored empty id is left alone."""
    provider, controllers = _create_provider()

    await provider._process_deletions({""})

    for controller in controllers.values():
        controller.get_library_item_by_prov_id.assert_not_called()


async def test_unsupported_extension_is_skipped() -> None:
    """A deleted file with an unsupported extension is left alone."""
    provider, controllers = _create_provider()

    await provider._process_deletions({"Artist/Album/cover.JPG"})

    for controller in controllers.values():
        controller.get_library_item_by_prov_id.assert_not_called()


@pytest.mark.parametrize("other_in_library", [True, False])
async def test_deleted_file_keeps_other_provider_mappings(
    mass: MusicAssistant, other_in_library: bool
) -> None:
    """Deleting a local file keeps a library track that another provider still maps."""
    provider, _ = _create_provider()
    provider.mass = mass
    file_path = "Artist/Album/01 - Track.flac"
    db_track = await mass.music.tracks.add_item_to_library(
        Track(
            item_id=file_path,
            provider="filesystem_local--test",
            name="Track",
            artists=UniqueList(
                [
                    Artist(
                        item_id="Artist",
                        provider="filesystem_local--test",
                        name="Artist",
                        provider_mappings={
                            ProviderMapping(
                                item_id="Artist",
                                provider_domain="filesystem_local",
                                provider_instance="filesystem_local--test",
                            )
                        },
                    )
                ]
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=file_path,
                    provider_domain="filesystem_local",
                    provider_instance="filesystem_local--test",
                    in_library=True,
                ),
                ProviderMapping(
                    item_id="sp1",
                    provider_domain="spotify",
                    provider_instance="spotify--test",
                    in_library=other_in_library,
                ),
            },
        )
    )

    analysis_row = {
        "media_type": MediaType.TRACK.value,
        "item_id": file_path,
        "provider": "filesystem_local--test",
    }
    await mass.streams.audio_analysis.database.insert(
        AA_TABLE_ANALYSIS,
        {**analysis_row, "aa_provider_domain": "test", "header": "{}", "payload": b""},
    )

    await provider._process_deletions({file_path})

    library_track = await mass.music.tracks.get_library_item(db_track.item_id)
    assert {x.provider_instance for x in library_track.provider_mappings} == {"spotify--test"}
    # the deleted file's audio analysis must not be reused by a new file at the same path
    assert not await mass.streams.audio_analysis.database.get_row(AA_TABLE_ANALYSIS, analysis_row)


INSTANCE_ID = "filesystem_local--test"
RELEASE_MBID = "5f4e0a1c-0000-4000-8000-0000000000a1"
OLD = "Tchaikovsky"
NEW = "Pyotr Ilyich Tchaikovsky"


def _fs_mapping(item_id: str, instance_id: str = INSTANCE_ID) -> set[ProviderMapping]:
    """Return a single provider mapping for the given item id."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=instance_id.split("--", maxsplit=1)[0],
            provider_instance=instance_id,
        )
    }


def _fs_artist(name: str) -> Artist:
    """Return an artist as the filesystem provider reports it, without a MusicBrainz id."""
    return Artist(
        item_id=name, provider=INSTANCE_ID, name=name, provider_mappings=_fs_mapping(name)
    )


def _fs_track(folder: str, artist: str, number: int = 1, album_artist: str | None = None) -> Track:
    """Return a track file in an album folder, by default with the artist as album artist."""
    album = Album(
        item_id=folder,
        provider=INSTANCE_ID,
        name="Swan Lake",
        provider_mappings=_fs_mapping(folder),
        artists=UniqueList([_fs_artist(album_artist or artist)]),
    )
    album.mbid = RELEASE_MBID
    return Track(
        item_id=f"{folder}/{number:02d}.flac",
        provider=INSTANCE_ID,
        name=f"Scene {number}",
        duration=200,
        track_number=number,
        external_ids={(ExternalID.MB_RECORDING, f"5f4e0a1c-0000-4000-8000-{number:012d}")},
        provider_mappings=_fs_mapping(f"{folder}/{number:02d}.flac"),
        artists=UniqueList([_fs_artist(artist)]),
        album=album,
    )


def _sync_provider(
    mass: MusicAssistant, base_path: Path, files_on_disk: list[Track]
) -> LocalFileSystemProvider:
    """Return a provider on a real folder layout that reads the given tracks from disk."""
    provider, _ = _create_provider()
    provider.mass = mass
    provider.base_path = str(base_path)
    for track in files_on_disk:
        file_path = base_path / track.item_id
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
    by_path = {track.item_id: track for track in files_on_disk}
    provider.get_track = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda path: by_path[path]
    )
    return provider


async def _add(mass: MusicAssistant, *tracks: Track) -> list[str]:
    """Add the tracks the way a sync adds new files and return their library ids."""
    return [(await mass.music.tracks.add_item_to_library(track)).item_id for track in tracks]


async def _track_artists(mass: MusicAssistant, track_id: str) -> set[str]:
    """Return the names of the artists of a library track."""
    return {a.name for a in (await mass.music.tracks.get_library_item(track_id)).artists}


async def _album(mass: MusicAssistant, track_id: str) -> tuple[set[str], set[str]]:
    """Return the artist names and mapped folders of the album of a library track."""
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    return {a.name for a in album.artists}, {m.item_id for m in album.provider_mappings}


async def test_renamed_album_drops_the_old_artist(mass: MusicAssistant, tmp_path: Path) -> None:
    """A whole album moved to a new folder used to keep the old artist and folder."""
    moved = [_fs_track(NEW, NEW, 1), _fs_track(NEW, NEW, 2)]
    provider = _sync_provider(mass, tmp_path, moved)
    old_ids = await _add(mass, _fs_track(OLD, OLD, 1), _fs_track(OLD, OLD, 2))
    assert await _add(mass, *moved) == old_ids

    await provider._process_deletions({f"{OLD}/01.flac", f"{OLD}/02.flac"})
    await provider._process_orphaned_albums_and_artists()

    for track_id in old_ids:
        assert await _track_artists(mass, track_id) == {NEW}
        assert await _album(mass, track_id) == ({NEW}, {NEW})
    rows = await mass.music.database.get_rows_from_query("SELECT name FROM artists")
    assert {row["name"] for row in rows} == {NEW}


async def test_renamed_album_keeps_the_album_artists_of_every_file(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """Files of one folder can name different album artists, and the album keeps all of them."""
    other = "Mariinsky Orchestra"
    moved = [_fs_track(NEW, NEW, 1), _fs_track(NEW, NEW, 2, album_artist=other)]
    provider = _sync_provider(mass, tmp_path, moved)
    old_ids = await _add(mass, _fs_track(OLD, OLD, 1), _fs_track(OLD, OLD, 2))
    await _add(mass, *moved)

    await provider._process_deletions({f"{OLD}/01.flac", f"{OLD}/02.flac"})

    assert await _album(mass, old_ids[0]) == ({NEW, other}, {NEW})


async def test_partly_moved_album_keeps_both_folders(mass: MusicAssistant, tmp_path: Path) -> None:
    """A track still in the old folder keeps that folder and its artist on the album."""
    moved = _fs_track(NEW, NEW, 1)
    provider = _sync_provider(mass, tmp_path, [moved, _fs_track(OLD, OLD, 2)])
    track_id, _ = await _add(mass, _fs_track(OLD, OLD, 1), _fs_track(OLD, OLD, 2))
    await _add(mass, moved)

    await provider._process_deletions({f"{OLD}/01.flac"})

    assert await _track_artists(mass, track_id) == {NEW}
    assert await _album(mass, track_id) == ({OLD, NEW}, {OLD, NEW})


async def test_album_of_another_provider_is_left_alone(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """The album data may have come from the other provider, so it stays merged."""
    moved = _fs_track(NEW, NEW, 1)
    provider = _sync_provider(mass, tmp_path, [moved])
    (track_id,) = await _add(mass, _fs_track(OLD, OLD, 1))
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    await mass.music.albums.add_provider_mappings(album.item_id, _fs_mapping("a1", "spotify--x"))
    await _add(mass, moved)

    await provider._process_deletions({f"{OLD}/01.flac"})

    assert await _track_artists(mass, track_id) == {NEW}
    assert (await _album(mass, track_id))[0] == {OLD, NEW}


async def test_two_files_left_keep_both_artists(mass: MusicAssistant, tmp_path: Path) -> None:
    """With two copies still on disk, neither copy's artists are stale."""
    copies = [_fs_track(NEW, NEW, 1), _fs_track("Copy", NEW, 1)]
    provider = _sync_provider(mass, tmp_path, copies)
    (track_id,) = await _add(mass, _fs_track(OLD, OLD, 1))
    await _add(mass, *copies)

    await provider._process_deletions({f"{OLD}/01.flac"})

    cast("AsyncMock", provider.get_track).assert_not_called()
    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_track_with_all_files_deleted_is_removed(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """Deleting both files of one track in the same pass removes it and the pass completes."""
    provider = _sync_provider(mass, tmp_path, [])
    (track_id,) = await _add(mass, _fs_track(OLD, OLD, 1))
    await _add(mass, _fs_track(NEW, NEW, 1))

    await provider._process_deletions({f"{OLD}/01.flac", f"{NEW}/01.flac"})

    cast("AsyncMock", provider.get_track).assert_not_called()
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_id)


@pytest.mark.parametrize("error", [MediaNotFoundError("gone"), OSError("share went away")])
async def test_unreadable_file_keeps_the_stored_data(
    mass: MusicAssistant, tmp_path: Path, error: Exception
) -> None:
    """A file that can not be read again leaves the track as stored and the pass completes."""
    provider = _sync_provider(mass, tmp_path, [])
    provider.get_track = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    (track_id,) = await _add(mass, _fs_track(OLD, OLD, 1))
    await _add(mass, _fs_track(NEW, NEW, 1))

    await provider._process_deletions({f"{OLD}/01.flac"})

    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_unexpected_error_is_not_hidden(mass: MusicAssistant, tmp_path: Path) -> None:
    """Only a file that can not be read is skipped, anything else stops the pass."""
    provider = _sync_provider(mass, tmp_path, [])
    provider.get_track = AsyncMock(side_effect=RuntimeError("bug"))  # type: ignore[method-assign]
    await _add(mass, _fs_track(OLD, OLD, 1), _fs_track(NEW, NEW, 1))

    with pytest.raises(RuntimeError):
        await provider._process_deletions({f"{OLD}/01.flac"})
