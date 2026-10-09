"""Tests for the library sync of a Local files source with podcasts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.constants import CONF_PROVIDERS, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.helpers.tags import AudioTags
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--podcasts"
PARSE_TAGS_TARGET = "music_assistant.providers.filesystem_local.async_parse_tags"


def _write_episodes(folder: Path, *names: str) -> None:
    """
    Create dummy episode files; their tags are supplied by the parse spy.

    :param folder: The podcast folder to write the episodes in.
    :param names: The file names of the episodes.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"dummy audio")


def _parse_tags_spy() -> AsyncMock:
    """Return an AsyncMock standing in for async_parse_tags, tagging each file by its folder."""

    async def _parse(path: str, _size: int | None = None) -> AudioTags:
        file = Path(path)
        return AudioTags(
            raw={},
            sample_rate=44100,
            channels=2,
            bits_per_sample=16,
            format="mp3",
            bit_rate=128,
            duration=42.0,
            tags={"album": file.parent.name, "title": file.stem},
            has_cover_image=False,
            filename=file.name,
        )

    return AsyncMock(side_effect=_parse)


async def _load_provider(mass: MusicAssistant, folder: Path) -> LocalFileSystemProvider:
    """
    Load a Local files source with podcasts on the given folder.

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
                "content_type": "podcasts",
            },
        },
    )
    await mass.load_provider(INSTANCE_ID)
    return cast("LocalFileSystemProvider", mass.get_provider(INSTANCE_ID))


async def _sync(provider: LocalFileSystemProvider) -> AsyncMock:
    """
    Run a library sync and return the parse spy it ran with.

    :param provider: The source to sync.
    """
    with patch(PARSE_TAGS_TARGET, new=_parse_tags_spy()) as parse_tags:
        await provider.sync_library(MediaType.PODCAST)
    return parse_tags


async def _library_podcasts(mass: MusicAssistant) -> list[str]:
    """Return the names of the podcasts in the library."""
    return sorted(podcast.name for podcast in await mass.music.podcasts.library_items())


async def _stored_signature(mass: MusicAssistant, folder: str) -> str | None:
    """
    Return the signature the last sync stored for a podcast folder.

    :param folder: The relative path of the podcast folder.
    """
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
        "WHERE provider_instance = :instance AND provider_item_id = :folder",
        {"instance": INSTANCE_ID, "folder": folder},
    )
    return rows[0]["details"] if rows else None


async def test_a_second_sync_reads_no_episodes(mass: MusicAssistant, tmp_path: Path) -> None:
    """A sync without changes on disk reads no episode file at all."""
    folder = tmp_path / "podcasts"
    _write_episodes(folder / "Show A", "ep1.mp3", "ep2.mp3")
    _write_episodes(folder / "Show B", "ep1.mp3")
    provider = await _load_provider(mass, folder)

    parse_tags = await _sync(provider)
    assert parse_tags.await_count == 3
    assert await _library_podcasts(mass) == ["Show A", "Show B"]

    parse_tags = await _sync(provider)
    parse_tags.assert_not_awaited()
    assert await _library_podcasts(mass) == ["Show A", "Show B"]


async def test_a_new_episode_resyncs_only_its_podcast(mass: MusicAssistant, tmp_path: Path) -> None:
    """A new episode makes the next sync read the episodes of its own podcast only."""
    folder = tmp_path / "podcasts"
    _write_episodes(folder / "Show A", "ep1.mp3")
    _write_episodes(folder / "Show B", "ep1.mp3")
    provider = await _load_provider(mass, folder)
    await _sync(provider)
    signature = await _stored_signature(mass, "Show A")

    _write_episodes(folder / "Show A", "ep2.mp3")
    parse_tags = await _sync(provider)

    parsed = sorted(Path(call.args[0]).relative_to(folder) for call in parse_tags.await_args_list)
    assert parsed == [Path("Show A/ep1.mp3"), Path("Show A/ep2.mp3")]
    assert await _stored_signature(mass, "Show A") not in (None, signature)
    assert await _library_podcasts(mass) == ["Show A", "Show B"]


async def test_a_deleted_podcast_folder_leaves_the_library(
    mass: MusicAssistant, tmp_path: Path
) -> None:
    """A podcast whose folder is gone is removed from the library."""
    folder = tmp_path / "podcasts"
    _write_episodes(folder / "Show A", "ep1.mp3")
    _write_episodes(folder / "Show B", "ep1.mp3")
    provider = await _load_provider(mass, folder)
    await _sync(provider)

    shutil.rmtree(folder / "Show B")
    await _sync(provider)

    assert await _library_podcasts(mass) == ["Show A"]
