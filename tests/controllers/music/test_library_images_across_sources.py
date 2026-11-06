"""Tests for the artwork of a library item linked to more than one music source."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import AlbumType, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance."""
    return music_mass


def _mapping(instance: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id, provider_domain="opensubsonic", provider_instance=instance
    )


def _metadata(*images: tuple[str, str]) -> MediaItemMetadata:
    return MediaItemMetadata(
        images=UniqueList(
            MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)
            for path, provider in images
        )
    )


async def test_refresh_from_one_source_keeps_the_other_sources_artwork(
    mass: MusicAssistant,
) -> None:
    """Two users' Navidrome accounts share an album and an artist (support#6602)."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Shared Artist",
            provider_mappings={_mapping("subsonic_a", "ar_a"), _mapping("subsonic_b", "ar_b")},
            metadata=_metadata(("ar_a.jpg", "subsonic_a"), ("ar_b.jpg", "subsonic_b")),
        )
    )
    album = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="Shared Album",
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping("subsonic_a", "al_a"), _mapping("subsonic_b", "al_b")},
            artists=UniqueList([artist]),
            metadata=_metadata(("al_a.jpg", "subsonic_a"), ("al_b.jpg", "subsonic_b")),
        )
    )

    # user B opens both items: each is refreshed from B's account only, with new artwork
    artist_b = Artist(
        item_id="ar_b",
        provider="subsonic_b",
        name="Shared Artist",
        provider_mappings={_mapping("subsonic_b", "ar_b")},
        metadata=_metadata(("ar_b_new.jpg", "subsonic_b")),
    )
    await mass.music.artists.update_item_in_library(artist.item_id, artist_b, overwrite=True)
    await mass.music.albums.update_item_in_library(
        album.item_id,
        Album(
            item_id="al_b",
            provider="subsonic_b",
            name="Shared Album",
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping("subsonic_b", "al_b")},
            artists=UniqueList([artist_b]),  # B's payload carries B's view of the artist
            metadata=_metadata(("al_b_new.jpg", "subsonic_b")),
        ),
        overwrite=True,
    )

    for stored, a_image, b_image in (
        (await mass.music.artists.get_library_item(artist.item_id), "ar_a.jpg", "ar_b_new.jpg"),
        (await mass.music.albums.get_library_item(album.item_id), "al_a.jpg", "al_b_new.jpg"),
    ):
        images = {(img.path, img.provider) for img in stored.metadata.images or []}
        # A's artwork survives; B's is replaced by the refreshed one
        assert images == {(a_image, "subsonic_a"), (b_image, "subsonic_b")}, stored.name
        assert {m.provider_instance for m in stored.provider_mappings} == {
            "subsonic_a",
            "subsonic_b",
        }
