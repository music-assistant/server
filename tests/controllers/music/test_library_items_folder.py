"""Tests for the `folder` filter on tracks library_items()."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import SortField
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant

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
