"""Tests for applying a changed artist.nfo during the library sync of a Local files source."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.tags import AudioTags
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--music"
PARSE_TAGS_TARGET = "music_assistant.providers.filesystem_local.async_parse_tags"


def _write_artist_nfo(folder: Path, biography: str, mtime: int) -> None:
    """
    Write an artist.nfo with only the given biography, as a user editing it would.

    :param folder: The artist folder.
    :param biography: The biography to write.
    :param mtime: The modification time to give the file.
    """
    nfo = folder / "artist.nfo"
    nfo.write_text(f"<artist><biography>{biography}</biography></artist>", encoding="utf-8")
    os.utime(nfo, (mtime, mtime))


async def _parse(path: str, _size: int | None = None, **_kwargs: Any) -> AudioTags:
    """
    Return the tags of a dummy track, its artist and album taken from its folders.

    :param path: The absolute path of the file.
    """
    file = Path(path)
    artist = file.parent.parent.name
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=128,
        duration=180.0,
        tags={
            "title": file.stem,
            "artist": artist,
            "albumartist": artist,
            "album": file.parent.name,
        },
        has_cover_image=False,
        filename=file.name,
    )


async def _load_provider(mass: MusicAssistant, folder: Path) -> LocalFileSystemProvider:
    """
    Load a Local files music source on the given folder.

    :param mass: The server to load the source on.
    :param folder: The folder of the source.
    """
    mass.config.set(
        f"{CONF_PROVIDERS}/{INSTANCE_ID}",
        {
            "type": "music",
            "domain": "filesystem_local",
            "instance_id": INSTANCE_ID,
            "enabled": True,
            "values": {},
            "setup_data": {"path": mass.config.encrypt_string(str(folder))},
        },
    )
    await mass.load_provider(INSTANCE_ID)
    return cast("LocalFileSystemProvider", mass.get_provider(INSTANCE_ID))


async def _sync(provider: LocalFileSystemProvider) -> None:
    """
    Run a library sync of the given source.

    :param provider: The source to sync.
    """
    with patch(PARSE_TAGS_TARGET, new=AsyncMock(side_effect=_parse)):
        await provider.sync_library(MediaType.TRACK)


async def test_an_edited_artist_nfo_updates_its_artist(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """The next sync after an artist.nfo edit stores the new details on its artist."""
    artist_folder = tmp_path / "Queen"
    album_folder = artist_folder / "A Night at the Opera"
    album_folder.mkdir(parents=True)
    (album_folder / "Love of My Life.mp3").write_bytes(b"dummy audio")
    _write_artist_nfo(artist_folder, "Bio A", 1_900_000_000)
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    artist = await mass.music.artists.get_library_item_by_prov_id("Queen", INSTANCE_ID)
    assert artist is not None
    assert artist.metadata.description == "Bio A"

    _write_artist_nfo(artist_folder, "Bio B", 2_000_000_000)
    await _sync(provider)

    artist = await mass.music.artists.get_library_item(artist.item_id)
    assert artist.metadata.description == "Bio B"


async def test_an_edited_artist_nfo_keeps_the_stored_artist_name(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """An artist.nfo edit does not rename the artist to the spelling of its track's tags."""
    artist_folder = tmp_path / "Queen"
    album_folder = artist_folder / "A Night at the Opera"
    album_folder.mkdir(parents=True)
    (album_folder / "Love of My Life.mp3").write_bytes(b"dummy audio")
    _write_artist_nfo(artist_folder, "Bio A", 1_900_000_000)
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    artist = await mass.music.artists.get_library_item_by_prov_id("Queen", INSTANCE_ID)
    assert artist is not None
    # another album spelled the name differently and that spelling was stored first
    await mass.music.database.update(
        "artists", {"item_id": int(artist.item_id)}, {"name": "Queen (UK)"}
    )

    _write_artist_nfo(artist_folder, "Bio B", 2_000_000_000)
    await _sync(provider)

    artist = await mass.music.artists.get_library_item(artist.item_id)
    assert artist.name == "Queen (UK)"
    assert artist.metadata.description == "Bio B"


async def test_a_sync_without_changes_leaves_the_artist_alone(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A sync that finds no changed artist.nfo does not rewrite its artist."""
    artist_folder = tmp_path / "Queen"
    album_folder = artist_folder / "A Night at the Opera"
    album_folder.mkdir(parents=True)
    (album_folder / "Love of My Life.mp3").write_bytes(b"dummy audio")
    _write_artist_nfo(artist_folder, "Bio A", 1_900_000_000)
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    artists = mass.music.artists

    with patch.object(
        artists, "update_item_in_library", wraps=artists.update_item_in_library
    ) as update_item_in_library:
        await _sync(provider)

    update_item_in_library.assert_not_awaited()


async def test_an_edited_artist_nfo_keeps_the_artists_other_folders(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """An artist.nfo edit keeps the links of the artist's other folders on the same source."""
    for genre, album in (("Rock", "A Night at the Opera"), ("Jazz", "Innuendo")):
        album_folder = tmp_path / genre / "Queen" / album
        album_folder.mkdir(parents=True)
        (album_folder / "Track.mp3").write_bytes(b"dummy audio")
    _write_artist_nfo(tmp_path / "Rock" / "Queen", "Bio A", 1_900_000_000)
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    artist = await mass.music.artists.get_library_item_by_prov_id("Rock/Queen", INSTANCE_ID)
    assert artist is not None
    assert {mapping.item_id for mapping in artist.provider_mappings} == {
        "Rock/Queen",
        "Jazz/Queen",
    }

    _write_artist_nfo(tmp_path / "Rock" / "Queen", "Bio B", 2_000_000_000)
    await _sync(provider)

    artist = await mass.music.artists.get_library_item(artist.item_id)
    assert {mapping.item_id for mapping in artist.provider_mappings} == {
        "Rock/Queen",
        "Jazz/Queen",
    }
    assert artist.metadata.description == "Bio B"
