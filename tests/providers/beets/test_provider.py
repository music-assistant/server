"""Tests for the beets provider setup, getters, streaming and images."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
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
from tests.providers.beets.beets_db import (
    ARTIST_MBID,
    GUEST_MBID,
    MULTI_VALUE_DELIMITER,
    BeetsDb,
    album_fields,
    item_fields,
)
from tests.providers.beets.conftest import INSTANCE_ID, album_prov_id, track_prov_id

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


async def test_mock_mass_accepts_library_calls(make_provider: MakeProvider) -> None:
    """The mocked add_item_to_library accepts the keyword call sync code makes."""
    provider = await make_provider()
    result: Any = await provider.mass.music.tracks.add_item_to_library(
        MagicMock(), overwrite_existing=True
    )
    assert result.item_id == 1
    assert result.favorite is False


async def test_getters_read_beets(make_provider: MakeProvider, beets_db: BeetsDb) -> None:
    """Tracks, albums, album tracks and artists are read from beets on demand."""
    album_id = beets_db.add_album(**album_fields())
    second = beets_db.add_item(**item_fields(album_id=album_id, track=2, title="Second"))
    first = beets_db.add_item(**item_fields(album_id=album_id, track=1, title="First"))
    provider = await make_provider()

    track = await provider.get_track(track_prov_id(first))
    assert track.name == "First"
    assert isinstance(track.album, Album)
    assert track.album.item_id == album_prov_id(album_id)
    assert (await provider.get_album(album_prov_id(album_id))).name == "Album"
    album_tracks = await provider.get_album_tracks(album_prov_id(album_id))
    assert [album_track.item_id for album_track in album_tracks] == [
        track_prov_id(first),
        track_prov_id(second),
    ]
    assert (await provider.get_artist("Artist")).mbid == ARTIST_MBID


async def test_get_artist_finds_featured_artist(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A name that only appears in the multi-valued artists list still resolves."""
    beets_db.add_item(**item_fields())
    provider = await make_provider()
    assert (await provider.get_artist("Guest")).mbid == GUEST_MBID


async def test_get_artist_finds_featured_artist_after_many_substring_matches(
    make_provider: MakeProvider, beets_db: BeetsDb
) -> None:
    """A featured artist whose name is a substring of many earlier rows still resolves."""
    for index in range(25):
        beets_db.add_item(
            **item_fields(
                title=f"Decoy {index}",
                artist="Moby",
                artist_sort="Moby",
                artists=f"Moby{MULTI_VALUE_DELIMITER}Lemon Demon",
                artists_sort=f"Moby{MULTI_VALUE_DELIMITER}Lemon Demon",
            )
        )
    beets_db.add_item(
        **item_fields(
            artist="Artist feat. Mo",
            artists=f"Artist{MULTI_VALUE_DELIMITER}Mo",
            artists_sort=f"Artist{MULTI_VALUE_DELIMITER}Mo",
        )
    )
    provider = await make_provider()
    assert (await provider.get_artist("Mo")).mbid == GUEST_MBID


@pytest.mark.parametrize(
    ("prov_track_id", "prov_album_id"),
    [(track_prov_id(9999), album_prov_id(9999)), ("not-a-number", "not-a-number")],
)
async def test_unknown_ids_raise_media_not_found(
    make_provider: MakeProvider, prov_track_id: str, prov_album_id: str
) -> None:
    """Unknown or malformed ids surface as MediaNotFoundError."""
    provider = await make_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_track(prov_track_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album(prov_album_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album_tracks(prov_album_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_artist("Nobody")


@pytest.mark.parametrize(
    "remainder",
    ["0{n}", "+{n}", " {n}", "{n}_0", "-{n}", ""],
)
async def test_get_track_rejects_non_canonical_remainder(
    make_provider: MakeProvider, beets_db: BeetsDb, remainder: str
) -> None:
    """Only the exact decimal beets id, with no leading zero or extra characters, resolves."""
    item_id = beets_db.add_item(**item_fields())
    provider = await make_provider()
    bad_id = f"track-{INSTANCE_ID}-{remainder.format(n=item_id)}"

    with pytest.raises(MediaNotFoundError):
        await provider.get_track(bad_id)
    assert (await provider.get_track(track_prov_id(item_id))).name == "Song"


@pytest.mark.parametrize(
    "prov_id",
    ["1", album_prov_id(1), track_prov_id(1, "beets--other"), f"track-{INSTANCE_ID}-nope"],
)
async def test_track_ids_need_this_instance_track_prefix(
    make_provider: MakeProvider, beets_db: BeetsDb, music_dir: Path, prov_id: str
) -> None:
    """A track id without this instance's track prefix is not found, though beets has item 1."""
    audio = music_dir / "Artist" / "Album" / "01 Song.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fLaC" + bytes(16))
    album_id = beets_db.add_album(**album_fields())
    item_id = beets_db.add_item(**item_fields(album_id=album_id))
    assert (album_id, item_id) == (1, 1)
    provider = await make_provider()

    with pytest.raises(MediaNotFoundError):
        await provider.get_track(prov_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(prov_id, MediaType.TRACK)
    assert (await provider.get_track(track_prov_id(item_id))).name == "Song"


@pytest.mark.parametrize(
    "prov_id",
    ["1", track_prov_id(1), album_prov_id(1, "beets--other"), f"album-{INSTANCE_ID}-nope"],
)
async def test_album_ids_need_this_instance_album_prefix(
    make_provider: MakeProvider, beets_db: BeetsDb, prov_id: str
) -> None:
    """An album id without this instance's album prefix is not found, though beets has album 1."""
    album_id = beets_db.add_album(**album_fields())
    item_id = beets_db.add_item(**item_fields(album_id=album_id))
    assert (album_id, item_id) == (1, 1)
    provider = await make_provider()

    with pytest.raises(MediaNotFoundError):
        await provider.get_album(prov_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album_tracks(prov_id)
    assert (await provider.get_album(album_prov_id(album_id))).name == "Album"


async def test_stream_details_for_existing_file(
    make_provider: MakeProvider, beets_db: BeetsDb, music_dir: Path
) -> None:
    """A track streams as a local file from the expanded beets path."""
    audio = music_dir / "Artist" / "Album" / "01 Song.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fLaC" + bytes(16))
    item_id = beets_db.add_item(**item_fields())
    provider = await make_provider()

    details = await provider.get_stream_details(track_prov_id(item_id), MediaType.TRACK)

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
        await provider.get_stream_details(track_prov_id(item_id), MediaType.TRACK)


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


def test_setup_error_strings_have_no_placeholders() -> None:
    """Setup error strings resolve without params, so a placeholder would show up literally."""
    strings_path = Path(beets.__file__).parent / "strings.json"
    strings = json.loads(strings_path.read_text())
    for message in strings["errors"].values():
        assert "{" not in message
