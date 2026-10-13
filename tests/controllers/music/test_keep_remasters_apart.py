"""Tests for the library option that keeps a remaster apart from the original recording."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.controllers.music.constants import CONF_KEEP_REMASTERS_APART
from music_assistant.helpers.compare import compare_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RECORDING_ID = "0c1d2e3f-4a5b-4c6d-9e8f-7a6b5c4d3e22"


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance: matching only touches the library database."""
    return music_mass


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    """Create a mapping on a provider instance named after its domain."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.split("--", maxsplit=1)[0],
        provider_instance=provider_instance,
    )


def _come_together(
    provider_instance: str, album_version: str, recording_id: str | None = RECORDING_ID
) -> Track:
    """
    Build a provider's copy of the same recording, on its own release of the album.

    :param provider_instance: The provider instance the copy comes from.
    :param album_version: The version of the release, e.g. "2009 Remaster".
    :param recording_id: The MusicBrainz recording id the copy carries, if any.
    """
    artist = Artist(
        item_id=f"{provider_instance}-artist",
        provider=provider_instance,
        name="The Beatles",
        provider_mappings={_mapping(provider_instance, f"{provider_instance}-artist")},
    )
    return Track(
        item_id=f"{provider_instance}-track",
        provider=provider_instance,
        name="Come Together",
        duration=259,
        disc_number=1,
        track_number=1,
        external_ids={(ExternalID.MB_RECORDING, recording_id)} if recording_id else set(),
        artists=UniqueList([artist]),
        album=Album(
            item_id=f"{provider_instance}-album",
            provider=provider_instance,
            name="Abbey Road",
            version=album_version,
            album_type=AlbumType.ALBUM,
            artists=UniqueList([artist]),
            provider_mappings={_mapping(provider_instance, f"{provider_instance}-album")},
        ),
        provider_mappings={_mapping(provider_instance, f"{provider_instance}-track")},
    )


@pytest.mark.parametrize(
    "saved_values", [[True], [True, False]], ids=["turned_on", "turned_off_again"]
)
async def test_an_original_joins_its_library_remaster_unless_kept_apart(
    mass: MusicAssistant, saved_values: list[bool]
) -> None:
    """A saved setting applies right away, and the library remaster's album version decides."""
    for value in saved_values:
        await mass.config.save_core_config("music", {CONF_KEEP_REMASTERS_APART: value})
    remaster = await mass.music.tracks.add_item_to_library(
        _come_together("spotify--test", "2009 Remaster")
    )

    original = await mass.music.tracks.add_item_to_library(
        _come_together("filesystem_local--test", "")
    )

    assert remaster.album is not None
    assert remaster.album.version == "2009 Remaster"
    assert (original.item_id != remaster.item_id) is saved_values[-1]


async def test_a_stored_setting_applies_at_startup(mass: MusicAssistant) -> None:
    """The setting stored by an earlier run applies as soon as the music controller starts."""
    mass.config.set_raw_core_config_value("music", CONF_KEEP_REMASTERS_APART, True)

    with patch.object(mass.music, "_setup_database", AsyncMock()):
        await mass.music.setup(await mass.config.get_core_config("music"))

    assert not compare_track(
        _come_together("spotify--test", "2009 Remaster"),
        _come_together("filesystem_local--test", ""),
    )


async def test_a_copy_on_the_same_edition_joins_the_library_track(mass: MusicAssistant) -> None:
    """The album edition of a library track takes part in matching, also with the setting off."""
    deluxe = await mass.music.tracks.add_item_to_library(
        _come_together("spotify--test", "Deluxe Edition", recording_id=None)
    )

    copy = await mass.music.tracks.add_item_to_library(
        _come_together("filesystem_local--test", "Deluxe Edition", recording_id=None)
    )

    assert copy.item_id == deluxe.item_id
