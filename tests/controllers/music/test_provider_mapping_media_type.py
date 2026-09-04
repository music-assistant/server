"""Tests that provider item lookups stay scoped to their own media type."""

from __future__ import annotations

import pytest
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    ProviderMapping,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

PROVIDER = "deezer_1"
ALBUM_ITEM_ID = "908993"
AUDIOBOOK_ITEM_ID = "14001886"


@pytest.fixture(scope="class", name="mass")
def mass_fixture(music_mass_class: MusicAssistant) -> MusicAssistant:
    """Return the class-scoped database-only Music Assistant fixture."""
    return music_mass_class


def _mapping(item_id: str) -> ProviderMapping:
    """Create a provider mapping for a fixture item."""
    return ProviderMapping(item_id=item_id, provider_domain="deezer", provider_instance=PROVIDER)


class TestProviderMappingMediaTypeScope:
    """A provider can expose an audiobook and an album from the same instance."""

    async def test_lookup_ignores_other_media_types(self, mass: MusicAssistant) -> None:
        """A mapping of another media type never resolves to the item sharing its library id."""
        album = await mass.music.albums.add_item_to_library(
            Album(
                item_id=ALBUM_ITEM_ID,
                provider=PROVIDER,
                name="Endgame",
                provider_mappings={_mapping(ALBUM_ITEM_ID)},
                artists=UniqueList(
                    [
                        Artist(
                            item_id="artist_1",
                            provider=PROVIDER,
                            name="Rise Against",
                            provider_mappings={_mapping("artist_1")},
                        )
                    ]
                ),
            )
        )
        audiobook = await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id=AUDIOBOOK_ITEM_ID,
                provider=PROVIDER,
                name="Folge 7: Tina in Gefahr",
                provider_mappings={_mapping(AUDIOBOOK_ITEM_ID)},
            )
        )
        # item ids are allocated per media type, so the collision only exists while both match
        assert int(album.item_id) == int(audiobook.item_id)

        assert (
            await mass.music.albums.get_library_item_by_prov_id(AUDIOBOOK_ITEM_ID, PROVIDER) is None
        )
        assert (
            await mass.music.audiobooks.get_library_item_by_prov_id(ALBUM_ITEM_ID, PROVIDER) is None
        )
        # the batched variant resolves through the same subquery
        assert (
            await mass.music.albums.get_library_items_by_prov_id(
                provider_instance=PROVIDER, provider_item_ids=[AUDIOBOOK_ITEM_ID]
            )
            == []
        )

        found_album = await mass.music.albums.get_library_item_by_prov_id(ALBUM_ITEM_ID, PROVIDER)
        found_audiobook = await mass.music.audiobooks.get_library_item_by_prov_id(
            AUDIOBOOK_ITEM_ID, PROVIDER
        )
        assert found_album is not None
        assert found_album.name == "Endgame"
        assert found_audiobook is not None
        assert found_audiobook.name == "Folge 7: Tina in Gefahr"
