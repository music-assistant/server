"""Tests for the beets provider setup, getters, streaming and images."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import Album
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers import beets
from music_assistant.providers.beets import BeetsProvider
from music_assistant.providers.beets.library import BeetsLibraryError
from music_assistant.providers.beets.setup_flow import run_setup
from tests.providers.beets.beets_db import ARTIST_MBID, BeetsDb, album_fields, item_fields
from tests.providers.beets.conftest import INSTANCE_ID

MakeProvider = Callable[..., Awaitable[BeetsProvider]]


async def test_setup_fails_for_missing_database(
    make_provider: MakeProvider, tmp_path: Path
) -> None:
    """A missing library.db is reported with its own translation key."""
    provider = await make_provider(db_path=tmp_path / "missing.db", open_library=False)
    with pytest.raises(SetupFailedError) as err:
        await provider.handle_async_init()
    assert err.value.translation_key == "library_db_not_found"


async def test_setup_fails_for_non_beets_database(
    make_provider: MakeProvider, tmp_path: Path
) -> None:
    """A SQLite file without beets tables is reported as invalid."""
    db_path = tmp_path / "other.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    provider = await make_provider(db_path=db_path, open_library=False)
    with pytest.raises(SetupFailedError) as err:
        await provider.handle_async_init()
    assert err.value.translation_key == "library_db_invalid"


async def test_setup_fails_for_missing_music_directory(
    make_provider: MakeProvider, music_dir: Path
) -> None:
    """A missing music directory fails setup and leaves the library closed."""
    provider = await make_provider(open_library=False)
    music_dir.rmdir()
    with pytest.raises(SetupFailedError) as err:
        await provider.handle_async_init()
    assert err.value.translation_key == "music_directory_not_found"
    with pytest.raises(BeetsLibraryError):
        await provider.library.count_items()


async def test_setup_opens_library_and_reads_threshold(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A valid setup opens the library and applies the configured rating threshold."""
    beets_db.add_item(**item_fields())
    provider = await make_provider(open_library=False)
    provider.config.get_value = MagicMock(return_value=0.5)  # type: ignore[method-assign]
    await provider.handle_async_init()
    assert await provider.library.count_items() == 1
    assert provider._ctx.favorite_rating_threshold == 0.5


async def test_config_entries_offer_rating_threshold(make_provider: MakeProvider) -> None:
    """The only runtime option is the favorite rating threshold."""
    provider = await make_provider()
    assert [entry.key for entry in await provider.get_config_entries()] == [
        "favorite_rating_threshold"
    ]


async def test_getters_read_beets(make_provider: MakeProvider, beets_db: BeetsDb) -> None:
    """Tracks, albums, album tracks and artists are read from beets on demand."""
    album_id = beets_db.add_album(**album_fields())
    second = beets_db.add_item(**item_fields(album_id=album_id, track=2, title="Second"))
    first = beets_db.add_item(**item_fields(album_id=album_id, track=1, title="First"))
    provider = await make_provider()

    track = await provider.get_track(str(first))
    assert track.name == "First"
    assert isinstance(track.album, Album)
    assert track.album.item_id == str(album_id)
    assert (await provider.get_album(str(album_id))).name == "Album"
    album_tracks = await provider.get_album_tracks(str(album_id))
    assert [album_track.item_id for album_track in album_tracks] == [str(first), str(second)]
    assert (await provider.get_artist("Artist")).mbid == ARTIST_MBID


@pytest.mark.parametrize("prov_id", ["9999", "not-a-number"])
async def test_unknown_ids_raise_media_not_found(make_provider: MakeProvider, prov_id: str) -> None:
    """Unknown or malformed ids surface as MediaNotFoundError."""
    provider = await make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_track(prov_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album(prov_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album_tracks(prov_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_artist("Nobody")


async def test_stream_details_for_existing_file(
    make_provider: MakeProvider, beets_db: BeetsDb, music_dir: Path
) -> None:
    """A track streams as a local file from the expanded beets path."""
    audio = music_dir / "Artist" / "Album" / "01 Song.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fLaC" + bytes(16))
    item_id = beets_db.add_item(**item_fields())
    provider = await make_provider()

    details = await provider.get_stream_details(str(item_id), MediaType.TRACK)

    assert details.stream_type == StreamType.LOCAL_FILE
    assert details.path == str(audio)
    assert details.size == 20
    assert details.duration == 215
    assert details.audio_format.content_type == ContentType.FLAC


async def test_stream_details_for_missing_file(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A track whose file is gone raises MediaNotFoundError."""
    item_id = beets_db.add_item(**item_fields())
    provider = await make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(str(item_id), MediaType.TRACK)


async def test_resolve_image_returns_album_art(
    make_provider: MakeProvider, beets_db: BeetsDb, music_dir: Path
) -> None:
    """An album image path resolves to the album's artpath on disk."""
    art = music_dir / "Artist" / "Album" / "cover.jpg"
    art.parent.mkdir(parents=True)
    art.write_bytes(b"jpg")
    album_id = beets_db.add_album(**album_fields(artpath=b"Artist/Album/cover.jpg"))
    provider = await make_provider()
    assert await provider.resolve_image(f"album/{album_id}?cs=abc") == str(art)


async def test_resolve_image_with_missing_art_file(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """An artpath pointing at a removed file raises MediaNotFoundError."""
    album_id = beets_db.add_album(**album_fields(artpath=b"Artist/Album/cover.jpg"))
    provider = await make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.resolve_image(f"album/{album_id}")


@pytest.mark.parametrize("path", ["album/9999", "album/nope", "/etc/passwd", "Artist/cover.jpg"])
async def test_resolve_image_rejects_other_paths(make_provider: MakeProvider, path: str) -> None:
    """Only album ids resolve; file paths are never served directly."""
    provider = await make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.resolve_image(path)


async def test_setup_flow_reprompts_with_error_then_finishes() -> None:
    """A failed finish shows its translation key on the form and the next submit finishes."""
    session = MagicMock()
    session.context.setup_data = {}
    good = {
        "library_db": "/media/beets/library.db",
        "music_directory": "/media/music",
        "beets_directory": "/home/kate/Music",
    }
    session.form = AsyncMock(side_effect=[{**good, "library_db": "/bad.db"}, good])
    session.finish = AsyncMock(
        side_effect=[
            SetupFlowError("missing", translation_key="library_db_not_found"),
            {"instance_id": INSTANCE_ID},
        ]
    )

    await run_setup(session)

    assert session.form.await_args_list[0].kwargs["errors"] is None
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "library_db_not_found"}
    assert session.finish.await_args_list[1].args[0] == good


async def test_manifest_is_not_self_service() -> None:
    """The manifest parses and keeps setup (which takes server paths) away from members."""
    manifest = await ProviderManifest.parse(str(Path(beets.__file__).parent / "manifest.json"))
    assert manifest.domain == "beets"
    assert manifest.self_service is False
