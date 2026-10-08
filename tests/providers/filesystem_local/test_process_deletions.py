"""Tests for the filesystem provider's deletion pass."""

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
RECORDING_MBID = "5f4e0a1c-0000-4000-8000-000000000001"
RELEASE_MBID = "5f4e0a1c-0000-4000-8000-0000000000a1"
OLD_NAME = "Tchaikovsky"
NEW_NAME = "Pyotr Ilyich Tchaikovsky"


def _fs_mapping(item_id: str) -> set[ProviderMapping]:
    """Return a single mapping of the test filesystem provider."""
    return {
        ProviderMapping(
            item_id=item_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID
        )
    }


def _fs_artist(name: str) -> Artist:
    """Return an artist as the filesystem provider reports it, without a MusicBrainz id."""
    return Artist(
        item_id=name, provider=INSTANCE_ID, name=name, provider_mappings=_fs_mapping(name)
    )


def _fs_track(folder: str, artist: str) -> Track:
    """Return the first track of an album folder, tagged with the given artist."""
    path = f"{folder}/01.flac"
    album = Album(
        item_id=folder,
        provider=INSTANCE_ID,
        name="Swan Lake",
        provider_mappings=_fs_mapping(folder),
        artists=UniqueList([_fs_artist(artist)]),
    )
    album.mbid = RELEASE_MBID
    return Track(
        item_id=path,
        provider=INSTANCE_ID,
        name="Scene",
        duration=200,
        track_number=1,
        external_ids={(ExternalID.MB_RECORDING, RECORDING_MBID)},
        provider_mappings=_fs_mapping(path),
        artists=UniqueList([_fs_artist(artist)]),
        album=album,
    )


async def _add_tracks(mass: MusicAssistant, *tracks: Track) -> str:
    """Add the tracks the way a sync adds new files and return the library id they share."""
    library_ids = {(await mass.music.tracks.add_item_to_library(track)).item_id for track in tracks}
    assert len(library_ids) == 1
    return library_ids.pop()


async def _artist_names(mass: MusicAssistant, track_id: str) -> tuple[set[str], set[str]]:
    """Return the names of the artists of a library track and of its album."""
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    return {a.name for a in track.artists}, {a.name for a in album.artists}


async def test_renamed_file_drops_the_old_artist(mass: MusicAssistant) -> None:
    """The renamed file used to leave the track and album under both spellings."""
    provider, _ = _create_provider()
    provider.mass = mass
    new_track = _fs_track(NEW_NAME, NEW_NAME)
    provider.get_track = AsyncMock(return_value=new_track)  # type: ignore[method-assign]
    track_id = await _add_tracks(mass, _fs_track(OLD_NAME, OLD_NAME), new_track)

    await provider._process_deletions({f"{OLD_NAME}/01.flac"})
    await provider._process_orphaned_albums_and_artists()

    provider.get_track.assert_awaited_once_with(f"{NEW_NAME}/01.flac")
    assert await _artist_names(mass, track_id) == ({NEW_NAME}, {NEW_NAME})
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    assert {m.item_id for m in album.provider_mappings} == {NEW_NAME}
    rows = await mass.music.database.get_rows_from_query("SELECT name FROM artists")
    assert {row["name"] for row in rows} == {NEW_NAME}


async def test_two_files_left_keep_both_artists(mass: MusicAssistant) -> None:
    """With two copies still on disk, neither copy's artists are stale."""
    provider, _ = _create_provider()
    provider.mass = mass
    provider.get_track = AsyncMock()  # type: ignore[method-assign]
    track_id = await _add_tracks(
        mass,
        _fs_track(OLD_NAME, OLD_NAME),
        _fs_track(NEW_NAME, NEW_NAME),
        _fs_track("Copy", NEW_NAME),
    )

    await provider._process_deletions({f"{OLD_NAME}/01.flac"})

    provider.get_track.assert_not_called()
    assert await _artist_names(mass, track_id) == ({OLD_NAME, NEW_NAME}, {OLD_NAME, NEW_NAME})


async def test_unreadable_remaining_file_keeps_the_stored_artists(mass: MusicAssistant) -> None:
    """A file that can not be read again leaves the track as it is stored."""
    provider, _ = _create_provider()
    provider.mass = mass
    provider.get_track = AsyncMock(  # type: ignore[method-assign]
        side_effect=MediaNotFoundError("gone")
    )
    track_id = await _add_tracks(mass, _fs_track(OLD_NAME, OLD_NAME), _fs_track(NEW_NAME, NEW_NAME))

    await provider._process_deletions({f"{OLD_NAME}/01.flac"})

    assert await _artist_names(mass, track_id) == ({OLD_NAME, NEW_NAME}, {OLD_NAME, NEW_NAME})
