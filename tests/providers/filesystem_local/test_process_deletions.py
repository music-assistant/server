"""Tests for the filesystem provider's deletion pass."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
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


FS = "filesystem_local--test"
RELEASE_MBID = "5f4e0a1c-0000-4000-8000-0000000000a1"
OLD = "Tchaikovsky"
NEW = "Pyotr Ilyich Tchaikovsky"


def _fs_mapping(item_id: str, url: str | None = None) -> set[ProviderMapping]:
    """Return a single filesystem provider mapping for the given item id."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="filesystem_local",
            provider_instance=FS,
            url=url,
            in_library=True,
        )
    }


def _fs_artist(name: str) -> Artist:
    """Return an artist as the filesystem provider reports it, without a MusicBrainz id."""
    return Artist(item_id=name, provider=FS, name=name, provider_mappings=_fs_mapping(name))


def _fs_track(folder: str, artist: str, number: int = 1) -> Track:
    """Return a track file in an album folder, with the artist as album artist."""
    album = Album(
        item_id=folder,
        provider=FS,
        name="Swan Lake",
        provider_mappings=_fs_mapping(folder, url=folder),
        artists=UniqueList([_fs_artist(artist)]),
    )
    album.mbid = RELEASE_MBID
    return Track(
        item_id=f"{folder}/{number:02d}.flac",
        provider=FS,
        name=f"Scene {number}",
        duration=200,
        track_number=number,
        external_ids={(ExternalID.MB_RECORDING, f"5f4e0a1c-0000-4000-8000-{number:012d}")},
        provider_mappings=_fs_mapping(f"{folder}/{number:02d}.flac"),
        artists=UniqueList([_fs_artist(artist)]),
        album=album,
    )


def _disk_provider(
    mass: MusicAssistant, base_path: Path, files_on_disk: list[Track]
) -> LocalFileSystemProvider:
    """Return a provider on a real folder layout whose files parse as the given tracks."""
    provider, _ = _create_provider()
    provider.mass = mass
    provider.base_path = str(base_path)
    provider.available = True
    provider._is_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]
    for track in files_on_disk:
        file_path = base_path / track.item_id
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
    by_path = {track.item_id: track for track in files_on_disk}
    provider.get_track = AsyncMock(side_effect=lambda path: by_path[path])  # type: ignore[method-assign]
    provider._parse_track = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda file_item, _tags: by_path[file_item.relative_path]
    )
    return provider


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
async def test_renamed_album_drops_the_old_artist(
    mass: MusicAssistant, tmp_path: Path, order: tuple[int, int]
) -> None:
    """A whole album moved and retagged leaves only the new artist and folder in the library."""
    moved = [_fs_track(NEW, NEW, 1), _fs_track(NEW, NEW, 2)]
    provider = _disk_provider(mass, tmp_path, moved)
    old_ids = [
        (await mass.music.tracks.add_item_to_library(track)).item_id
        for track in (_fs_track(OLD, OLD, 1), _fs_track(OLD, OLD, 2), *moved)
    ][:2]

    with (
        patch.object(mass, "get_provider", return_value=provider),
        patch("music_assistant.providers.filesystem_local.async_parse_tags", AsyncMock()),
    ):
        # one file per pass, so both orders are covered whatever the set iteration order
        for number in order:
            await provider._process_deletions({f"{OLD}/{number:02d}.flac"})
            await provider._process_orphaned_albums_and_artists()

    for track_id in old_ids:
        track = await mass.music.tracks.get_library_item(track_id)
        assert {x.name for x in track.artists} == {NEW}
        assert track.album
        album = await mass.music.albums.get_library_item(track.album.item_id)
        assert {x.name for x in album.artists} == {NEW}
        assert {x.item_id for x in album.provider_mappings} == {NEW}
    rows = await mass.music.database.get_rows_from_query("SELECT name FROM artists")
    assert {row["name"] for row in rows} == {NEW}


async def test_get_album_of_a_removed_folder_is_not_found(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A library album whose folder is gone is reported as not found."""
    provider = _disk_provider(mass, tmp_path, [])
    await mass.music.tracks.add_item_to_library(_fs_track(OLD, OLD, 1))

    with pytest.raises(MediaNotFoundError):
        await provider.get_album(OLD)


async def test_get_album_on_unreachable_storage_is_unavailable(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A folder that can not be seen because the storage is offline is not reported gone."""
    provider = _disk_provider(mass, tmp_path, [])
    provider._is_reachable = AsyncMock(return_value=False)  # type: ignore[method-assign]
    await mass.music.tracks.add_item_to_library(_fs_track(OLD, OLD, 1))

    with pytest.raises(ProviderUnavailableError):
        await provider.get_album(OLD)


async def test_get_album_without_a_folder_skips_the_folder_check(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """An album built from tags alone has no folder, so it is never reported gone for that."""
    track = _fs_track(OLD, OLD, 1)
    assert isinstance(track.album, Album)
    track.album.provider_mappings = _fs_mapping(f"{OLD}/Swan Lake")
    track.album.item_id = f"{OLD}/Swan Lake"
    provider = _disk_provider(mass, tmp_path, [track])
    await mass.music.tracks.add_item_to_library(track)

    with patch("music_assistant.providers.filesystem_local.async_parse_tags", AsyncMock()):
        album = await provider.get_album(f"{OLD}/Swan Lake")

    assert album.name == "Swan Lake"
