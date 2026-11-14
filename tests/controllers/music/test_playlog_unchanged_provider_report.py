"""Tests that a provider report stamps the playlog with the time its progress was made."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.mass import MusicAssistant

PROVIDER_INSTANCE = "audiobookshelf--sync"


async def _add_audiobook(mass: MusicAssistant, item_id: str) -> Audiobook:
    return await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id=item_id,
            provider=PROVIDER_INSTANCE,
            name="Book",
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain="audiobookshelf",
                    provider_instance=PROVIDER_INSTANCE,
                )
            },
        )
    )


@pytest.mark.parametrize(("seconds_played", "timestamp_kept"), [(120, True), (300, False)])
async def test_unchanged_provider_report_keeps_timestamp(
    mass: MusicAssistant, seconds_played: int, timestamp_kept: bool
) -> None:
    """
    Only a changed progress is stamped as just played.

    The row still takes the current name, and a user without a row still gets it.
    """
    listener = await mass.webserver.auth.create_user(f"listener{seconds_played}")
    newcomer = await mass.webserver.auth.create_user(f"newcomer{seconds_played}")
    audiobook = await _add_audiobook(mass, f"book_{seconds_played}")
    row_key = {
        "item_id": audiobook.item_id,
        "provider": "library",
        "media_type": MediaType.AUDIOBOOK.value,
    }
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            **row_key,
            "name": "Old name",
            "userid": listener.user_id,
            "seconds_played": 120,
            "fully_played": False,
            "timestamp": 1000,
        },
    )

    await mass.music.mark_item_played(
        audiobook,
        fully_played=False,
        seconds_played=seconds_played,
        user_initiated=False,
        provider_instance_id=PROVIDER_INSTANCE,
    )

    listener_row = await mass.music.database.get_row(
        DB_TABLE_MEDIA_PROGRESS, {**row_key, "userid": listener.user_id}
    )
    newcomer_row = await mass.music.database.get_row(
        DB_TABLE_MEDIA_PROGRESS, {**row_key, "userid": newcomer.user_id}
    )
    assert listener_row is not None
    assert listener_row["seconds_played"] == seconds_played
    assert (listener_row["timestamp"] == 1000) is timestamp_kept
    assert listener_row["name"] == "Book"
    assert newcomer_row is not None
    assert newcomer_row["seconds_played"] == seconds_played


@pytest.mark.parametrize(
    ("progress_timestamp", "expected_timestamp"), [(1000, 1000), (4102444800, None)]
)
async def test_provider_report_keeps_its_time(
    mass: MusicAssistant, progress_timestamp: int, expected_timestamp: int | None
) -> None:
    """A report is stamped with the time the provider recorded; a future one counts as now."""
    listener = await mass.webserver.auth.create_user(f"timed{progress_timestamp}")
    audiobook = await _add_audiobook(mass, f"timed_{progress_timestamp}")

    await mass.music.mark_item_played(
        audiobook,
        fully_played=False,
        seconds_played=60,
        user_initiated=False,
        userid=listener.user_id,
        progress_timestamp=progress_timestamp,
    )

    row = await mass.music.database.get_row(
        DB_TABLE_MEDIA_PROGRESS,
        {
            "item_id": audiobook.item_id,
            "provider": "library",
            "media_type": MediaType.AUDIOBOOK.value,
            "userid": listener.user_id,
        },
    )
    assert row is not None
    if expected_timestamp is None:
        assert abs(row["timestamp"] - utc_timestamp()) < 60
    else:
        assert row["timestamp"] == expected_timestamp


@pytest.mark.parametrize(
    ("reset", "progress_timestamp", "expected_row"),
    [
        (False, 1000, (120, 2000)),
        (False, 3000, (300, 3000)),
        (True, 1000, (120, 2000)),
        (True, 3000, None),
    ],
)
async def test_older_provider_report_keeps_newer_progress(
    mass: MusicAssistant,
    reset: bool,
    progress_timestamp: int,
    expected_row: tuple[int, int] | None,
) -> None:
    """A progress or reset the provider recorded before the stored progress does not replace it."""
    name = f"older_{reset}_{progress_timestamp}"
    listener = await mass.webserver.auth.create_user(name)
    audiobook = await _add_audiobook(mass, name)
    row_key = {
        "item_id": audiobook.item_id,
        "provider": "library",
        "media_type": MediaType.AUDIOBOOK.value,
        "userid": listener.user_id,
    }
    await mass.music.database.insert(
        DB_TABLE_MEDIA_PROGRESS,
        {
            **row_key,
            "name": "Book",
            "seconds_played": 120,
            "fully_played": False,
            "timestamp": 2000,
        },
    )

    if reset:
        await mass.music.mark_item_unplayed(
            audiobook,
            provider_instance_id=PROVIDER_INSTANCE,
            progress_timestamp=progress_timestamp,
        )
    else:
        await mass.music.mark_item_played(
            audiobook,
            fully_played=False,
            seconds_played=300,
            user_initiated=False,
            provider_instance_id=PROVIDER_INSTANCE,
            progress_timestamp=progress_timestamp,
        )

    row = await mass.music.database.get_row(DB_TABLE_MEDIA_PROGRESS, row_key)
    assert (row and (row["seconds_played"], row["timestamp"])) == expected_row
