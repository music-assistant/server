"""Tests for the library sync of a Local files source with audiobooks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.constants import CONF_PROVIDERS, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.helpers.tags import AudioTags
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_AUTHOR_NARRATOR_REPARSE_DONE,
    CONF_CHAPTER_FOLDER_REPARSE_DONE,
)

INSTANCE_ID = "filesystem_local--audiobooks"
PARSE_TAGS_TARGET = "music_assistant.providers.filesystem_local.async_parse_tags"
CHAPTER_DURATION = 42.0


def _write_files(folder: Path, *names: str) -> None:
    """
    Create dummy audio files; their tags are supplied by the parse spy.

    :param folder: The folder to write the files in.
    :param names: The file names.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"dummy audio")


def _change_file(file: Path) -> None:
    """
    Change a file on disk, as retagging it would.

    :param file: The file to change.
    """
    file.write_bytes(b"retagged dummy audio")
    os.utime(file, (2_000_000_000, 2_000_000_000))


async def _parse(path: str, _size: int | None = None) -> AudioTags:
    """
    Return the tags of a dummy file, derived from its name.

    An ``.m4b`` file is a book with embedded chapters, a ``partN`` file is chapter N of the
    book named after its folder, any other file is an untagged file of that book. A ``partN``
    file with embedded chapters is an ``.m4b`` named ``partN``.

    :param path: The absolute path of the file.
    """
    file = Path(path)
    tags: dict[str, Any] = {"title": file.stem}
    raw: dict[str, Any] = {}
    if file.suffix == ".m4b" and not file.stem.startswith("part"):
        tags["album"] = file.stem
    else:
        tags["album"] = file.parent.name
        if file.stem.startswith("part"):
            tags["track"] = file.stem.removeprefix("part")
    if file.suffix == ".m4b":
        raw["chapters"] = [{"id": 1, "start_time": "0", "end_time": str(CHAPTER_DURATION)}]
    return AudioTags(
        raw=raw,
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=128,
        duration=CHAPTER_DURATION,
        tags=tags,
        has_cover_image=False,
        filename=file.name,
    )


async def _load_provider(mass: MusicAssistant, folder: Path) -> LocalFileSystemProvider:
    """
    Load a Local files source with audiobooks on the given folder.

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
            "setup_data": {
                "path": mass.config.encrypt_string(str(folder)),
                "content_type": "audiobooks",
            },
        },
    )
    await mass.load_provider(INSTANCE_ID)
    return cast("LocalFileSystemProvider", mass.get_provider(INSTANCE_ID))


async def _sync(provider: LocalFileSystemProvider) -> tuple[list[str], list[str]]:
    """
    Run a library sync and return the files it read and the books it wrote to the library.

    :param provider: The source to sync.
    """
    audiobooks = provider.mass.music.audiobooks
    with (
        patch(PARSE_TAGS_TARGET, new=AsyncMock(side_effect=_parse)) as parse_tags,
        patch.object(
            audiobooks, "add_item_to_library", wraps=audiobooks.add_item_to_library
        ) as add_item_to_library,
    ):
        await provider.sync_library(MediaType.AUDIOBOOK)
    read = {
        Path(call.args[0]).relative_to(provider.base_path) for call in parse_tags.await_args_list
    }
    added = [call.args[0].item_id for call in add_item_to_library.await_args_list]
    return sorted(str(path) for path in read), sorted(added)


async def _library_mappings(mass: MusicAssistant) -> list[str]:
    """
    Return the files the audiobooks in the library are mapped to.

    :param mass: The server that holds the library.
    """
    return sorted(
        mapping.item_id
        for audiobook in await mass.music.audiobooks.library_items()
        for mapping in audiobook.provider_mappings
    )


async def test_a_second_sync_reads_no_chapter_file(mass: MusicAssistant, tmp_path: Path) -> None:
    """A sync without changes on disk reads no file and writes no book."""
    _write_files(tmp_path / "Author" / "Book", "part1.mp3", "part2.mp3", "part3.mp3")
    _write_files(tmp_path / "Author", "Single A.m4b", "Single B.m4b")
    provider = await _load_provider(mass, tmp_path)

    _, added = await _sync(provider)
    assert added == ["Author/Book/part1.mp3", "Author/Single A.m4b", "Author/Single B.m4b"]

    read, added = await _sync(provider)
    assert read == []
    assert added == []
    assert await _library_mappings(mass) == [
        "Author/Book/part1.mp3",
        "Author/Single A.m4b",
        "Author/Single B.m4b",
    ]


async def test_a_changed_chapter_refreshes_its_audiobook(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A changed or added chapter file makes the next sync refresh its own book only."""
    book = tmp_path / "Author" / "Book"
    _write_files(book, "part1.mp3", "part2.mp3", "part3.mp3")
    _write_files(tmp_path / "Author", "Single.m4b")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)

    _change_file(book / "part2.mp3")
    _write_files(book, "part4.mp3")
    _, added = await _sync(provider)

    assert added == ["Author/Book/part1.mp3"]
    audiobook = await mass.music.audiobooks.get_library_item_by_prov_id(
        "Author/Book/part1.mp3", INSTANCE_ID
    )
    assert audiobook is not None
    assert audiobook.duration == 4 * CHAPTER_DURATION
    assert len(audiobook.metadata.chapters or []) == 4


@pytest.mark.parametrize("image_name", ["cover.jpg", "Book Title.jpg"])
async def test_new_artwork_refreshes_its_audiobook(
    mass: MusicAssistant, tmp_path: Path, image_name: str
) -> None:
    """Artwork added to the folder of a multi-file book reaches the library on the next sync."""
    book = tmp_path / "Author" / "Book"
    _write_files(book, "part1.mp3", "part2.mp3")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)

    (book / image_name).write_bytes(b"dummy image")
    _, added = await _sync(provider)

    assert added == ["Author/Book/part1.mp3"]
    audiobook = await mass.music.audiobooks.get_library_item_by_prov_id(
        "Author/Book/part1.mp3", INSTANCE_ID
    )
    assert audiobook is not None
    assert audiobook.image is not None
    assert audiobook.image.path.startswith(f"Author/Book/{image_name}")


async def test_a_book_split_over_files_with_embedded_chapters_reads_no_part_again(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A book whose first part has embedded chapters still takes the other parts as chapters."""
    _write_files(tmp_path / "Author" / "Book", "part1.m4b", "part2.mp3")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)

    read, added = await _sync(provider)

    assert read == []
    assert added == []


async def test_a_refreshed_multi_file_book_is_read_once_more(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A multi-file book refreshed by hand is read once more by the next sync, then skipped."""
    _write_files(tmp_path / "Author" / "Book", "part1.mp3", "part2.mp3")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    audiobook = await mass.music.audiobooks.get_library_item_by_prov_id(
        "Author/Book/part1.mp3", INSTANCE_ID
    )
    assert audiobook is not None
    with patch(PARSE_TAGS_TARGET, new=AsyncMock(side_effect=_parse)):
        await mass.music.refresh_item(audiobook)

    _, added = await _sync(provider)
    assert added == ["Author/Book/part1.mp3"]

    read, added = await _sync(provider)
    assert read == []
    assert added == []


async def test_a_new_book_next_to_single_file_books_adds_only_that_book(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A book added to a folder of books with embedded chapters leaves the others untouched."""
    _write_files(tmp_path / "Author", "Single A.m4b", "Single B.m4b")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)

    _write_files(tmp_path / "Author", "Single C.m4b")
    _, added = await _sync(provider)

    assert added == ["Author/Single C.m4b"]


async def test_a_file_that_became_a_chapter_leaves_the_library(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A book stored for a file that is now a chapter of another book is removed."""
    book = tmp_path / "Author" / "Untagged"
    _write_files(book, "b.mp3", "c.mp3")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    assert await _library_mappings(mass) == ["Author/Untagged/b.mp3"]

    # without track tags, the first file by name holds the book
    _write_files(book, "a.mp3")
    await _sync(provider)

    assert await _library_mappings(mass) == ["Author/Untagged/a.mp3"]
    read, _ = await _sync(provider)
    assert read == []


async def test_a_multi_file_book_from_an_earlier_version_is_read_once(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A multi-file book stored before folder signatures existed is read once more, then skipped."""
    book = tmp_path / "Author" / "Book"
    _write_files(book, "part1.mp3", "part2.mp3")
    _write_files(tmp_path / "Author", "Single.m4b")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    # what an install that synced before folder signatures existed has stored
    await mass.music.database.update(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"provider_instance": INSTANCE_ID, "provider_item_id": "Author/Book/part1.mp3"},
        {"details": str(int((book / "part1.mp3").stat().st_mtime))},
    )
    mass.config.set_raw_provider_config_value(INSTANCE_ID, CONF_CHAPTER_FOLDER_REPARSE_DONE, False)

    _, added = await _sync(provider)
    assert added == ["Author/Book/part1.mp3"]

    read, added = await _sync(provider)
    assert read == []
    assert added == []


async def test_a_forced_reparse_reads_every_book_again(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """The one-time reparse that promotes authors and narrators to artists reads every book."""
    _write_files(tmp_path / "Author" / "Book", "part1.mp3", "part2.mp3")
    _write_files(tmp_path / "Author", "Single.m4b")
    provider = await _load_provider(mass, tmp_path)
    await _sync(provider)
    mass.config.set_raw_provider_config_value(INSTANCE_ID, CONF_AUTHOR_NARRATOR_REPARSE_DONE, False)

    _, added = await _sync(provider)

    assert added == ["Author/Book/part1.mp3", "Author/Single.m4b"]
