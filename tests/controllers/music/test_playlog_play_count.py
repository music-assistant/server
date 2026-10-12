"""
Tests that marking an item unplayed only clears mutable progress state.

Completed plays are append-only history and are not undone by clearing progress.
"""

from __future__ import annotations

import asyncio

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ItemMapping, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS, DB_TABLE_PLAY_HISTORY
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
        DB_TABLE_MEDIA_PROGRESS,
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


async def test_marking_a_finished_audiobook_unplayed_preserves_history_and_play_count(
    mass: MusicAssistant,
) -> None:
    """Clearing the progress row does not undo completed history or its count."""
    user = await mass.webserver.auth.create_user("finishedbook")
    db_item_id = await _add_library_audiobook(mass, play_count=2)
    await _add_playlog_row(mass, user.user_id, fully_played=True)
    for timestamp in (1000, 2000):
        await mass.music.database.insert(
            DB_TABLE_PLAY_HISTORY,
            {
                "item_id": AUDIOBOOK_ID,
                "provider": AUDIOBOOK_PROVIDER,
                "media_type": MediaType.AUDIOBOOK.value,
                "userid": user.user_id,
                "timestamp": timestamp,
                "name": AUDIOBOOK_NAME,
            },
        )

    await mass.music.mark_item_unplayed(_reference(), userid=user.user_id)

    assert await _play_count(mass, db_item_id) == 2
    assert (
        len(await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY, {"userid": user.user_id}))
        == 2
    )


async def test_play_count_is_never_driven_below_zero(mass: MusicAssistant) -> None:
    """A completed row against an uncounted library item still leaves the count at zero."""
    user = await mass.webserver.auth.create_user("uncountedbook")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    await _add_playlog_row(mass, user.user_id, fully_played=True)

    await mass.music.mark_item_unplayed(_reference(), userid=user.user_id)

    assert await _play_count(mass, db_item_id) == 0


async def test_provider_sync_appends_history_only_when_progress_completes(
    mass: MusicAssistant,
) -> None:
    """Provider sync records one event on the incomplete-to-completed transition."""
    user = await mass.webserver.auth.create_user("providerhistory")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    audiobook = await mass.music.audiobooks.get_library_item(db_item_id)

    await mass.music.mark_item_played(
        audiobook,
        fully_played=False,
        seconds_played=120,
        user_initiated=False,
        userid=user.user_id,
    )
    assert not await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY, {"userid": user.user_id})

    await asyncio.gather(
        mass.music.mark_item_played(
            audiobook,
            fully_played=True,
            seconds_played=3600,
            user_initiated=False,
            userid=user.user_id,
        ),
        mass.music.mark_item_played(
            audiobook,
            fully_played=True,
            seconds_played=3600,
            user_initiated=False,
            userid=user.user_id,
        ),
    )

    rows = await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY, {"userid": user.user_id})
    assert len(rows) == 1
    assert rows[0]["media_type"] == MediaType.AUDIOBOOK.value
    assert await _play_count(mass, db_item_id) == 1


async def test_unattributed_completion_does_not_fan_out_into_history(
    mass: MusicAssistant,
) -> None:
    """Progress may fan out to users, but an unknown listener is not a history event."""
    await mass.webserver.auth.create_user("historyfanout1")
    await mass.webserver.auth.create_user("historyfanout2")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    audiobook = await mass.music.audiobooks.get_library_item(db_item_id)

    await mass.music.mark_item_played(audiobook, fully_played=True, queue_id="anonymous-queue")

    progress_rows = await mass.music.database.get_rows(
        DB_TABLE_MEDIA_PROGRESS,
        {"item_id": db_item_id, "media_type": MediaType.AUDIOBOOK.value},
    )
    history_rows = await mass.music.database.get_rows(
        DB_TABLE_PLAY_HISTORY,
        {"item_id": db_item_id, "media_type": MediaType.AUDIOBOOK.value},
    )
    assert len(progress_rows) == 2
    assert not history_rows
    assert await _play_count(mass, db_item_id) == 0


async def test_manual_mark_played_does_not_append_history_or_increment_count(
    mass: MusicAssistant,
) -> None:
    """Manual state changes are not completed playback events."""
    user = await mass.webserver.auth.create_user("manualhistory")
    db_item_id = await _add_library_audiobook(mass, play_count=0)
    audiobook = await mass.music.audiobooks.get_library_item(db_item_id)

    await mass.music.mark_item_played(audiobook, userid=user.user_id)

    assert not await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY, {"userid": user.user_id})
    assert await _play_count(mass, db_item_id) == 0
