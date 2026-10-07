"""Tests that client-supplied playlog item ids never reach SQL as code."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ItemMapping, ProviderMapping

from music_assistant.constants import DB_TABLE_PLAYLOG, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant

ABS_INSTANCE = "audiobookshelf--AbCd"
INJECTION_ITEM_ID = "1 UNION SELECT sqlite_version(),2,3,4,5,6,7,8,9,10--"


async def _add_playlog_row(mass: MusicAssistant, item_id: str, userid: str) -> None:
    """Insert an in-progress library audiobook playlog row."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": "library",
            "media_type": MediaType.AUDIOBOOK.value,
            "name": f"Book {item_id}",
            "timestamp": 1000,
            "fully_played": False,
            "seconds_played": 60,
            "userid": userid,
        },
    )


async def _audiobook_playlog_rows(mass: MusicAssistant) -> list[Mapping[str, Any]]:
    """Return every audiobook playlog row."""
    return await mass.music.database.get_rows(
        DB_TABLE_PLAYLOG, {"media_type": MediaType.AUDIOBOOK.value}
    )


async def _add_library_audiobook(mass: MusicAssistant) -> Audiobook:
    """Add an audiobook to the library and return the library item."""
    return await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="book-001",
            provider="filesystem_local--AbCd",
            name="A Library Audiobook",
            provider_mappings={
                ProviderMapping(
                    item_id="book-001",
                    provider_domain="filesystem_local",
                    provider_instance="filesystem_local--AbCd",
                )
            },
        )
    )


async def test_playlog_provider_item_ids_ignores_injected_item_id(mass: MusicAssistant) -> None:
    """A stored item id carrying SQL is compared as a value and matches nothing."""
    user = await mass.webserver.auth.create_user("playlogsqlinjection")
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.AUDIOBOOK.value,
            "item_id": 1,
            "provider_domain": "audiobookshelf",
            "provider_instance": ABS_INSTANCE,
            "provider_item_id": "abs-book-1",
            "available": True,
            "in_library": True,
        },
    )
    await _add_playlog_row(mass, "1", user.user_id)
    await _add_playlog_row(mass, INJECTION_ITEM_ID, user.user_id)

    result = await mass.music.get_playlog_provider_item_ids(ABS_INSTANCE, userid=user.user_id)

    assert result == [(MediaType.AUDIOBOOK, "abs-book-1")]


async def test_mark_played_stores_numeric_library_item_id(mass: MusicAssistant) -> None:
    """A library item with its numeric database id is still written to the playlog."""
    user = await mass.webserver.auth.create_user("playlognumericlibraryid")
    db_book = await _add_library_audiobook(mass)

    await mass.music.mark_item_played(
        db_book, fully_played=False, seconds_played=60, userid=user.user_id
    )

    rows = await _audiobook_playlog_rows(mass)
    assert [(row["item_id"], row["provider"]) for row in rows] == [(db_book.item_id, "library")]


async def test_mark_played_stores_library_item_mapping(mass: MusicAssistant) -> None:
    """A minimized library item reference is still resolved and written to the playlog."""
    user = await mass.webserver.auth.create_user("playloglibrarymapping")
    db_book = await _add_library_audiobook(mass)

    await mass.music.mark_item_played(
        ItemMapping.from_item(db_book),
        fully_played=False,
        seconds_played=60,
        userid=user.user_id,
    )

    rows = await _audiobook_playlog_rows(mass)
    assert [(row["item_id"], row["provider"]) for row in rows] == [(db_book.item_id, "library")]
