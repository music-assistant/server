"""Tests for the filesystem provider's deletion pass."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

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
        {**analysis_row, "aa_provider_domain": "test", "analysis_data": "{}"},
    )

    await provider._process_deletions({file_path})

    library_track = await mass.music.tracks.get_library_item(db_track.item_id)
    assert {x.provider_instance for x in library_track.provider_mappings} == {"spotify--test"}
    # the deleted file's audio analysis must not be reused by a new file at the same path
    assert not await mass.streams.audio_analysis.database.get_row(AA_TABLE_ANALYSIS, analysis_row)
