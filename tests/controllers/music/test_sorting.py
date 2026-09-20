"""Tests for the sort field definitions and metadata module."""

from __future__ import annotations

from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType, MediaType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.sorting import (
    MEDIA_TYPE_SORT_FIELDS,
    SORT_FIELD_DEFINITIONS,
    get_default_direction,
    get_sort_options_for_media_type,
)
from music_assistant.mass import MusicAssistant


@pytest.fixture(scope="module", name="mass")
def mass_fixture(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Return the module-scoped database-only Music Assistant fixture."""
    return music_mass_module


def _mapping() -> ProviderMapping:
    """Create a library-mapped ProviderMapping with a unique provider_item_id."""
    return ProviderMapping(
        item_id=uuid4().hex, provider_domain="test", provider_instance="test_inst", in_library=True
    )


@pytest.fixture(scope="module")
async def artist_sorted_mass(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Seed artists/tracks/albums to exercise typed ARTIST_NAME sorting and its JOIN."""
    mass = music_mass_module
    artists = {}
    for name in ("Zebra Artist", "Apple Artist", "Mango Artist"):
        artist = Artist(item_id="0", provider="library", name=name, provider_mappings={_mapping()})
        artists[name] = await mass.music.artists.add_item_to_library(artist)

    for idx, (artist_name, year) in enumerate(
        (("Zebra Artist", 2001), ("Apple Artist", 2002), ("Mango Artist", 2003))
    ):
        album = Album(
            item_id="0",
            provider="library",
            name=f"Sort Album {idx}",
            album_type=AlbumType.ALBUM,
            year=year,
            provider_mappings={_mapping()},
            artists=UniqueList([artists[artist_name]]),
        )
        await mass.music.albums.add_item_to_library(album)

        track = Track(
            item_id="0",
            provider="library",
            name=f"Sort Track {idx}",
            provider_mappings={_mapping()},
            artists=UniqueList([artists[artist_name]]),
        )
        await mass.music.tracks.add_item_to_library(track)

    for idx in range(30):
        await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name=f"Random Track {idx}",
                provider_mappings={_mapping()},
                artists=UniqueList([artists["Zebra Artist"]]),
            )
        )

    return mass


def test_every_media_type_sort_field_has_a_definition() -> None:
    """Every field listed per media type must have a corresponding SortFieldDefinition."""
    for media_type, fields in MEDIA_TYPE_SORT_FIELDS.items():
        for field in fields:
            assert field in SORT_FIELD_DEFINITIONS, (
                f"{field} for {media_type} has no SortFieldDefinition"
            )


def test_get_default_direction_uses_definition_default() -> None:
    """get_default_direction should return the field's configured default direction."""
    assert get_default_direction(SortField.TIMESTAMP_ADDED) == SortDirection.DESC
    assert get_default_direction(SortField.NAME) == SortDirection.ASC


def test_get_default_direction_falls_back_to_asc_for_random_fields() -> None:
    """Fields without a configured default direction (e.g. RANDOM) fall back to ASC."""
    assert get_default_direction(SortField.RANDOM) == SortDirection.ASC
    assert get_default_direction(SortField.RANDOM_PLAY_COUNT) == SortDirection.ASC


def test_get_sort_options_for_album_includes_artist_name_and_random() -> None:
    """Album sort options should include type-specific fields and the random fields."""
    options = get_sort_options_for_media_type(MediaType.ALBUM)
    fields = {option.field for option in options}
    assert SortField.ARTIST_NAME.value in fields
    assert SortField.YEAR.value in fields
    assert SortField.RANDOM.value in fields
    assert SortField.RANDOM_PLAY_COUNT.value in fields


def test_get_sort_options_for_genre_excludes_random_play_count() -> None:
    """Genres have no play_count column, so RANDOM_PLAY_COUNT must not be offered."""
    options = get_sort_options_for_media_type(MediaType.GENRE)
    fields = {option.field for option in options}
    assert SortField.RANDOM_PLAY_COUNT.value not in fields


def test_get_sort_options_random_field_does_not_support_direction() -> None:
    """RANDOM sort options must be marked as not supporting a direction."""
    options = get_sort_options_for_media_type(MediaType.TRACK)
    random_option = next(o for o in options if o.field == SortField.RANDOM.value)
    assert random_option.supports_direction is False
    assert random_option.default_direction is None


@pytest.mark.asyncio
async def test_resolve_sort_parameters_rejects_unsupported_field_for_media_type(
    mass: MusicAssistant,
) -> None:
    """A sort_field not listed for a media type must raise InvalidDataError."""
    # YEAR is not a valid sort field for genres (no year column on that table)
    with pytest.raises(InvalidDataError):
        await mass.music.genres.library_items(sort_field=SortField.YEAR)


@pytest.mark.asyncio
async def test_resolve_sort_parameters_accepts_supported_field_for_media_type(
    mass: MusicAssistant,
) -> None:
    """A sort_field listed for the media type must be accepted without raising."""
    # YEAR is a valid sort field for albums
    await mass.music.albums.library_items(
        sort_field=SortField.YEAR, sort_direction=SortDirection.ASC
    )


@pytest.mark.asyncio
async def test_resolve_sort_parameters_applies_default_direction(
    mass: MusicAssistant,
) -> None:
    """Omitting sort_direction should apply the field's configured default direction."""
    controller = mass.music.albums
    final_order_by = controller._resolve_sort_parameters(SortField.YEAR, None, None)
    assert final_order_by == "year:desc"


@pytest.mark.asyncio
async def test_parse_order_by_new_format(mass: MusicAssistant) -> None:
    """The new 'field:direction' format must parse into the matching enum values."""
    controller = mass.music.tracks
    assert controller._parse_order_by("name:desc") == (SortField.NAME, SortDirection.DESC)
    assert controller._parse_order_by("name:asc") == (SortField.NAME, SortDirection.ASC)


@pytest.mark.asyncio
async def test_parse_order_by_legacy_format(mass: MusicAssistant) -> None:
    """The legacy 'field_desc' format must still resolve to the same field/direction."""
    controller = mass.music.tracks
    assert controller._parse_order_by("name_desc") == (SortField.NAME, SortDirection.DESC)
    assert controller._parse_order_by("sort_name") == (SortField.SORT_NAME, SortDirection.ASC)


@pytest.mark.asyncio
async def test_parse_order_by_random_fields_have_no_direction(mass: MusicAssistant) -> None:
    """RANDOM and RANDOM_PLAY_COUNT never carry a direction."""
    controller = mass.music.tracks
    assert controller._parse_order_by("random") == (SortField.RANDOM, None)
    assert controller._parse_order_by("random_play_count") == (SortField.RANDOM_PLAY_COUNT, None)


@pytest.mark.asyncio
async def test_parse_order_by_invalid_value_returns_none(mass: MusicAssistant) -> None:
    """An unparsable order_by string should return None rather than raise."""
    controller = mass.music.tracks
    assert controller._parse_order_by("not_a_real_field") is None
    assert controller._parse_order_by("name:not_a_direction") is None
    assert controller._parse_order_by(None) is None


@pytest.mark.asyncio
async def test_get_sort_options_api_matches_media_type(mass: MusicAssistant) -> None:
    """The get_sort_options() API on a controller should match its own media type's options."""
    result = await mass.music.albums.get_sort_options()
    expected = get_sort_options_for_media_type(MediaType.ALBUM)
    assert [o.field for o in result] == [o.field for o in expected]


@pytest.mark.asyncio
async def test_library_items_typed_artist_name_sort_on_tracks(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """Typed sort_field=ARTIST_NAME must add the artist JOIN and order tracks by artist name."""
    result = await artist_sorted_mass.music.tracks.library_items(
        sort_field=SortField.ARTIST_NAME,
        sort_direction=SortDirection.ASC,
        search="Sort Track",
        summary=False,
    )
    artist_names = [track.artists[0].name for track in result]
    assert artist_names == ["Apple Artist", "Mango Artist", "Zebra Artist"]


@pytest.mark.asyncio
async def test_library_items_typed_artist_name_sort_on_albums(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """Typed sort_field=ARTIST_NAME must add the artist JOIN and order albums by artist name."""
    result = await artist_sorted_mass.music.albums.library_items(
        sort_field=SortField.ARTIST_NAME,
        sort_direction=SortDirection.ASC,
        search="Sort Album",
        summary=False,
    )
    artist_names = [album.artists[0].name for album in result]
    assert artist_names == ["Apple Artist", "Mango Artist", "Zebra Artist"]


@pytest.mark.asyncio
async def test_library_items_random_sort_supports_pagination(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """A random-sorted page with offset > 0 must still return rows (regression test)."""
    result = await artist_sorted_mass.music.tracks.library_items(
        sort_field=SortField.RANDOM, limit=5, offset=5, summary=False
    )
    assert len(result) == 5


def test_random_play_count_subquery_preserves_play_count_order(
    mass: MusicAssistant,
) -> None:
    """RANDOM_PLAY_COUNT must sort by play count before shuffling equal counts."""
    query_parts: list[str] = []
    mass.music.tracks._apply_random_subquery(
        query_parts=query_parts,
        query_params={},
        join_parts=[],
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=None,
        order_by="random_play_count",
        limit=5,
        offset=7,
    )
    query = query_parts[0]
    assert "ORDER BY COALESCE(play_count, 0), RANDOM()" in query
    assert "LIMIT 12" in query
