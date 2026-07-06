"""Tests for the indexed external id lookup of media items."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_EXTERNAL_ID_LOOKUP
from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant

ISRC = "USRC17607839"
MBID = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller with a real library database."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    await controller._setup_database()
    yield controller
    if controller._database:
        await controller._database.close()


def _create_track(
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


async def _get_lookup_rows(music: MusicController, item_id: int | str) -> set[tuple[str, str]]:
    """Return the (external_id_type, external_id) lookup rows stored for a track."""
    return {
        (row["external_id_type"], row["external_id"])
        for row in await music.database.get_rows(
            DB_TABLE_EXTERNAL_ID_LOOKUP, {"media_type": "track", "item_id": int(item_id)}
        )
    }


async def test_same_isrc_from_two_providers_dedupes(music: MusicController) -> None:
    """Two providers exposing the same track with an identical ISRC merge into one item."""
    library_track_1 = await music.tracks.add_item_to_library(
        _create_track("spotify_1", "track_abc")
    )
    library_track_2 = await music.tracks.add_item_to_library(_create_track("tidal_1", "track_xyz"))

    assert library_track_1.item_id == library_track_2.item_id
    assert len(library_track_2.provider_mappings) == 2
    assert await music.tracks.library_count() == 1


async def test_get_library_item_by_external_id(music: MusicController) -> None:
    """Library items resolve by external id, both typed and untyped."""
    track = _create_track("spotify_1", "track_abc")
    track.external_ids.add((ExternalID.MB_RECORDING, MBID))
    library_track = await music.tracks.add_item_to_library(track)

    # typed lookup
    match = await music.tracks.get_library_item_by_external_id(ISRC, ExternalID.ISRC)
    assert match is not None
    assert match.item_id == library_track.item_id
    match = await music.tracks.get_library_item_by_external_id(MBID, ExternalID.MB_RECORDING)
    assert match is not None
    assert match.item_id == library_track.item_id
    # untyped lookup
    match = await music.tracks.get_library_item_by_external_id(ISRC)
    assert match is not None
    assert match.item_id == library_track.item_id
    # matching is case-insensitive (as the previous LIKE based scan was)
    match = await music.tracks.get_library_item_by_external_id(ISRC.lower(), ExternalID.ISRC)
    assert match is not None
    assert match.item_id == library_track.item_id
    # no (partial) match on wrong type or unknown id
    assert await music.tracks.get_library_item_by_external_id(ISRC, ExternalID.BARCODE) is None
    assert await music.tracks.get_library_item_by_external_id("something-else") is None
    assert await music.tracks.get_library_item_by_external_id(ISRC[:-1]) is None


async def test_external_id_lookup_rows_follow_item_updates(music: MusicController) -> None:
    """The lookup rows are kept in sync when an item is updated or removed."""
    library_track = await music.tracks.add_item_to_library(_create_track("spotify_1", "track_abc"))
    assert await _get_lookup_rows(music, library_track.item_id) == {(str(ExternalID.ISRC), ISRC)}

    # an update merges in newly discovered external ids
    update = _create_track("spotify_1", "track_abc")
    update.external_ids.add((ExternalID.MB_RECORDING, MBID))
    await music.tracks.update_item_in_library(library_track.item_id, update)
    assert await _get_lookup_rows(music, library_track.item_id) == {
        (str(ExternalID.ISRC), ISRC),
        (str(ExternalID.MB_RECORDING), MBID),
    }

    # an overwrite update replaces the lookup rows
    await music.tracks.update_item_in_library(
        library_track.item_id,
        _create_track("spotify_1", "track_abc", isrc="GBUM71029604"),
        overwrite=True,
    )
    assert await _get_lookup_rows(music, library_track.item_id) == {
        (str(ExternalID.ISRC), "GBUM71029604")
    }
    assert await music.tracks.get_library_item_by_external_id(ISRC) is None

    # removal cleans up the lookup rows
    await music.tracks.remove_item_from_library(library_track.item_id)
    assert await _get_lookup_rows(music, library_track.item_id) == set()
