"""
Tests for the library listing query builder (EXISTS-based provider filtering).

The provider/in-library filters in ``get_library_items_by_query`` are applied as
correlated EXISTS subqueries (instead of a JOIN + GROUP BY) so SQLite can stream
results straight from the sort index. These tests verify that:

* the new queries return the exact same items (and order) as the legacy
  JOIN + GROUP BY queries for a matrix of filter combinations
* the generated listing queries stream from the sort index without a
  temporary b-tree for sorting
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType, ArtistType, MediaType
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaCollection,
    MediaItemCollection,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.music.media.base import MediaControllerBase
from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def seeded_mass(
    music_mass_module: MusicAssistant,
) -> MusicAssistant:
    """Return a module-scoped database-only instance with a seeded library."""
    await _seed_library(music_mass_module)
    return music_mass_module


def _mapping(provider_instance: str = "prov_a_inst") -> ProviderMapping:
    """Create a provider mapping with a unique provider_item_id."""
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=True,
    )


async def _seed_library(mass: MusicAssistant) -> None:
    """Seed artists, albums and tracks covering all listing edge cases."""
    artists: list[Artist] = []
    for idx in range(1, 5):
        artist = Artist(
            item_id="0",
            provider="library",
            name=f"Artist {idx:02d}",
            provider_mappings={_mapping()},
            favorite=idx % 2 == 0,
        )
        artists.append(await mass.music.artists.add_item_to_library(artist))

    albums: list[Album] = []
    for idx in range(1, 6):
        album = Album(
            item_id="0",
            provider="library",
            name=f"Album {idx:02d}",
            album_type=AlbumType.ALBUM,
            provider_mappings={
                # some albums have multiple provider mappings (fanout in legacy JOIN)
                _mapping(),
                *([_mapping("prov_b_inst")] if idx % 2 == 0 else []),
            },
            artists=UniqueList([artists[idx % len(artists)]]),
            favorite=idx % 3 == 0,
        )
        albums.append(await mass.music.albums.add_item_to_library(album))

    for idx in range(1, 21):
        track = Track(
            item_id="0",
            provider="library",
            name=f"Track {idx:02d}",
            provider_mappings={
                _mapping(),
                *([_mapping("prov_b_inst")] if idx % 4 == 0 else []),
            },
            artists=UniqueList([artists[idx % len(artists)]]),
            album=albums[idx % len(albums)],
            disc_number=1,
            track_number=idx,
            favorite=idx % 5 == 0,
        )
        db_track = await mass.music.tracks.add_item_to_library(track)
        # track 3 appears on two albums (fanout in legacy album_tracks JOIN)
        if idx == 3:
            await mass.music.tracks._set_track_album(
                db_id=int(db_track.item_id),
                album=albums[4],
                disc_number=2,
                track_number=99,
            )

    # one track only has a mapping with in_library=0 (hidden from library listings)
    hidden = Track(
        item_id="0",
        provider="library",
        name="Track hidden",
        provider_mappings={_mapping()},
        artists=UniqueList([artists[0]]),
        favorite=True,
    )
    db_hidden = await mass.music.tracks.add_item_to_library(hidden)
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 0 "
        "WHERE item_id = :item_id AND media_type = 'track'",
        {"item_id": int(db_hidden.item_id)},
    )

    # genre mapping for some tracks
    for track_db_id in (1, 2, 3):
        await mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {"genre_id": 1, "media_id": track_db_id, "media_type": "track"},
        )
    # mark some items as played
    await mass.music.database.execute(
        "UPDATE tracks SET last_played = item_id WHERE item_id IN (2, 4, 6)"
    )
    await mass.music.database.commit()


def _legacy_tracks_base_query() -> str:
    """Return the legacy tracks base query (with the album_tracks JOIN)."""
    return """
        SELECT
            tracks.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', track_pm.provider_item_id,
                    'provider_domain', track_pm.provider_domain,
                        'provider_instance', track_pm.provider_instance,
                        'available', track_pm.available,
                        'audio_format', json(track_pm.audio_format),
                        'url', track_pm.url,
                        'details', track_pm.details,
                        'in_library', track_pm.in_library,
                        'is_unique', track_pm.is_unique
                )) FROM provider_mappings track_pm WHERE track_pm.item_id = tracks.item_id AND track_pm.media_type = 'track') AS provider_mappings,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', artists.item_id,
                'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist',
                    'external_ids', json((SELECT json_group_array(json_array(
                        external_id_lookup.external_id_type, external_id_lookup.external_id))
                        FROM external_id_lookup WHERE external_id_lookup.media_type = 'artist'
                        AND external_id_lookup.item_id = artists.item_id))
                )) FROM artists JOIN track_artists on track_artists.track_id = tracks.item_id  WHERE artists.item_id = track_artists.artist_id) AS artists,
            (SELECT
                json_object(
                'item_id', albums.item_id,
                'provider', 'library',
                    'name', albums.name,
                    'sort_name', albums.sort_name,
                    'media_type', 'album',
                    'year', albums.year,
                    'disc_number', album_tracks.disc_number,
                    'track_number', album_tracks.track_number,
                    'images', json_extract(albums.metadata, '$.images')
                ) FROM albums WHERE albums.item_id = album_tracks.album_id) AS track_album
            FROM tracks
            LEFT JOIN album_tracks on album_tracks.track_id = tracks.item_id
            """


def _legacy_query(  # noqa: PLR0913
    controller: MediaControllerBase[Any],
    *,
    base_query: str | None = None,
    favorite: bool | None = None,
    search: str | None = None,
    provider_filter: list[str] | None = None,
    in_library_only: bool = False,
    played_only: bool = False,
    genre_ids: list[int] | None = None,
    extra_query_parts: list[str] | None = None,
    extra_join_parts: list[str] | None = None,
    order_by: str | None = "sort_name",
) -> tuple[str, dict[str, Any]]:
    """Build a listing query the way the legacy (JOIN + GROUP BY) builder did."""
    table = controller.db_table
    query_parts: list[str] = list(extra_query_parts or [])
    join_parts: list[str] = list(extra_join_parts or [])
    params: dict[str, Any] = {}
    if search:
        query_parts.append(f"{table}.search_name LIKE :search")
        params["search"] = f"%{create_safe_string(search, True, True)}%"
    if favorite is not None:
        query_parts.append(f"{table}.favorite = :favorite")
        params["favorite"] = favorite
    if played_only:
        query_parts.append(f"{table}.last_played > 0")
    if genre_ids:
        params["genre_ids"] = genre_ids
        params["genre_media_type"] = controller.media_type.value
        query_parts.append(
            f"EXISTS(SELECT 1 FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} gm "
            f"WHERE gm.media_id = {table}.item_id "
            "AND gm.media_type = :genre_media_type AND gm.genre_id IN :genre_ids)"
        )
    if provider_filter:
        provider_conditions = []
        for idx, prov in enumerate(provider_filter):
            provider_conditions.append(f"provider_mappings.provider_instance = :prov_{idx}")
            params[f"prov_{idx}"] = prov
        params["provider_media_type"] = controller.media_type.value
        in_library_clause = "AND provider_mappings.in_library = 1 " if in_library_only else ""
        join_parts.append(
            f"JOIN provider_mappings ON provider_mappings.item_id = {table}.item_id "
            "AND provider_mappings.media_type = :provider_media_type "
            f"{in_library_clause}"
            f"AND ({' OR '.join(provider_conditions)})"
        )
    elif in_library_only:
        params["provider_media_type"] = controller.media_type.value
        join_parts.append(
            f"JOIN provider_mappings ON provider_mappings.item_id = {table}.item_id "
            "AND provider_mappings.media_type = :provider_media_type "
            "AND provider_mappings.in_library = 1"
        )
    sql_query = base_query if base_query is not None else controller.base_query[0]
    if join_parts:
        sql_query += f" {' '.join(join_parts)} "
    if query_parts:
        sql_query += " WHERE " + " AND ".join(
            x[5:] if x.lower().startswith("where ") else x for x in query_parts
        )
    sql_query += f" GROUP BY {table}.item_id"
    if order_by:
        sort_key = {
            "sort_name": "search_sort_name ASC",
            "sort_name_desc": "search_sort_name DESC",
            "timestamp_added": "timestamp_added ASC",
            "timestamp_added_desc": "timestamp_added DESC",
            "last_played": "last_played ASC",
            "last_played_desc": "last_played DESC",
        }[order_by]
        sql_query += f" ORDER BY {sort_key}"
    return sql_query, params


async def _compare(
    mass: MusicAssistant,
    controller: MediaControllerBase[Any],
    *,
    legacy_base_query: str | None = None,
    limit: int = 500,
    offset: int = 0,
    **filter_kwargs: Any,
) -> list[Any]:
    """Run the new and legacy listing queries and assert identical items/order."""
    legacy_sql, legacy_params = _legacy_query(
        controller, base_query=legacy_base_query, **filter_kwargs
    )
    legacy_rows = await mass.music.database.get_rows_from_query(
        legacy_sql, legacy_params, limit=limit, offset=offset
    )
    order_by = filter_kwargs.pop("order_by", "sort_name")
    new_items = await controller.get_library_items_by_query(
        order_by=order_by, limit=limit, offset=offset, **filter_kwargs
    )
    assert [str(row["item_id"]) for row in legacy_rows] == [x.item_id for x in new_items]
    # provider mappings must be hydrated identically
    for row, item in zip(legacy_rows, new_items, strict=True):
        legacy_mappings = {
            (m["provider_instance"], m["item_id"]) for m in json.loads(row["provider_mappings"])
        }
        new_mappings = {(m.provider_instance, m.item_id) for m in item.provider_mappings}
        assert legacy_mappings == new_mappings
    assert isinstance(new_items, list)
    return new_items


FILTER_MATRIX: list[dict[str, Any]] = [
    {"in_library_only": True},
    {"in_library_only": True, "favorite": True},
    {"in_library_only": True, "search": "02"},
    {"in_library_only": True, "search": "rack"},
    {"in_library_only": True, "search": "Track 02"},
    {"in_library_only": True, "provider_filter": ["prov_b_inst"]},
    {"in_library_only": True, "provider_filter": ["prov_a_inst", "prov_b_inst"]},
    {"provider_filter": ["prov_b_inst"]},
    {"in_library_only": True, "favorite": False, "search": "0", "order_by": "timestamp_added"},
    {
        "in_library_only": True,
        "favorite": True,
        "provider_filter": ["prov_a_inst"],
        "search": "1",
    },
    {"in_library_only": True, "order_by": "sort_name_desc"},
    {"in_library_only": True, "order_by": "timestamp_added_desc"},
]


@pytest.mark.parametrize("filter_kwargs", FILTER_MATRIX)
async def test_artist_listing_matches_legacy(
    seeded_mass: MusicAssistant, filter_kwargs: dict[str, Any]
) -> None:
    """Artist listings return the same items/order as the legacy JOIN queries."""
    await _compare(seeded_mass, seeded_mass.music.artists, **dict(filter_kwargs))


@pytest.mark.parametrize("filter_kwargs", FILTER_MATRIX)
async def test_album_listing_matches_legacy(
    seeded_mass: MusicAssistant, filter_kwargs: dict[str, Any]
) -> None:
    """Album listings return the same items/order as the legacy JOIN queries."""
    await _compare(seeded_mass, seeded_mass.music.albums, **dict(filter_kwargs))


@pytest.mark.parametrize("filter_kwargs", FILTER_MATRIX)
async def test_track_listing_matches_legacy(
    seeded_mass: MusicAssistant, filter_kwargs: dict[str, Any]
) -> None:
    """Track listings return the same items/order as the legacy JOIN queries."""
    await _compare(
        seeded_mass,
        seeded_mass.music.tracks,
        legacy_base_query=_legacy_tracks_base_query(),
        **dict(filter_kwargs),
    )


async def test_track_listing_with_pagination_matches_legacy(seeded_mass: MusicAssistant) -> None:
    """Paged track listings return the same items/order as the legacy queries."""
    for offset in (0, 5, 15):
        await _compare(
            seeded_mass,
            seeded_mass.music.tracks,
            legacy_base_query=_legacy_tracks_base_query(),
            limit=5,
            offset=offset,
            in_library_only=True,
        )


async def test_track_listing_played_only_matches_legacy(seeded_mass: MusicAssistant) -> None:
    """played_only track listings return the same items as the legacy queries."""
    items = await _compare(
        seeded_mass,
        seeded_mass.music.tracks,
        legacy_base_query=_legacy_tracks_base_query(),
        in_library_only=True,
        played_only=True,
    )
    assert len(items) == 3


async def test_track_listing_genre_filter_matches_legacy(seeded_mass: MusicAssistant) -> None:
    """Genre-filtered track listings return the same items as the legacy queries."""
    items = await _compare(
        seeded_mass,
        seeded_mass.music.tracks,
        legacy_base_query=_legacy_tracks_base_query(),
        in_library_only=True,
        genre_ids=[1],
    )
    assert len(items) == 3


async def test_track_listing_artist_join_matches_legacy(seeded_mass: MusicAssistant) -> None:
    """Artist-name search (with join fanout) still dedupes via GROUP BY."""
    join = (
        "JOIN track_artists ON track_artists.track_id = tracks.item_id "
        "JOIN artists ON artists.item_id = track_artists.artist_id "
        "AND artists.search_name LIKE :search_artist"
    )
    legacy_sql, legacy_params = _legacy_query(
        seeded_mass.music.tracks,
        base_query=_legacy_tracks_base_query(),
        in_library_only=True,
        extra_join_parts=[join],
    )
    legacy_params["search_artist"] = "%artist02%"
    legacy_rows = await seeded_mass.music.database.get_rows_from_query(legacy_sql, legacy_params)
    new_items = await seeded_mass.music.tracks.get_library_items_by_query(
        order_by="sort_name",
        in_library_only=True,
        extra_join_parts=[join],
        extra_query_params={"search_artist": "%artist02%"},
    )
    assert [str(row["item_id"]) for row in legacy_rows] == [x.item_id for x in new_items]
    assert len(new_items) > 0


async def test_track_artist_name_sorting(seeded_mass: MusicAssistant) -> None:
    """Tracks can be sorted by artist name, with secondary sorting by track name."""
    # Test ascending artist name sort
    tracks_asc = await seeded_mass.music.tracks.library_items(order_by="track_artist_name")
    # Build list of (artist_name, track_name) tuples for validation
    artist_track_pairs_asc = [(t.artists[0].name, t.name) for t in tracks_asc if t.artists]
    # Sort should be by artist name first, then track name
    expected_asc = sorted(artist_track_pairs_asc, key=lambda x: (x[0], x[1]))
    assert artist_track_pairs_asc == expected_asc, "Should sort by artist name, then track name"
    assert len(artist_track_pairs_asc) > 0, "Should return tracks with artists"

    # Test descending artist name sort
    tracks_desc = await seeded_mass.music.tracks.library_items(order_by="track_artist_name_desc")
    artist_track_pairs_desc = [(t.artists[0].name, t.name) for t in tracks_desc if t.artists]
    # DESC sort: artist name descending, but track name still ascending within each artist
    grouped: dict[str, list[str]] = {}
    for pair in artist_track_pairs_asc:
        artist, track = pair
        if artist not in grouped:
            grouped[artist] = []
        grouped[artist].append(track)
    # Sort artists descending, tracks within artist ascending
    expected_desc = []
    for artist in sorted(grouped.keys(), reverse=True):
        for track in sorted(grouped[artist]):
            expected_desc.append((artist, track))
    assert artist_track_pairs_desc == expected_desc, "Should sort by artist name descending"


async def test_album_artist_name_sorting(seeded_mass: MusicAssistant) -> None:
    """Albums can be sorted by artist name, with secondary sorting by year."""
    # Test ascending artist name sort
    albums_asc = await seeded_mass.music.albums.library_items(order_by="album_artist_name")
    # Build list of (artist_name, album_name, year) tuples for validation
    artist_album_pairs_asc = [(a.artists[0].name, a.name, a.year) for a in albums_asc if a.artists]
    # Sort should be by artist name first, then year descending
    expected_asc = sorted(artist_album_pairs_asc, key=lambda x: (x[0], -x[2] if x[2] else 0))
    assert artist_album_pairs_asc == expected_asc, "Should sort by artist name, then year desc"
    assert len(artist_album_pairs_asc) > 0, "Should return albums with artists"

    # Test descending artist name sort
    albums_desc = await seeded_mass.music.albums.library_items(order_by="album_artist_name_desc")
    artist_album_pairs_desc = [
        (a.artists[0].name, a.name, a.year) for a in albums_desc if a.artists
    ]
    # SQL: artists.search_name DESC, year DESC - both descending
    expected_desc = sorted(artist_album_pairs_asc, key=lambda x: x[2] or 0, reverse=True)
    expected_desc = sorted(expected_desc, key=lambda x: x[0], reverse=True)
    assert artist_album_pairs_desc == expected_desc, "Should sort by artist name descending"


async def test_hidden_track_excluded_from_library_listing(seeded_mass: MusicAssistant) -> None:
    """A track whose only mapping has in_library=0 is hidden from library listings."""
    items = await seeded_mass.music.tracks.get_library_items_by_query(in_library_only=True)
    assert "Track hidden" not in {x.name for x in items}
    # ...but still returned when not filtering on library membership
    items = await seeded_mass.music.tracks.get_library_items_by_query()
    assert "Track hidden" in {x.name for x in items}


async def test_random_order_applies_filters(seeded_mass: MusicAssistant) -> None:
    """The random-order subquery applies the provider/in-library filters."""
    items = await seeded_mass.music.tracks.get_library_items_by_query(
        order_by="random", in_library_only=True, provider_filter=["prov_b_inst"]
    )
    expected = await seeded_mass.music.tracks.get_library_items_by_query(
        in_library_only=True, provider_filter=["prov_b_inst"]
    )
    assert {x.item_id for x in items} == {x.item_id for x in expected}


async def test_album_tracks_returns_album_scoped_disc_and_track_numbers(
    seeded_mass: MusicAssistant,
) -> None:
    """get_library_album_tracks returns the requested album's disc/track numbers."""
    # track 3 is on two albums with different disc/track numbers
    row = await seeded_mass.music.database.get_row("tracks", {"name": "Track 03"})
    assert row is not None
    track_db_id = int(row["item_id"])
    album_track_rows = await seeded_mass.music.database.get_rows(
        DB_TABLE_ALBUM_TRACKS, {"track_id": track_db_id}
    )
    assert len(album_track_rows) == 2
    for album_track_row in album_track_rows:
        album_tracks = await seeded_mass.music.albums.get_library_album_tracks(
            album_track_row["album_id"]
        )
        track = next(x for x in album_tracks if int(x.item_id) == track_db_id)
        assert track.album is not None
        assert int(track.album.item_id) == album_track_row["album_id"]
        assert track.disc_number == album_track_row["disc_number"]
        assert track.track_number == album_track_row["track_number"]


async def test_album_tracks_matches_legacy(seeded_mass: MusicAssistant) -> None:
    """get_library_album_tracks returns the same tracks as the legacy album JOIN query."""
    albums = await seeded_mass.music.albums.get_library_items_by_query(in_library_only=True)
    for album in albums:
        legacy_sql, legacy_params = _legacy_query(
            seeded_mass.music.tracks,
            base_query=_legacy_tracks_base_query(),
            extra_query_parts=["WHERE album_tracks.album_id = :album_id"],
            order_by=None,
        )
        legacy_params["album_id"] = int(album.item_id)
        legacy_rows = await seeded_mass.music.database.get_rows_from_query(
            legacy_sql, legacy_params
        )
        new_items = await seeded_mass.music.albums.get_library_album_tracks(album.item_id)
        assert sorted(str(row["item_id"]) for row in legacy_rows) == sorted(
            x.item_id for x in new_items
        )


async def test_audiobook_listing_resume_info(seeded_mass: MusicAssistant) -> None:
    """Audiobook listings dedupe playlog rows and surface the most recent resume info."""
    audiobook = Audiobook(
        item_id="0",
        provider="library",
        name="Book 01",
        provider_mappings={_mapping()},
    )
    db_book = await seeded_mass.music.audiobooks.add_item_to_library(audiobook)
    # two playlog rows for the same book (e.g. two users/providers)
    for userid, timestamp, seconds_played in (("user-a", 100, 60), ("user-b", 200, 120)):
        await seeded_mass.music.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": db_book.item_id,
                "provider": "library",
                "media_type": MediaType.AUDIOBOOK.value,
                "name": db_book.name,
                "timestamp": timestamp,
                "fully_played": False,
                "seconds_played": seconds_played,
                "userid": userid,
                "user_initiated": True,
            },
        )
    await seeded_mass.music.database.commit()
    items = await seeded_mass.music.audiobooks.get_library_items_by_query(in_library_only=True)
    books = [x for x in items if x.item_id == db_book.item_id]
    # the playlog join may not fan out the result set
    assert len(books) == 1
    # the most recent playlog entry wins
    assert books[0].resume_position_ms == 120 * 1000


async def test_audiobook_collections_collapse_and_preserve_order(
    mass: MusicAssistant,
) -> None:
    """Collapsed audiobook collections remain ordered and reuse the cached row shape."""
    controller = mass.music.audiobooks
    assert (
        await controller.get_library_items_by_query(
            in_library_only=True,
            collapse_collections=True,
        )
        == []
    )

    collection_books = (
        ("Alpha 2", "Alpha Series", 2.0),
        ("Alpha 1.5", "Alpha Series", "1.5"),
        ("Alpha 1", "Alpha Series", 1.0),
        ("Beta 1", "Beta Series", 1.0),
    )
    for name, collection_name, sequence in collection_books:
        await controller.add_item_to_library(
            Audiobook(
                item_id="0",
                provider="library",
                name=name,
                provider_mappings={_mapping()},
                metadata=MediaItemMetadata(
                    collections=UniqueList(
                        [MediaItemCollection(title=collection_name, sequence=sequence)]
                    )
                ),
            )
        )
    await controller.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Standalone",
            provider_mappings={_mapping()},
        )
    )

    items = await controller.get_library_items_by_query(
        in_library_only=True,
        order_by="name_desc",
        collapse_collections=True,
    )
    collections = {item.name: item for item in items if isinstance(item, MediaCollection)}
    assert list(collections) == ["Beta Series", "Alpha Series"]
    assert [item.name for item in collections["Alpha Series"].items] == [
        "Alpha 1",
        "Alpha 1.5",
        "Alpha 2",
    ]

    with patch.object(
        mass.music.database,
        "get_rows_from_query",
        wraps=mass.music.database.get_rows_from_query,
    ) as get_rows:
        search_results = await controller.get_library_items_by_query(
            search="Series",
            in_library_only=True,
            order_by="name_desc",
            collapse_collections=True,
        )
    assert [item.name for item in search_results] == ["Beta Series", "Alpha Series"]
    assert get_rows.await_count == 1

    collection = await controller.get_collection(collections["Alpha Series"].item_id)
    assert [item.name for item in collection.items] == ["Alpha 1", "Alpha 1.5", "Alpha 2"]
    assert all(isinstance(item, Audiobook) for item in collection.items)


async def test_artist_audiobooks_collapse_collections(
    mass: MusicAssistant,
) -> None:
    """Artist audiobook listings collapse collections while preserving the artist scope."""
    author = Artist(
        item_id="0",
        provider="library",
        name="Test Author",
        provider_mappings={_mapping()},
        artist_type=ArtistType.AUTHOR,
    )
    author = await mass.music.artists.add_item_to_library(author)

    for name, sequence in (("Book 2", 2), ("Book 1", 1)):
        await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id="0",
                provider="library",
                name=name,
                provider_mappings={_mapping()},
                authors=UniqueList([author]),
                metadata=MediaItemMetadata(
                    collections=UniqueList(
                        [MediaItemCollection(title="Test Collection", sequence=sequence)]
                    )
                ),
            )
        )

    await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Standalone",
            provider_mappings={_mapping()},
            authors=UniqueList([author]),
        )
    )

    result = await mass.music.artists.audiobooks(
        author.item_id,
        author.provider,
        author.artist_type,
        in_library_only=True,
        collapse_collections=True,
    )

    assert len(result) == 2

    collection = next(item for item in result if isinstance(item, MediaCollection))
    standalone = next(item for item in result if isinstance(item, Audiobook))

    assert collection.name == "Test Collection"
    assert [item.name for item in collection.items] == ["Book 1", "Book 2"]
    assert standalone.name == "Standalone"


async def test_listing_queries_stream_from_sort_index(seeded_mass: MusicAssistant) -> None:
    """Default library listings stream from the sort index without a temp b-tree."""
    database = seeded_mass.music.database
    # drop the planner statistics so SQLite falls back to its default (large) table
    # size estimates: with only a handful of seeded rows it would (rightfully) just
    # scan the table, while the streaming-from-sort-index behaviour is what matters
    # for large libraries
    await database.execute("ANALYZE")
    await database.execute("DELETE FROM sqlite_stat1")
    await database.execute("ANALYZE sqlite_master")
    try:
        for controller in (
            seeded_mass.music.artists,
            seeded_mass.music.albums,
            seeded_mass.music.tracks,
        ):
            captured: dict[str, Any] = {}
            orig = database.get_rows_from_query

            async def spy(
                query: str,
                params: dict[str, Any] | None = None,
                limit: int = 500,
                offset: int = 0,
                _captured: dict[str, Any] = captured,
                _orig: Any = orig,
            ) -> list[Any]:
                _captured["query"], _captured["params"] = query, params
                result: list[Any] = await _orig(query, params, limit=limit, offset=offset)
                return result

            with patch.object(database, "get_rows_from_query", spy):
                await controller.get_library_items_by_query(
                    order_by="sort_name", in_library_only=True
                )
            plan_rows = await database.get_rows_from_query(
                f"EXPLAIN QUERY PLAN {captured['query']} LIMIT 500 OFFSET 0",
                captured["params"],
                limit=0,
            )
            details = [row["detail"] for row in plan_rows]
            table = controller.db_table
            assert any(
                f"SCAN {table} USING INDEX {table}_search_sort_name_idx" in detail
                for detail in details
            ), details
            # no temp b-tree may be used to sort the (potentially huge) main result;
            # correlated subqueries only sort the few rows of a single item
            top_level = [row["detail"] for row in plan_rows if row["parent"] == 0]
            assert not any("TEMP B-TREE" in detail.upper() for detail in top_level), details
    finally:
        # restore the real statistics for any tests running after this one
        await database.execute("ANALYZE")


async def test_search_uses_fts_index(seeded_mass: MusicAssistant) -> None:
    """Search terms of >= 3 chars are matched through the FTS index."""
    database = seeded_mass.music.database
    captured: dict[str, Any] = {}
    orig = database.get_rows_from_query

    async def spy(
        query: str,
        params: dict[str, Any] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Any]:
        captured["query"], captured["params"] = query, params
        return await orig(query, params, limit=limit, offset=offset)

    with patch.object(database, "get_rows_from_query", spy):
        items = await seeded_mass.music.tracks.get_library_items_by_query(
            search="rack", in_library_only=True
        )
    assert "tracks_fts MATCH" in captured["query"]
    # midword substring matching works the same as the LIKE scan did
    assert len(items) == 20

    # terms shorter than a trigram fall back to a LIKE scan
    with patch.object(database, "get_rows_from_query", spy):
        items = await seeded_mass.music.tracks.get_library_items_by_query(
            search="02", in_library_only=True
        )
    assert "tracks_fts" not in captured["query"]
    assert "search_name LIKE" in captured["query"]
    assert {x.name for x in items} == {"Track 02"}


async def test_fts_index_follows_item_changes(seeded_mass: MusicAssistant) -> None:
    """The FTS index reflects added, renamed and deleted library items."""
    database = seeded_mass.music.database
    artists = await seeded_mass.music.artists.get_library_items_by_query(limit=1)
    track = Track(
        item_id="0",
        provider="library",
        name="Bohemian Rhapsody",
        provider_mappings={_mapping()},
        artists=UniqueList([artists[0]]),
    )
    db_track = await seeded_mass.music.tracks.add_item_to_library(track)

    async def _search(term: str) -> set[str]:
        items = await seeded_mass.music.tracks.get_library_items_by_query(search=term)
        return {x.item_id for x in items}

    assert db_track.item_id in await _search("rhapsody")

    # a rename must be picked up by the index
    await database.execute(
        "UPDATE tracks SET name = 'Radio Ga Ga', search_name = 'radiogaga' "
        "WHERE item_id = :item_id",
        {"item_id": int(db_track.item_id)},
    )
    await database.commit()
    assert db_track.item_id not in await _search("rhapsody")
    assert db_track.item_id in await _search("gaga")

    # a delete must remove the item from the index
    await database.execute(
        "DELETE FROM tracks WHERE item_id = :item_id", {"item_id": int(db_track.item_id)}
    )
    await database.commit()
    assert db_track.item_id not in await _search("gaga")
