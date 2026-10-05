"""
Tests that marking an item unplayed only undoes a play that was actually counted.

``mark_item_played`` raises ``play_count`` for a completed play only, so removing a
playlog row that was still in progress must leave the count alone.
"""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ItemMapping, ProviderMapping

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant

AUDIOBOOK_PROVIDER = "filesystem_local--AbCd"
AUDIOBOOK_ID = "book-001"
AUDIOBOOK_NAME = "A Library Audiobook"


async def _add_library_audiobook(mass: MusicAssistant, play_count: int) -> str:
    """
    Add the audiobook under test to the library and return its library item id.

    :param mass: The MusicAssistant instance to seed.
    :param play_count: The play count to store for the library item.
    """
    db_item = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id=AUDIOBOOK_ID,
            provider=AUDIOBOOK_PROVIDER,
            name=AUDIOBOOK_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIOBOOK_ID,
                    provider_domain="filesystem_local",
                    provider_instance=AUDIOBOOK_PROVIDER,
                )
            },
        )
    )
    await mass.music.database.execute(
        f"UPDATE {mass.music.audiobooks.db_table} SET play_count = {play_count} "
        f"WHERE item_id = {db_item.item_id}"
    )
    await mass.music.database.commit()
    return db_item.item_id


async def _add_playlog_row(mass: MusicAssistant, userid: str, *, fully_played: bool) -> None:
    """
    Seed the playlog row for the audiobook under test.

    :param mass: The MusicAssistant instance to seed.
    :param userid: The user the row belongs to.
    :param fully_played: Whether the row records a completed play.
    """
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": AUDIOBOOK_ID,
            "provider": AUDIOBOOK_PROVIDER,
            "media_type": MediaType.AUDIOBOOK.value,
            "name": AUDIOBOOK_NAME,
            "userid": userid,
            "seconds_played": 120,
            "fully_played": fully_played,
            "timestamp": 1000,
        },
        allow_replace=True,
    )


async def _play_count(mass: MusicAssistant, db_item_id: str) -> int:
    """
    Return the stored play count of the audiobook under test.

    :param mass: The MusicAssistant instance to read from.
    :param db_item_id: The library item id of the audiobook.
    """
    row = await mass.music.database.get_row(
        mass.music.audiobooks.db_table, {"item_id": int(db_item_id)}
    )
    assert row is not None
    return int(row["play_count"])


def _reference() -> ItemMapping:
    """Return the audiobook reference as a Discover row hands it out."""
    return ItemMapping(
        item_id=AUDIOBOOK_ID,
        provider=AUDIOBOOK_PROVIDER,
        name=AUDIOBOOK_NAME,
        media_type=MediaType.AUDIOBOOK,
    )


async def test_marking_an_in_progress_audiobook_unplayed_keeps_the_play_count(
    mass: MusicAssistant,
) -> None:
    """An unfinished play was never counted, so removing it must not discount one."""
    user = await mass.webserver.auth.create_user("inprogressbook")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    await _add_playlog_row(mass, user.user_id, fully_played=False)

    await mass.music.mark_item_unplayed(_reference(), userid=user.user_id)

    assert await _play_count(mass, db_item_id) == 0


async def test_marking_a_finished_audiobook_unplayed_discounts_the_play(
    mass: MusicAssistant,
) -> None:
    """A completed play was counted, so removing it discounts one again."""
    user = await mass.webserver.auth.create_user("finishedbook")
    db_item_id = await _add_library_audiobook(mass, play_count=1)
    await _add_playlog_row(mass, user.user_id, fully_played=True)

    await mass.music.mark_item_unplayed(_reference(), userid=user.user_id)

    assert await _play_count(mass, db_item_id) == 0


async def test_play_count_is_never_driven_below_zero(mass: MusicAssistant) -> None:
    """A completed row against an uncounted library item still leaves the count at zero."""
    user = await mass.webserver.auth.create_user("uncountedbook")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    await _add_playlog_row(mass, user.user_id, fully_played=True)

    await mass.music.mark_item_unplayed(_reference(), userid=user.user_id)

    assert await _play_count(mass, db_item_id) == 0
