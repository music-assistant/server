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

    @pytest.mark.parametrize("provider_id", [PROVIDER, "deezer"])
    @pytest.mark.parametrize("shared_provider_id", [False, True])
    async def test_lookup_scopes_media_type(
        self, mass: MusicAssistant, provider_id: str, shared_provider_id: bool
    ) -> None:
        """Lookups respect media types even when the provider reuses an item id."""
        album_id = "shared-book" if shared_provider_id else ALBUM_ITEM_ID
        audiobook_id = "shared-book" if shared_provider_id else AUDIOBOOK_ITEM_ID
        album = await mass.music.albums.add_item_to_library(
            Album(
                item_id=album_id,
                provider=PROVIDER,
                name="Shared audiobook" if shared_provider_id else "Endgame",
                provider_mappings={_mapping(album_id)},
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
                item_id=audiobook_id,
                provider=PROVIDER,
                name="Shared audiobook" if shared_provider_id else "Folge 7: Tina in Gefahr",
                provider_mappings={_mapping(audiobook_id)},
            )
        )
        # item ids are allocated per media type, so the collision only exists while both match
        assert int(album.item_id) == int(audiobook.item_id)

        album_match = await mass.music.albums.get_library_item_by_prov_id(audiobook_id, provider_id)
        audiobook_match = await mass.music.audiobooks.get_library_item_by_prov_id(
            album_id, provider_id
        )
        assert (album_match.item_id if album_match else None) == (
            album.item_id if shared_provider_id else None
        )
        assert (audiobook_match.item_id if audiobook_match else None) == (
            audiobook.item_id if shared_provider_id else None
        )

        # The batched lookup also accepts explicit instance and domain selectors.
        batch = await mass.music.albums.get_library_items_by_prov_id(
            provider_instance=provider_id if provider_id == PROVIDER else None,
            provider_domain=provider_id if provider_id != PROVIDER else None,
            provider_item_ids=[audiobook_id],
        )
        assert [item.item_id for item in batch] == ([album.item_id] if shared_provider_id else [])

        found_album = await mass.music.albums.get_library_item_by_prov_id(album_id, provider_id)
        found_audiobook = await mass.music.audiobooks.get_library_item_by_prov_id(
            audiobook_id, provider_id
        )
        assert found_album is not None
        assert found_album.item_id == album.item_id
        assert found_audiobook is not None
        assert found_audiobook.item_id == audiobook.item_id
