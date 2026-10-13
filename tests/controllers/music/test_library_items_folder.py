"""Tests for the `folder` filter on tracks library_items()."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

FS = "filesystem_local--music"
FS_OTHER = "filesystem_local--other"


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance: these tests only touch the library."""
    return music_mass


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.split("--", maxsplit=1)[0],
        provider_instance=provider_instance,
        in_library=True,
    )


async def _add_track(mass: MusicAssistant, provider_instance: str, path: str) -> Track:
    """Add a library track backed by a file at the given relative path."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=f"{path} Artist",
            provider_mappings={_mapping(provider_instance, f"{path}-artist")},
        )
    )
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=path,
            provider_mappings={_mapping(provider_instance, path)},
            artists=UniqueList([artist]),
        )
    )


@pytest.fixture(name="library")
async def library_fixture(mass: MusicAssistant) -> None:
    """Fill the library with tracks spread over a few folders."""
    for path in (
        "Rock/a.mp3",
        "Rock/Classic/b.flac",
        "Rock/Classic/Live/c.flac",
        "Rockabilly/d.mp3",
        "rock/e.mp3",
        "Jazz/f.mp3",
        "g.mp3",
    ):
        await _add_track(mass, FS, path)
    await _add_track(mass, FS_OTHER, "Rock/h.mp3")


@pytest.mark.usefixtures("library")
@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        # recursive, but neither a sibling folder sharing the prefix nor a different case
        (f"{FS}://Rock", {"Rock/a.mp3", "Rock/Classic/b.flac", "Rock/Classic/Live/c.flac"}),
        (f"{FS}://Rock/", {"Rock/a.mp3", "Rock/Classic/b.flac", "Rock/Classic/Live/c.flac"}),
        (f"{FS}://Rock/Classic", {"Rock/Classic/b.flac", "Rock/Classic/Live/c.flac"}),
        ([f"{FS}://Jazz", f"{FS_OTHER}://Rock"], {"Jazz/f.mp3", "Rock/h.mp3"}),
        (f"{FS_OTHER}://", {"Rock/h.mp3"}),
        (f"{FS}://Missing", set()),
    ],
)
async def test_folder_filter(
    mass: MusicAssistant, folder: str | list[str], expected: set[str]
) -> None:
    """Only tracks located (recursively) in the given folder(s) are returned."""
    for sort_field in (None, SortField.RANDOM):
        result = await mass.music.tracks.library_items(folder=folder, sort_field=sort_field)
        assert {t.name for t in result} == expected


@pytest.mark.usefixtures("library")
async def test_folder_filter_root_includes_all_tracks_of_provider(mass: MusicAssistant) -> None:
    """The root folder of a provider matches every track of that provider."""
    result = await mass.music.tracks.library_items(folder=f"{FS}://")
    assert len(result) == 7


@pytest.mark.parametrize("folder", ["Rock", "://Rock", "spotify--x://", "", []])
async def test_folder_filter_rejects_non_filesystem_folder(
    mass: MusicAssistant, folder: str | list[str]
) -> None:
    """A folder must live on a filesystem provider instance."""
    with pytest.raises(InvalidDataError):
        await mass.music.tracks.library_items(folder=folder)


async def test_folder_filter_ignores_folders_on_hidden_sources(mass: MusicAssistant) -> None:
    """A folder on a source hidden from the user must not match via another mapping."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Artist",
            provider_mappings={_mapping(FS, "artist")},
        )
    )
    # one track, merged from a file on each source
    await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name="shared",
            provider_mappings={_mapping(FS, "Public/x.mp3"), _mapping(FS_OTHER, "Private/x.mp3")},
            artists=UniqueList([artist]),
        )
    )
    set_music_source_access(
        mass,
        {
            FS: ProviderAccess(owner="user-a", sharing=ProviderSharing.PRIVATE),
            FS_OTHER: ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )
    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=User(user_id="user-a", username="user-a", role=UserRole.USER),
    ):
        result = await mass.music.tracks.library_items(folder=f"{FS_OTHER}://Private")
        assert result == []
        result = await mass.music.tracks.library_items(
            folder=[f"{FS}://Missing", f"{FS_OTHER}://Private"]
        )
        assert result == []
        result = await mass.music.tracks.library_items(folder=f"{FS}://Public")
        assert [t.name for t in result] == ["shared"]
    # a background refresh carries the user's sources as provider filter instead of a user
    result = await mass.music.tracks.library_items(folder=f"{FS_OTHER}://Private", provider=[FS])
    assert result == []
