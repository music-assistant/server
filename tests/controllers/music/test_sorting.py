"""Tests for the sort field definitions and metadata module."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError

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


def test_parse_order_by_supports_new_field_direction_format() -> None:
    """The new 'field:direction' format must parse into the matching enum values."""


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
