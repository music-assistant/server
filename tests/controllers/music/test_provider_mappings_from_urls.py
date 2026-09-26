"""Tests for turning streaming service links into provider mappings."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import ExternalID, MediaType

from music_assistant.controllers.music.helpers import (
    discogs_external_id,
    provider_mappings_from_urls,
)

SPOTIFY_ARTIST = "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"
SPOTIFY_ALBUM = "https://open.spotify.com/album/7eyQXxuf2nGj9d2367Gi5f"
TIDAL_ARTIST = "https://tidal.com/artist/64518"
APPLE_ARTIST = "https://music.apple.com/gb/artist/657515"
DEEZER_ARTISTS = ("https://www.deezer.com/artist/399", "https://www.deezer.com/artist/323887691")
QOBUZ_ARTIST = "https://www.qobuz.com/us-en/interpreter/radiohead/43840"
DISCOGS_ARTIST = "https://www.discogs.com/artist/3840"
BANDCAMP = "https://radiohead.bandcamp.com/"


def _instance(instance_id: str) -> MagicMock:
    """Return a loaded provider instance stub."""
    instance = MagicMock()
    instance.instance_id = instance_id
    return instance


def _mass(loaded: dict[str, list[str]]) -> MagicMock:
    """
    Return a MusicAssistant stub with the given music provider instances loaded.

    :param loaded: Provider instance ids per provider domain.
    """
    mass = MagicMock()
    mass.music.get_provider_instances = MagicMock(
        side_effect=lambda domain, **_kwargs: [
            _instance(instance_id) for instance_id in loaded.get(domain, [])
        ]
    )
    return mass


async def test_one_mapping_per_loaded_provider_on_its_first_instance() -> None:
    """Map each linked provider once, on its first instance, as an available non-library item."""
    mass = _mass({"spotify": ["spotify_2", "spotify_1"], "tidal": ["tidal_1"]})
    urls = [SPOTIFY_ARTIST, TIDAL_ARTIST, DISCOGS_ARTIST, BANDCAMP]

    mappings = await provider_mappings_from_urls(mass, urls, MediaType.ARTIST, set())

    assert [(m.provider_domain, m.provider_instance, m.item_id) for m in mappings] == [
        ("spotify", "spotify_1", "4Z8W4fKeB5YxbusRsdQVPb"),
        ("tidal", "tidal_1", "64518"),
    ]
    assert all(m.available is True and m.in_library is False for m in mappings)
    assert [m.url for m in mappings] == [SPOTIFY_ARTIST, TIDAL_ARTIST]


async def test_mapping_keeps_the_linked_url_without_a_canonical_form() -> None:
    """An Apple Music link has no storefront-less canonical form, so the link itself is kept."""
    mass = _mass({"apple_music": ["apple_music_1"]})

    mappings = await provider_mappings_from_urls(mass, [APPLE_ARTIST], MediaType.ARTIST, set())

    assert [(m.provider_domain, m.item_id, m.url) for m in mappings] == [
        ("apple_music", "657515", APPLE_ARTIST)
    ]


async def test_provider_linked_to_several_items_is_dropped() -> None:
    """A provider linked to two different items identifies neither."""
    mass = _mass({"deezer": ["deezer_1"], "spotify": ["spotify_1"]})
    urls = [*DEEZER_ARTISTS, SPOTIFY_ARTIST]

    mappings = await provider_mappings_from_urls(mass, urls, MediaType.ARTIST, set())

    assert [m.provider_domain for m in mappings] == ["spotify"]


async def test_the_same_item_linked_twice_is_one_mapping() -> None:
    """Two links to one item (share URL variants) are not an ambiguity."""
    mass = _mass({"spotify": ["spotify_1"]})
    urls = [SPOTIFY_ARTIST, f"{SPOTIFY_ARTIST}?si=abc"]

    mappings = await provider_mappings_from_urls(mass, urls, MediaType.ARTIST, set())

    assert [(m.provider_domain, m.item_id) for m in mappings] == [
        ("spotify", "4Z8W4fKeB5YxbusRsdQVPb")
    ]


async def test_unloaded_and_excluded_providers_are_dropped() -> None:
    """Only loaded providers not on the exclusion list get a mapping."""
    mass = _mass({"spotify": ["spotify_1"], "tidal": ["tidal_1"]})
    urls = [SPOTIFY_ARTIST, TIDAL_ARTIST, QOBUZ_ARTIST]

    mappings = await provider_mappings_from_urls(mass, urls, MediaType.ARTIST, {"tidal"})

    assert [m.provider_domain for m in mappings] == ["spotify"]


async def test_links_to_another_media_type_are_dropped() -> None:
    """A link to an album never becomes an artist mapping."""
    mass = _mass({"spotify": ["spotify_1"]})

    mappings = await provider_mappings_from_urls(mass, [SPOTIFY_ALBUM], MediaType.ARTIST, set())

    assert mappings == []
    mass.music.get_provider_instances.assert_not_called()


def test_discogs_external_id() -> None:
    """Pick the Discogs id of the item's kind out of its links, if there is one."""
    urls = [BANDCAMP, "https://www.discogs.com/master/21491", DISCOGS_ARTIST]
    assert discogs_external_id(urls, MediaType.ARTIST) == (ExternalID.DISCOGS, "3840")
    assert discogs_external_id(urls, MediaType.ALBUM) is None
    assert discogs_external_id(["https://www.discogs.com/release/1119453"], MediaType.ALBUM) == (
        ExternalID.DISCOGS,
        "1119453",
    )
