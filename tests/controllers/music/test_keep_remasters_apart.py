"""Tests for the library option that keeps a remaster apart from the original recording."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import AlbumType, ExternalID
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.controllers.music.constants import CONF_KEEP_REMASTERS_APART

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


def _come_together(provider_instance: str, album_version: str) -> Track:
    """
    Build a provider's copy of the same recording, on its own release of the album.

    :param provider_instance: The provider instance the copy comes from.
    :param album_version: The version of the release, e.g. "2009 Remaster".
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
        external_ids={(ExternalID.MB_RECORDING, RECORDING_ID)},
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


@pytest.mark.parametrize("keep_apart", [False, True], ids=["merged", "kept_apart"])
async def test_an_original_joins_its_library_remaster_unless_kept_apart(
    mass: MusicAssistant, keep_apart: bool
) -> None:
    """The remastered album the library holds the track on keeps the original apart."""
    await mass.config.save_core_config("music", {CONF_KEEP_REMASTERS_APART: keep_apart})
    remaster = await mass.music.tracks.add_item_to_library(
        _come_together("spotify--test", "2009 Remaster")
    )

    original = await mass.music.tracks.add_item_to_library(
        _come_together("filesystem_local--test", "")
    )

    assert remaster.album is not None
    assert remaster.album.version == "2009 Remaster"
    assert (original.item_id != remaster.item_id) is keep_apart
