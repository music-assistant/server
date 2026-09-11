"""Tests for the artist discography command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.media_items import Album, Artist, ProviderMapping
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.music_provider import MusicProvider

from .helpers import create_album

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant

PROV_A = "alpha_1"
PROV_B = "beta_1"
ARTIST_ID_A = "alpha_artist1"
ARTIST_ID_B = "beta_artist1"
ARTIST_NAME = "Test Artist"
LIBRARY_ALBUM = "Library Album"


class FakeCatalogProvider(MusicProvider):
    """Music provider serving one fixed album catalog for every artist."""

    catalog: list[Album]
    failure: Exception | None = None

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Return the configured catalog, or raise the configured failure."""
        if self.failure is not None:
            raise self.failure
        return self.catalog


def _register_provider(
    mass: MusicAssistant, instance_id: str, catalog: list[Album]
) -> FakeCatalogProvider:
    """Register a fake music provider serving the given artist album catalog."""
    domain = instance_id.split("_", maxsplit=1)[0]
    provider = FakeCatalogProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain=domain,
            name=domain,
            description="Fake album catalog provider",
            codeowners=["@music-assistant"],
        ),
        config=ProviderConfig(
            values={},
            type=ProviderType.MUSIC,
            domain=domain,
            instance_id=instance_id,
            name=domain,
        ),
        supported_features={ProviderFeature.ARTIST_ALBUMS},
    )
    provider.catalog = catalog
    provider.available = True
    mass._providers[instance_id] = provider
    return provider


def _synced_album(provider_instance: str, item_id: str, name: str, artist_item_id: str) -> Album:
    """Create a provider album flagged as being in that provider's library."""
    album = create_album(provider_instance, item_id, name=name, artist_item_id=artist_item_id)
    for mapping in album.provider_mappings:
        mapping.in_library = True
    return album


async def _seed_library(mass: MusicAssistant) -> Artist:
    """Add an artist attached to both fake providers, with one of its albums in the library."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id=ARTIST_ID_A,
            provider=PROV_A,
            name=ARTIST_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=ARTIST_ID_A,
                    provider_domain="alpha",
                    provider_instance=PROV_A,
                    in_library=True,
                )
            },
        )
    )
    await mass.music.artists.add_provider_mappings(
        artist.item_id,
        [
            ProviderMapping(
                item_id=ARTIST_ID_B,
                provider_domain="beta",
                provider_instance=PROV_B,
                in_library=True,
            )
        ],
    )
    await mass.music.albums.add_item_to_library(
        _synced_album(PROV_A, "alpha_album1", LIBRARY_ALBUM, ARTIST_ID_A)
    )
    return artist


async def test_in_library_release_is_returned_once(mass: MusicAssistant) -> None:
    """A release that is both in the library and in a provider's catalog is returned once."""
    artist = await _seed_library(mass)
    _register_provider(
        mass,
        PROV_A,
        [
            create_album(PROV_A, "alpha_album1", name=LIBRARY_ALBUM, artist_item_id=ARTIST_ID_A),
            create_album(PROV_A, "alpha_album2", name="Alpha Only", artist_item_id=ARTIST_ID_A),
        ],
    )

    result = await mass.music.artists.discography(artist.item_id, "library")

    assert [(album.name, album.provider) for album in result] == [
        (LIBRARY_ALBUM, "library"),
        ("Alpha Only", PROV_A),
    ]


async def test_release_listed_by_two_providers_is_returned_once(mass: MusicAssistant) -> None:
    """A release the library does not have is kept once, however many providers list it."""
    artist = await _seed_library(mass)
    _register_provider(
        mass,
        PROV_A,
        [create_album(PROV_A, "alpha_shared", name="Shared Release", artist_item_id=ARTIST_ID_A)],
    )
    _register_provider(
        mass,
        PROV_B,
        [
            create_album(PROV_B, "beta_shared", name="Shared Release", artist_item_id=ARTIST_ID_B),
            create_album(PROV_B, "beta_album2", name="Beta Only", artist_item_id=ARTIST_ID_B),
        ],
    )

    result = await mass.music.artists.discography(artist.item_id, "library")

    # providers are queried in instance-id order, so the first one's copy is the one kept
    assert [(album.name, album.provider) for album in result] == [
        (LIBRARY_ALBUM, "library"),
        ("Shared Release", PROV_A),
        ("Beta Only", PROV_B),
    ]


async def test_provider_filter_limits_library_and_catalogs(mass: MusicAssistant) -> None:
    """A provider filter narrows the in-library albums as well as the catalogs fetched."""
    artist = await _seed_library(mass)
    await mass.music.albums.add_item_to_library(
        _synced_album(PROV_B, "beta_album1", "Beta Library Album", ARTIST_ID_B)
    )
    _register_provider(
        mass,
        PROV_A,
        [create_album(PROV_A, "alpha_album2", name="Alpha Only", artist_item_id=ARTIST_ID_A)],
    )
    _register_provider(
        mass,
        PROV_B,
        [create_album(PROV_B, "beta_album2", name="Beta Only", artist_item_id=ARTIST_ID_B)],
    )

    unfiltered = await mass.music.artists.discography(artist.item_id, "library")
    filtered = await mass.music.artists.discography(
        artist.item_id, "library", provider_filter=PROV_A
    )

    assert {album.name for album in unfiltered} == {
        LIBRARY_ALBUM,
        "Beta Library Album",
        "Alpha Only",
        "Beta Only",
    }
    assert [album.name for album in filtered] == [LIBRARY_ALBUM, "Alpha Only"]


async def test_failing_provider_is_logged_and_skipped(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that fails is logged and does not take the other providers' albums down."""
    artist = await _seed_library(mass)
    failing = _register_provider(mass, PROV_A, [])
    failing.failure = RuntimeError("provider offline")
    _register_provider(
        mass,
        PROV_B,
        [create_album(PROV_B, "beta_album2", name="Beta Only", artist_item_id=ARTIST_ID_B)],
    )

    result = await mass.music.artists.discography(artist.item_id, "library")

    assert [album.name for album in result] == [LIBRARY_ALBUM, "Beta Only"]
    assert f"Error fetching albums for artist {ARTIST_NAME} from a provider" in caplog.text


async def test_provider_artist_catalog_is_resolved_to_library_items(mass: MusicAssistant) -> None:
    """The catalog of a provider artist is returned with its in-library albums resolved."""
    await _seed_library(mass)
    _register_provider(
        mass,
        PROV_A,
        [
            create_album(PROV_A, "alpha_album1", name=LIBRARY_ALBUM, artist_item_id=ARTIST_ID_A),
            create_album(PROV_A, "alpha_album2", name="Alpha Only", artist_item_id=ARTIST_ID_A),
        ],
    )

    result = await mass.music.artists.discography(ARTIST_ID_A, PROV_A)

    assert [(album.name, album.provider) for album in result] == [
        (LIBRARY_ALBUM, "library"),
        ("Alpha Only", PROV_A),
    ]
