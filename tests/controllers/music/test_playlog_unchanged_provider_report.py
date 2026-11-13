"""Tests that a provider sync repeating unchanged progress keeps the playlog timestamp."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.mass import MusicAssistant

PROVIDER_INSTANCE = "audiobookshelf--sync"


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
    audiobook = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id=f"book_{seconds_played}",
            provider=PROVIDER_INSTANCE,
            name="Book",
            provider_mappings={
                ProviderMapping(
                    item_id=f"book_{seconds_played}",
                    provider_domain="audiobookshelf",
                    provider_instance=PROVIDER_INSTANCE,
                )
            },
        )
    )
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
