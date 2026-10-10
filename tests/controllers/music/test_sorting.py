"""Tests for the typed sort of library listings and the legacy sort keys."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.api import SortOptionInfo
from music_assistant_models.enums import AlbumType, MediaType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemCollection,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.constants import (
    FAVORITE_TIMESTAMP_SORT_KEYS,
    LEGACY_SORT_KEYS,
)
from music_assistant.controllers.music.sorting import (
    MEDIA_TYPE_SORT_FIELDS,
    SORT_FIELD_DEFINITIONS,
    get_default_direction,
    get_sort_options_for_media_type,
)
from music_assistant.mass import MusicAssistant

# the legacy key of ARTIST_NAME differs per media type
LEGACY_ARTIST_NAME_KEYS = {
    MediaType.ALBUM: "album_artist_name",
    MediaType.TRACK: "track_artist_name",
}


@pytest.fixture(scope="module", name="mass")
def mass_fixture(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Return the module-scoped database-only Music Assistant fixture."""
    return music_mass_module


@contextmanager
def _without_cache_store(mass: MusicAssistant) -> Iterator[None]:
    """Let the collections query run on the database-only fixture, which has no cache store."""
    with (
        patch.object(mass.cache, "get", new_callable=AsyncMock, return_value=None),
        patch.object(mass.cache, "set", new_callable=AsyncMock),
    ):
        yield


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

    for idx, (collection_name, year) in enumerate(
        (
            ("Sort Collection A", 2010),
            ("Sort Collection A", 2012),
            ("Sort Collection B", 2020),
            ("Sort Collection B", 2005),
        )
    ):
        await mass.music.albums.add_item_to_library(
            Album(
                item_id="0",
                provider="library",
                name=f"Collection Sort Album {idx}",
                album_type=AlbumType.ALBUM,
                year=year,
                provider_mappings={_mapping()},
                artists=UniqueList([artists["Zebra Artist"]]),
                metadata=MediaItemMetadata(
                    collections=UniqueList([MediaItemCollection(title=collection_name)])
                ),
            )
        )

    return mass


def test_every_media_type_sort_field_has_a_definition() -> None:
    """Every field listed per media type must have a definition."""
    for media_type, fields in MEDIA_TYPE_SORT_FIELDS.items():
        for field in fields:
            assert field in SORT_FIELD_DEFINITIONS, f"{field} for {media_type} has no definition"


def test_every_legacy_key_maps_to_a_defined_field() -> None:
    """Every legacy key maps to a defined field, with a direction only where the field has one."""
    for key, (field, direction) in LEGACY_SORT_KEYS.items():
        definition = SORT_FIELD_DEFINITIONS[field]
        assert (direction is not None) == definition.supports_direction, key
        assert key.endswith("_desc") == (direction == SortDirection.DESC), key


def test_legacy_keys_cover_every_offered_field(mass: MusicAssistant) -> None:
    """Every field a media type offers is reachable through its legacy key, in both directions."""
    for media_type, fields in MEDIA_TYPE_SORT_FIELDS.items():
        controller = mass.music.get_controller(media_type)
        for field in fields:
            key = (
                LEGACY_ARTIST_NAME_KEYS[media_type]
                if field == SortField.ARTIST_NAME
                else field.value
            )
            assert controller.resolve_sort(order_by=key) == (field, LEGACY_SORT_KEYS[key][1], None)
            if SORT_FIELD_DEFINITIONS[field].supports_direction:
                assert controller.resolve_sort(order_by=f"{key}_desc") == (
                    field,
                    SortDirection.DESC,
                    None,
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
    """Album sort options are the shared definitions, including the album-specific fields."""
    options = get_sort_options_for_media_type(MediaType.ALBUM)
    assert all(isinstance(option, SortOptionInfo) for option in options)
    fields = {option.field for option in options}
    assert SortField.ARTIST_NAME in fields
    assert SortField.YEAR in fields
    assert SortField.RANDOM in fields
    assert SortField.RANDOM_PLAY_COUNT in fields


def test_get_sort_options_for_genre_includes_supported_fields() -> None:
    """Genre sort options include timestamp and play-count fields."""
    options = get_sort_options_for_media_type(MediaType.GENRE)
    fields = {option.field for option in options}
    assert SortField.TIMESTAMP_ADDED in fields
    assert SortField.TIMESTAMP_MODIFIED in fields
    assert SortField.LAST_PLAYED in fields
    assert SortField.PLAY_COUNT in fields
    assert SortField.RANDOM_PLAY_COUNT in fields


def test_get_sort_options_random_field_does_not_support_direction() -> None:
    """RANDOM sort options must be marked as not supporting a direction."""
    options = get_sort_options_for_media_type(MediaType.TRACK)
    random_option = next(o for o in options if o.field == SortField.RANDOM)
    assert random_option.supports_direction is False
    assert random_option.default_direction is None


@pytest.mark.asyncio
async def test_get_sort_options_api_matches_media_type(mass: MusicAssistant) -> None:
    """The get_sort_options() API on a controller should match its own media type's options."""
    result = await mass.music.albums.get_sort_options()
    expected = get_sort_options_for_media_type(MediaType.ALBUM)
    assert [o.field for o in result] == [o.field for o in expected]


def test_resolve_sort_defaults_and_precedence(mass: MusicAssistant) -> None:
    """Nothing requested means the listing default; typed parameters win over the legacy key."""
    controller = mass.music.albums
    assert controller.resolve_sort() == (SortField.SORT_NAME, None, None)
    assert controller.resolve_sort(default=None) == (None, None, None)
    assert controller.resolve_sort(sort_direction=SortDirection.DESC) == (
        SortField.SORT_NAME,
        SortDirection.DESC,
        None,
    )
    assert controller.resolve_sort(SortField.YEAR) == (SortField.YEAR, None, None)
    assert controller.resolve_sort(order_by="year_desc") == (
        SortField.YEAR,
        SortDirection.DESC,
        None,
    )
    assert controller.resolve_sort(SortField.NAME, SortDirection.DESC, "year_desc") == (
        SortField.NAME,
        SortDirection.DESC,
        None,
    )


def test_resolve_sort_favorite_timestamp_keys(mass: MusicAssistant) -> None:
    """The legacy favorite_timestamp keys resolve to a sort on the user's favorite moment."""
    controller = mass.music.tracks
    for key, direction in FAVORITE_TIMESTAMP_SORT_KEYS.items():
        assert controller.resolve_sort(order_by=key) == (None, None, direction)
    query, _ = controller._build_final_query([], [], favorite_sort=SortDirection.DESC)
    assert query.endswith("tracks.item_id) DESC")
    assert "SELECT favorites.timestamp FROM favorites" in query


@pytest.mark.asyncio
async def test_resolve_sort_rejects_unsupported_field_for_media_type(
    mass: MusicAssistant,
) -> None:
    """A sort field not offered for a media type must raise InvalidDataError, typed or legacy."""
    # YEAR is not a valid sort field for genres (no year column on that table)
    with pytest.raises(InvalidDataError):
        await mass.music.genres.library_items(sort_field=SortField.YEAR)
    with pytest.raises(InvalidDataError):
        await mass.music.genres.library_items(order_by="year")


@pytest.mark.asyncio
async def test_resolve_sort_rejects_unknown_legacy_key(mass: MusicAssistant) -> None:
    """An unknown legacy sort key must raise InvalidDataError instead of being ignored."""
    with pytest.raises(InvalidDataError):
        await mass.music.tracks.library_items(order_by="not_a_sort_key")


@pytest.mark.asyncio
async def test_resolve_sort_accepts_supported_field_for_media_type(
    mass: MusicAssistant,
) -> None:
    """A sort_field listed for the media type must be accepted without raising."""
    # YEAR is a valid sort field for albums
    await mass.music.albums.library_items(
        sort_field=SortField.YEAR, sort_direction=SortDirection.ASC
    )
    await mass.music.genres.library_items(sort_field=SortField.TIMESTAMP_ADDED)
    await mass.music.genres.library_items(sort_field=SortField.TIMESTAMP_MODIFIED)
    await mass.music.genres.library_items(sort_field=SortField.LAST_PLAYED)
    await mass.music.genres.library_items(sort_field=SortField.PLAY_COUNT)
    await mass.music.genres.library_items(sort_field=SortField.RANDOM_PLAY_COUNT)
    await mass.music.tracks.library_items(favorite=True, order_by="favorite_timestamp_desc")


def test_sort_sql_qualifies_columns_and_applies_default_direction(mass: MusicAssistant) -> None:
    """The ORDER BY clause names the table and uses the field default when no direction is given."""
    tracks = mass.music.tracks
    assert tracks._get_sort_sql(SortField.NAME, None) == "tracks.search_name ASC"
    assert tracks._get_sort_sql(SortField.NAME, SortDirection.DESC) == "tracks.search_name DESC"
    assert tracks._get_sort_sql(SortField.TIMESTAMP_ADDED, None) == "tracks.timestamp_added DESC"
    assert tracks._get_sort_sql(SortField.RANDOM, SortDirection.DESC) == "RANDOM()"
    assert (
        tracks._get_sort_sql(SortField.RANDOM_PLAY_COUNT, None)
        == "COALESCE(tracks.play_count, 0), RANDOM()"
    )
    assert (
        tracks._get_sort_sql(SortField.ARTIST_NAME, SortDirection.DESC)
        == "artists.search_name DESC, tracks.search_name ASC"
    )
    with pytest.raises(InvalidDataError):
        mass.music.artists._get_sort_sql(SortField.ARTIST_NAME, None)


@pytest.mark.asyncio
async def test_collapsed_collections_sort_on_unqualified_columns(
    artist_sorted_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The collapsed listing orders its derived query on bare columns, NAME for other fields."""
    controller = artist_sorted_mass.music.albums
    caplog.set_level("WARNING", logger=controller.logger.name)
    sql_query, query_params = controller._build_final_query([], [], summary=True)

    async def adapt(field: SortField, direction: SortDirection | None) -> str:
        with _without_cache_store(artist_sorted_mass):
            return await controller._adapt_query_for_collections(
                sql_query, query_params, summary=True, sort_field=field, sort_direction=direction
            )

    assert (await adapt(SortField.NAME, SortDirection.DESC)).endswith(" ORDER BY search_name DESC")
    assert (await adapt(SortField.YEAR, None)).endswith(" ORDER BY year DESC")
    assert (await adapt(SortField.RANDOM_PLAY_COUNT, None)).endswith(
        " ORDER BY COALESCE(play_count, 0), RANDOM()"
    )
    assert (await adapt(SortField.ARTIST_NAME, SortDirection.DESC)).endswith(
        " ORDER BY search_name ASC"
    )
    assert "artist_name is not supported" in caplog.text


@pytest.mark.asyncio
async def test_collapsed_collections_fall_back_for_the_favorite_sort(
    artist_sorted_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A collapsed listing has no favorite moment to sort on and falls back to NAME."""
    controller = artist_sorted_mass.music.albums
    caplog.set_level("WARNING", logger=controller.logger.name)
    with (
        _without_cache_store(artist_sorted_mass),
        patch.object(
            artist_sorted_mass.music.database,
            "get_rows_from_query",
            new_callable=AsyncMock,
            return_value=[],
        ) as get_rows,
    ):
        await controller.get_library_items_by_query(
            favorite_sort=SortDirection.DESC, collapse_collections=True, summary=True
        )

    assert get_rows.await_args is not None
    assert get_rows.await_args.args[0].endswith(" ORDER BY search_name ASC")
    assert "favorite_timestamp is not supported" in caplog.text


@pytest.mark.asyncio
async def test_collapsed_album_collections_sort_by_year(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """Collapsed album collections sort on the earliest year of their albums."""
    with _without_cache_store(artist_sorted_mass):
        result = await artist_sorted_mass.music.albums.get_library_items_by_query(
            in_library_only=True,
            sort_field=SortField.YEAR,
            sort_direction=SortDirection.ASC,
            collapse_collections=True,
            summary=True,
        )
    assert [item.name for item in result] == [
        "Sort Album 0",
        "Sort Album 1",
        "Sort Album 2",
        "Sort Collection B",
        "Sort Collection A",
    ]


@pytest.mark.asyncio
async def test_localized_search_fallback_keeps_the_requested_sort(mass: MusicAssistant) -> None:
    """The retry on canonical names forwards the sort of the original request."""
    controller = mass.music.playlists
    translations = SimpleNamespace(
        reverse_lookup_media_names=AsyncMock(return_value={"Canonical Name"})
    )
    with (
        patch.object(mass, "translations", translations, create=True),
        patch.object(
            controller, "get_library_items_by_query", new_callable=AsyncMock, return_value=[]
        ) as get_items,
    ):
        await controller.library_items(search="Lokalisiert", order_by="timestamp_added_desc")

    assert get_items.await_count == 2
    retry_kwargs = get_items.await_args_list[1].kwargs
    assert retry_kwargs["search"] == "Canonical Name"
    assert retry_kwargs["sort_field"] == SortField.TIMESTAMP_ADDED
    assert retry_kwargs["sort_direction"] == SortDirection.DESC


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
    artist_names = [
        album.artists[0].name for album in result if album.name.startswith("Sort Album ")
    ]
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


def test_library_items_preserves_legacy_positional_arguments(mass: MusicAssistant) -> None:
    """Legacy order_by and existing filters remain positional across media controllers."""
    positional = (None, None, 500, 0, "sort_name", "provider", 7, True)
    controllers = (
        mass.music.albums,
        mass.music.artists,
        mass.music.audiobooks,
        mass.music.genres,
        mass.music.podcasts,
        mass.music.playlists,
        mass.music.radio,
        mass.music.tracks,
    )
    for controller in controllers:
        signature(controller.library_items).bind(*positional)

    signature(mass.music.albums.library_items).bind(*positional, None)
    signature(mass.music.artists.library_items).bind(*positional, False, None)
    signature(mass.music.genres.library_items).bind(*positional, None, None, None)
    signature(mass.music.tracks.library_items).bind(*positional, None)


@pytest.mark.asyncio
async def test_library_items_random_play_count_ignores_direction(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """A direction given with RANDOM_PLAY_COUNT is ignored, keeping its SQL order and paging."""
    controller = artist_sorted_mass.music.tracks
    with patch.object(
        artist_sorted_mass.music.database,
        "get_rows_from_query",
        new_callable=AsyncMock,
        return_value=[],
    ) as get_rows:
        await controller.library_items(
            sort_field=SortField.RANDOM_PLAY_COUNT,
            sort_direction=SortDirection.DESC,
            limit=5,
            offset=2,
            summary=False,
        )

    assert get_rows.await_args is not None
    query = get_rows.await_args.args[0]
    assert "COALESCE(tracks.play_count, 0), RANDOM()" in query
    assert "RANDOM() DESC" not in query
    assert "LIMIT 7" in query


@pytest.mark.asyncio
async def test_random_sort_paginates_collapsed_collections_after_aggregation(
    artist_sorted_mass: MusicAssistant,
) -> None:
    """Random sampling must not limit items before they are collapsed into collections."""
    controller = artist_sorted_mass.music.albums
    with (
        patch.object(
            controller, "_apply_random_subquery", wraps=controller._apply_random_subquery
        ) as sample,
        patch.object(
            controller,
            "_adapt_query_for_collections",
            new_callable=AsyncMock,
            return_value="SELECT 1",
        ) as adapt,
        patch.object(
            artist_sorted_mass.music.database,
            "get_rows_from_query",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        results = await controller.get_library_items_by_query(
            in_library_only=True,
            sort_field=SortField.RANDOM,
            limit=1,
            offset=1,
            collapse_collections=True,
        )

    sample.assert_not_called()
    assert results == []
    adapt.assert_awaited_once()


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
        sort_field=SortField.RANDOM_PLAY_COUNT,
        limit=5,
        offset=7,
    )
    query = query_parts[0]
    assert "ORDER BY COALESCE(tracks.play_count, 0), RANDOM()" in query
    assert "LIMIT 12" in query


def test_random_subquery_deduplicates_items_before_limit(mass: MusicAssistant) -> None:
    """Artist joins must not let duplicate item IDs consume the random sample limit."""
    query_parts: list[str] = []
    mass.music.tracks._apply_random_subquery(
        query_parts=query_parts,
        query_params={},
        join_parts=[
            "JOIN track_artists ON track_artists.track_id = tracks.item_id",
            "JOIN artists ON artists.item_id = track_artists.artist_id",
        ],
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=None,
        sort_field=SortField.RANDOM,
        limit=5,
        offset=2,
    )

    query = query_parts[0]
    assert "SELECT DISTINCT tracks.item_id FROM tracks JOIN track_artists" in query
    assert "LIMIT 7" in query
