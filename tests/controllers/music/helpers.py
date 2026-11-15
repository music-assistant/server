"""Shared helpers for the music controller tests."""

from __future__ import annotations

from typing import Literal
from unittest.mock import MagicMock

from music_assistant_models.enums import EventType, ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.helpers.json import serialize_to_json
from music_assistant.mass import MusicAssistant

ISRC = "USRC17607839"

CollectionBookState = Literal[
    "unplayed", "finished", "in_progress", "unavailable", "not_in_library"
]


def create_track(
    provider_instance: str,
    item_id: str,
    name: str = "Test Track",
    isrc: str = ISRC,
    duration: int = 200,
) -> Track:
    """
    Create a Track as it would be received from a music provider.

    :param provider_instance: The provider instance id the track originates from.
    :param item_id: The item id of the track on the provider.
    :param name: The track name.
    :param isrc: The ISRC external id to attach to the track.
    :param duration: The track duration in seconds.
    """
    provider_domain = provider_instance.split("_", maxsplit=1)[0]
    return Track(
        item_id=item_id,
        provider=provider_instance,
        name=name,
        duration=duration,
        external_ids={(ExternalID.ISRC, isrc)},
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                audio_format=AudioFormat(),
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id=f"{item_id}_artist",
                    provider=provider_instance,
                    name="Test Artist",
                    provider_mappings={
                        ProviderMapping(
                            item_id=f"{item_id}_artist",
                            provider_domain=provider_domain,
                            provider_instance=provider_instance,
                            audio_format=AudioFormat(),
                        )
                    },
                )
            ]
        ),
    )


def create_album(
    provider_instance: str,
    item_id: str,
    name: str = "Test Album",
    artist_name: str | None = "Test Artist",
    artist_item_id: str | None = None,
    external_ids: set[tuple[ExternalID, str]] | None = None,
) -> Album:
    """
    Create an Album as it would be received from a music provider.

    :param provider_instance: The provider instance id the album originates from.
    :param item_id: The item id of the album on the provider.
    :param name: The album name.
    :param artist_name: The album artist name, or None for an album without artists.
    :param artist_item_id: The item id of the album artist on the provider,
        defaults to one derived from the album item id.
    :param external_ids: External ids (e.g. a MusicBrainz release id) to attach.
    """
    provider_domain = provider_instance.split("_", maxsplit=1)[0]
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    if artist_name is not None:
        artist_id = artist_item_id or f"{item_id}_artist"
        artists.append(
            Artist(
                item_id=artist_id,
                provider=provider_instance,
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=artist_id,
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                        audio_format=AudioFormat(),
                    )
                },
            )
        )
    return Album(
        item_id=item_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                audio_format=AudioFormat(),
            )
        },
        artists=artists,
        external_ids=external_ids or set(),
    )


def media_item_events(signal_event: MagicMock) -> list[EventType]:
    """Return the events a patched signal_event sent, without the debounced task list updates."""
    return [
        call.args[0]
        for call in signal_event.call_args_list
        if call.args[0] != EventType.TASKS_UPDATED
    ]


async def add_collection_audiobook(
    mass: MusicAssistant,
    item_id: int,
    sequence: float | str | None,
    state: CollectionBookState = "unplayed",
    *,
    collection: str = "Saga",
    played_at: int = 100,
) -> None:
    """Insert a library audiobook of a collection on local_1, in the given state for user-a."""
    await mass.music.database.insert(
        DB_TABLE_AUDIOBOOKS,
        {
            "item_id": item_id,
            "name": f"Book {item_id}",
            "sort_name": f"book {item_id}",
            "authors": "[]",
            "narrators": "[]",
            "metadata": serialize_to_json(
                {
                    "collections": [{"title": collection, "sequence": sequence}],
                    "images": [
                        {"type": "thumb", "path": f"cover_{item_id}", "provider": "local_1"}
                    ],
                }
            ),
            "search_name": f"book{item_id}",
            "search_sort_name": f"book{item_id}",
        },
    )
    await mass.music.database.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": "audiobook",
            "item_id": item_id,
            "provider_domain": "local",
            "provider_instance": "local_1",
            "provider_item_id": f"local-{item_id}",
            "available": state != "unavailable",
            "in_library": state != "not_in_library",
        },
    )
    if state in ("finished", "in_progress"):
        await mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": str(item_id),
                "provider": "library",
                "media_type": "audiobook",
                "name": f"Book {item_id}",
                "timestamp": played_at,
                "fully_played": state == "finished",
                "seconds_played": 60,
                "userid": "user-a",
            },
        )
