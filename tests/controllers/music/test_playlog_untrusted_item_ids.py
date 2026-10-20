"""Tests that client-supplied playlog item ids never reach SQL as code."""

from __future__ import annotations

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant

ABS_INSTANCE = "audiobookshelf--AbCd"
INJECTION_ITEM_ID = "1 UNION SELECT sqlite_version(),2,3,4,5,6,7,8,9,10--"


async def _add_playlog_row(mass: MusicAssistant, item_id: str, userid: str) -> None:
    """Insert an in-progress library audiobook playlog row."""
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
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
