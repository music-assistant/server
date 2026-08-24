"""Tests for filesystem provider artist path resolution with stale library paths."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--test"

ARTIST_FOLDER = "Simone, Nina"
ALBUM_FOLDER = "1987 Live At Ronnie Scott's"
TRACK_FILE = "12. My Baby Just Cares for Me.mp3"


def _make_tags(artist: str, album: str) -> AudioTags:
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=320,
        duration=240.0,
        tags={
            "artist": artist,
            "albumartist": artist,
            "album": album,
            "title": "My Baby Just Cares for Me",
            "track": "12",
        },
        has_cover_image=False,
        filename=os.path.join(ARTIST_FOLDER, ALBUM_FOLDER, TRACK_FILE),
    )


def _make_provider(base_path: str, lib_artists: list[Artist]) -> LocalFileSystemProvider:
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = base_path
    provider.logger = MagicMock()
    provider.write_access = False
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.config.get_value = MagicMock(return_value="various_artists")
    provider.manifest = MagicMock()
    provider.manifest.domain = "filesystem_local"
    provider.cache = MagicMock()
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock(return_value=None)

    async def iter_library_items(
        search: str | None = None,  # noqa: ARG001
        provider: str | None = None,  # noqa: ARG001
    ) -> AsyncGenerator[Artist]:
        for lib_artist in lib_artists:
            yield lib_artist

    provider.mass = MagicMock()
    provider.mass.music.artists.iter_library_items = iter_library_items
    provider.mass.get_provider = MagicMock(return_value=None)
    provider.mass.create_task = MagicMock()
    return provider


def _lib_artist(name: str, url: str | None) -> Artist:
    return Artist(
        item_id="1",
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=url or name,
                provider_domain="filesystem_local",
                provider_instance=INSTANCE_ID,
                url=url,
                in_library=True,
            )
        },
    )


@pytest.fixture
def music_tree(tmp_path: Path) -> str:
    """Create a sort-name style artist folder with one track file."""
    track_dir = tmp_path / ARTIST_FOLDER / ALBUM_FOLDER
    track_dir.mkdir(parents=True)
    (track_dir / TRACK_FILE).write_bytes(b"\x00" * 128)
    return str(tmp_path)


async def test_stale_artist_path_does_not_fail_track_parse(music_tree: str) -> None:
    """
    A stale artist path stored in the library must not fail parsing the track.

    Regression test: the artist folder was renamed from display-name style
    ("Nina Simone") to sort-name style ("Simone, Nina"), but the library still
    holds the old path in the provider mapping url. Parsing any track by that
    artist raised FileNotFoundError and aborted the sync for that track.
    """
    provider = _make_provider(music_tree, [_lib_artist("Nina Simone", "Nina Simone")])
    file_item = await provider.resolve(os.path.join(ARTIST_FOLDER, ALBUM_FOLDER, TRACK_FILE))

    track = await provider._parse_track(file_item, _make_tags("Nina Simone", ALBUM_FOLDER))

    assert [a.name for a in track.artists] == ["Nina Simone"]


async def test_valid_artist_path_still_resolved(music_tree: str) -> None:
    """A valid stored artist path keeps being used as the artist's item_id."""
    provider = _make_provider(music_tree, [_lib_artist("Nina Simone", ARTIST_FOLDER)])
    file_item = await provider.resolve(os.path.join(ARTIST_FOLDER, ALBUM_FOLDER, TRACK_FILE))

    track = await provider._parse_track(file_item, _make_tags("Nina Simone", ALBUM_FOLDER))

    assert [a.item_id for a in track.artists] == [ARTIST_FOLDER]
