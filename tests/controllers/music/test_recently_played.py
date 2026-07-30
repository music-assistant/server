"""Tests for MusicController.recently_played playlog queries."""

from __future__ import annotations

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant


async def _add_playlog_track(
    mass: MusicAssistant,
    item_id: str,
    timestamp: int,
    *,
    fully_played: bool = True,
    userid: str = "user-a",
) -> None:
    """Insert a single track row into the playlog."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": "library",
            "media_type": MediaType.TRACK.value,
            "name": f"Track {item_id}",
            "timestamp": timestamp,
            "fully_played": fully_played,
            "seconds_played": 180,
            "userid": userid,
            "user_initiated": True,
        },
    )


async def test_recently_played_filters_by_played_after_timestamp(mass: MusicAssistant) -> None:
    """Only entries with timestamp >= played_after_timestamp are returned."""
    await _add_playlog_track(mass, "recent", timestamp=2000)
    await _add_playlog_track(mass, "old", timestamp=1000)

    result = await mass.music.recently_played(
        limit=0,
        media_types=[MediaType.TRACK],
        userid="user-a",
        played_after_timestamp=1500,
    )

    item_ids = {item.item_id for item in result}
    assert "recent" in item_ids
    assert "old" not in item_ids


async def test_recently_played_without_timestamp_returns_all(mass: MusicAssistant) -> None:
    """Existing callers (no played_after_timestamp) still get every entry."""
    await _add_playlog_track(mass, "recent", timestamp=2000)
    await _add_playlog_track(mass, "old", timestamp=1000)

    result = await mass.music.recently_played(
        limit=0,
        media_types=[MediaType.TRACK],
        userid="user-a",
    )

    item_ids = {item.item_id for item in result}
    assert {"recent", "old"} <= item_ids
