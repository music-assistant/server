"""Integration tests for the GenreController (V3 schema).

Uses the ``mass`` fixture from ``tests/conftest.py`` which creates a full
MusicAssistant instance with a real SQLite database in a temporary directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    Genre,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DEFAULT_GENRE_MAPPING,
)
from music_assistant.controllers.media.genres import GenreController
from music_assistant.mass import MusicAssistant

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
async def mass(tmp_path_factory: pytest.TempPathFactory) -> AsyncGenerator[MusicAssistant, None]:
    """Class-scoped MusicAssistant instance (one per test class)."""
    tmp_path = tmp_path_factory.mktemp("genre_tests")
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    logging.getLogger("aiosqlite").level = logging.INFO
    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    await mass_instance.start()
    try:
        yield mass_instance
    finally:
        await mass_instance.stop()


@pytest.fixture(scope="class")
async def genre_ctrl(mass: MusicAssistant) -> GenreController:
    """Get the genre controller from a running MusicAssistant instance."""
    return mass.music.genres


def _make_genre(name: str, favorite: bool = False) -> Genre:
    """Create a Genre object for adding to the library."""
    return Genre(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=set(),
        favorite=favorite,
    )


async def _add_test_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal artist to the library."""
    artist = Artist(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=set(),
    )
    return await mass.music.artists.add_item_to_library(artist)


async def _add_test_track(mass: MusicAssistant, name: str) -> Track:
    """Add a minimal track to the library (creates an artist first)."""
    artist = await _add_test_artist(mass, f"Artist for {name}")
    track = Track(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    return await mass.music.tracks.add_item_to_library(track)


# ===================================================================
# Group B: Genre CRUD (14 tests)
# ===================================================================


class TestGenreCRUD:
    """Tests for adding, reading, updating, and removing genres."""

    async def test_add_genre(self, genre_ctrl: GenreController) -> None:
        """add_item_to_library returns Genre with numeric id and correct name."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Rock"))
        assert int(genre.item_id) > 0
        assert genre.name == "Rock"

    async def test_add_genre_creates_self_alias(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Genre has its own name in genre_aliases JSON column."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Blues"))
        # Check genre_aliases JSON column directly
        row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": int(genre.item_id)})
        assert row is not None
        aliases = json.loads(row["genre_aliases"])
        assert "Blues" in aliases

    async def test_add_genre_duplicate_updates(self, genre_ctrl: GenreController) -> None:
        """Adding the same genre with library id returns the same item_id (update, no duplicate)."""
        genre1 = await genre_ctrl.add_item_to_library(_make_genre("Jazz"))
        # Second add using the real library id (simulates re-adding same item)
        dup = Genre(
            item_id=genre1.item_id,
            provider="library",
            name="Jazz",
            provider_mappings=set(),
        )
        genre2 = await genre_ctrl.add_item_to_library(dup)
        assert genre1.item_id == genre2.item_id

    async def test_get_library_item(self, genre_ctrl: GenreController) -> None:
        """get_library_item returns Genre with genre_aliases populated."""
        created = await genre_ctrl.add_item_to_library(_make_genre("Funk"))
        fetched = await genre_ctrl.get_library_item(int(created.item_id))
        assert fetched.name == "Funk"
        assert fetched.genre_aliases is not None
        assert "Funk" in fetched.genre_aliases

    async def test_get_library_item_not_found(self, genre_ctrl: GenreController) -> None:
        """Raises MediaNotFoundError for nonexistent id."""
        with pytest.raises(MediaNotFoundError):
            await genre_ctrl.get_library_item(999999)

    async def test_update_smart_merge(self, genre_ctrl: GenreController) -> None:
        """Update with metadata merges without overwrite flag."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Reggae"))
        update = _make_genre("Reggae")
        update.favorite = True
        updated = await genre_ctrl.update_item_in_library(genre.item_id, update, overwrite=False)
        assert updated.favorite is True
        assert updated.name == "Reggae"

    async def test_update_overwrite(self, genre_ctrl: GenreController) -> None:
        """Update with overwrite=True replaces name."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("OldName"))
        update = _make_genre("NewName")
        updated = await genre_ctrl.update_item_in_library(genre.item_id, update, overwrite=True)
        assert updated.name == "NewName"

    async def test_update_ensures_self_alias(self, genre_ctrl: GenreController) -> None:
        """After name update, self-alias exists for new name."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("OldGenre"))
        update = _make_genre("RenamedGenre")
        updated = await genre_ctrl.update_item_in_library(genre.item_id, update, overwrite=True)
        assert updated.genre_aliases is not None
        assert "RenamedGenre" in updated.genre_aliases

    async def test_remove_genre(self, genre_ctrl: GenreController) -> None:
        """After remove, get_library_item raises MediaNotFoundError."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Ska"))
        await genre_ctrl.remove_item_from_library(genre.item_id)
        with pytest.raises(MediaNotFoundError):
            await genre_ctrl.get_library_item(int(genre.item_id))

    async def test_remove_cleans_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """After remove, genre_media_item_mapping entries for that genre are gone."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Dubstep"))
        genre_id = int(genre.item_id)
        # Add a media mapping first
        track = await _add_test_track(mass, "Dubstep Track")
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track.item_id, "Dubstep")
        # Now remove the genre
        await genre_ctrl.remove_item_from_library(genre.item_id)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} WHERE genre_id = :genre_id",
            {"genre_id": genre_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_library_items(self, genre_ctrl: GenreController) -> None:
        """Add 3 genres, returns all 3."""
        for name in ("Alpha", "Beta", "Gamma"):
            await genre_ctrl.add_item_to_library(_make_genre(name))
        items = await genre_ctrl.library_items(hide_empty=False)
        names = {g.name for g in items}
        assert {"Alpha", "Beta", "Gamma"}.issubset(names)

    async def test_library_items_search(self, genre_ctrl: GenreController) -> None:
        """Search 'country' returns only matching genres."""
        await genre_ctrl.add_item_to_library(_make_genre("Country"))
        await genre_ctrl.add_item_to_library(_make_genre("Metal"))
        items = await genre_ctrl.library_items(search="country", hide_empty=False)
        assert all("country" in g.name.lower() for g in items)

    async def test_library_items_rejects_genre_param(self, genre_ctrl: GenreController) -> None:
        """library_items(genre=1) raises ValueError."""
        with pytest.raises(ValueError, match="genre parameter is not supported"):
            await genre_ctrl.library_items(genre=1)

    async def test_library_count(self, genre_ctrl: GenreController) -> None:
        """Returns correct count; favorite_only=True filters."""
        await genre_ctrl.add_item_to_library(_make_genre("CountA"))
        await genre_ctrl.add_item_to_library(_make_genre("CountB", favorite=True))
        total = await genre_ctrl.library_count()
        assert total >= 2
        fav = await genre_ctrl.library_count(favorite_only=True)
        assert fav >= 1
        assert fav <= total


# ===================================================================
# Group C: Alias Operations (8 tests)
# ===================================================================


class TestAliasOperations:
    """Tests for add_alias, remove_alias string operations on genres."""

    async def test_add_alias(self, genre_ctrl: GenreController) -> None:
        """add_alias adds a string to genre_aliases."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Electronic"))
        updated = await genre_ctrl.add_alias(genre.item_id, "EDM")
        assert updated.genre_aliases is not None
        assert "EDM" in updated.genre_aliases
        assert "Electronic" in updated.genre_aliases

    async def test_add_alias_idempotent(self, genre_ctrl: GenreController) -> None:
        """Adding the same alias twice doesn't duplicate."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("House"))
        await genre_ctrl.add_alias(genre.item_id, "Deep House")
        updated = await genre_ctrl.add_alias(genre.item_id, "Deep House")
        assert updated.genre_aliases is not None
        assert list(updated.genre_aliases).count("Deep House") == 1

    async def test_add_alias_multiple(self, genre_ctrl: GenreController) -> None:
        """Multiple aliases can be added to a single genre."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Ambient"))
        await genre_ctrl.add_alias(genre.item_id, "Ambient Music")
        updated = await genre_ctrl.add_alias(genre.item_id, "Chill Ambient")
        assert updated.genre_aliases is not None
        assert "Ambient" in updated.genre_aliases
        assert "Ambient Music" in updated.genre_aliases
        assert "Chill Ambient" in updated.genre_aliases

    async def test_remove_alias(self, genre_ctrl: GenreController) -> None:
        """remove_alias removes a string from genre_aliases."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Techno"))
        await genre_ctrl.add_alias(genre.item_id, "Detroit Techno")
        updated = await genre_ctrl.remove_alias(genre.item_id, "Detroit Techno")
        assert updated.genre_aliases is not None
        assert "Detroit Techno" not in updated.genre_aliases
        assert "Techno" in updated.genre_aliases

    async def test_remove_self_alias_raises(self, genre_ctrl: GenreController) -> None:
        """Removing the genre's own name raises ValueError."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Soul"))
        with pytest.raises(ValueError, match="Cannot remove self-alias"):
            await genre_ctrl.remove_alias(genre.item_id, "Soul")

    async def test_remove_alias_cleans_media_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Removing an alias also removes media mappings that used that alias."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Latin"))
        await genre_ctrl.add_alias(genre.item_id, "Latin Pop")
        track = await _add_test_track(mass, "Latin Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "Latin Pop"
        )
        # Remove the alias
        await genre_ctrl.remove_alias(genre.item_id, "Latin Pop")
        # Check mapping is gone
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND alias = :alias",
            {"gid": int(genre.item_id), "alias": "Latin Pop"},
            limit=0,
        )
        assert len(rows) == 0

    async def test_add_alias_not_found(self, genre_ctrl: GenreController) -> None:
        """add_alias for nonexistent genre raises MediaNotFoundError."""
        with pytest.raises(MediaNotFoundError):
            await genre_ctrl.add_alias(999999, "NoGenre")

    async def test_remove_alias_not_found(self, genre_ctrl: GenreController) -> None:
        """remove_alias for nonexistent genre raises MediaNotFoundError."""
        with pytest.raises(MediaNotFoundError):
            await genre_ctrl.remove_alias(999999, "NoGenre")


# ===================================================================
# Group D: Media Mapping Operations (8 tests)
# ===================================================================


class TestMediaMappingOperations:
    """Tests for add_media_mapping and remove_media_mapping."""

    async def test_add_media_mapping_track(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mapping exists in genre_media_item_mapping table."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Pop"))
        track = await _add_test_track(mass, "Pop Track")
        await genre_ctrl.add_media_mapping(genre.item_id, MediaType.TRACK, track.item_id, "Pop")
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = :mt AND media_id = :mid",
            {
                "gid": int(genre.item_id),
                "mt": MediaType.TRACK.value,
                "mid": int(track.item_id),
            },
            limit=1,
        )
        assert len(rows) == 1
        assert rows[0]["alias"] == "Pop"

    async def test_add_media_mapping_idempotent(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Calling add_media_mapping twice doesn't raise (uses allow_replace)."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Grunge"))
        track = await _add_test_track(mass, "Grunge Song")
        await genre_ctrl.add_media_mapping(genre.item_id, MediaType.TRACK, track.item_id, "Grunge")
        await genre_ctrl.add_media_mapping(genre.item_id, MediaType.TRACK, track.item_id, "Grunge")

    async def test_remove_media_mapping_track(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mapping removed from DB."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Disco"))
        track = await _add_test_track(mass, "Disco Track")
        await genre_ctrl.add_media_mapping(genre.item_id, MediaType.TRACK, track.item_id, "Disco")
        await genre_ctrl.remove_media_mapping(genre.item_id, MediaType.TRACK, track.item_id)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = :mt AND media_id = :mid",
            {
                "gid": int(genre.item_id),
                "mt": MediaType.TRACK.value,
                "mid": int(track.item_id),
            },
            limit=1,
        )
        assert len(rows) == 0

    async def test_add_media_mapping_artist(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Artist mapping works correctly."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Funk2"))
        artist = await _add_test_artist(mass, "Funk Artist")
        await genre_ctrl.add_media_mapping(genre.item_id, MediaType.ARTIST, artist.item_id, "Funk2")
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = :mt AND media_id = :mid",
            {
                "gid": int(genre.item_id),
                "mt": MediaType.ARTIST.value,
                "mid": int(artist.item_id),
            },
            limit=1,
        )
        assert len(rows) == 1

    async def test_mapping_preserves_alias_string(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """The alias column records which alias caused the mapping."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Afrobeat"))
        await genre_ctrl.add_alias(genre.item_id, "Highlife")
        track = await _add_test_track(mass, "Afrobeat Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "Highlife"
        )
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT alias FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": int(genre.item_id), "mid": int(track.item_id)},
            limit=1,
        )
        assert len(rows) == 1
        assert rows[0]["alias"] == "Highlife"

    async def test_multiple_genres_same_track(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A track can be mapped to multiple genres."""
        genre1 = await genre_ctrl.add_item_to_library(_make_genre("Genre1"))
        genre2 = await genre_ctrl.add_item_to_library(_make_genre("Genre2"))
        track = await _add_test_track(mass, "Multi Genre Track")
        await genre_ctrl.add_media_mapping(genre1.item_id, MediaType.TRACK, track.item_id, "Genre1")
        await genre_ctrl.add_media_mapping(genre2.item_id, MediaType.TRACK, track.item_id, "Genre2")
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 2

    async def test_multiple_tracks_same_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Multiple tracks can be mapped to the same genre."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("SharedGenre"))
        track1 = await _add_test_track(mass, "Shared Track 1")
        track2 = await _add_test_track(mass, "Shared Track 2")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track1.item_id, "SharedGenre"
        )
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track2.item_id, "SharedGenre"
        )
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = 'track'",
            {"gid": int(genre.item_id)},
            limit=0,
        )
        assert len(rows) == 2

    async def test_remove_nonexistent_mapping(self, genre_ctrl: GenreController) -> None:
        """Removing a mapping that doesn't exist doesn't raise."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("NoMapping"))
        await genre_ctrl.remove_media_mapping(genre.item_id, MediaType.TRACK, 999999)


# ===================================================================
# Group E: sync_media_item_genres (8 tests)
# ===================================================================


class TestSyncMediaItemGenres:
    """Tests for sync_media_item_genres."""

    async def test_sync_creates_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """New genre created, mapping exists."""
        track = await _add_test_track(mass, "Sync Track 1")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"Psytrance"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRES} WHERE name = :name",
            {"name": "Psytrance"},
            limit=1,
        )
        assert len(rows) == 1

    async def test_sync_uses_existing_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """No duplicate genre created."""
        await genre_ctrl.add_item_to_library(_make_genre("Punk"))
        track = await _add_test_track(mass, "Sync Track 2")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"Punk"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRES} WHERE name = :name",
            {"name": "Punk"},
            limit=0,
        )
        assert len(rows) == 1

    async def test_sync_adds_new_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Multiple genres creates both mappings."""
        track = await _add_test_track(mass, "Sync Track 3")
        await genre_ctrl.sync_media_item_genres(
            MediaType.TRACK, track.item_id, {"SyncRock", "SyncJazz"}
        )
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 2

    async def test_sync_removes_stale_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Re-sync with subset removes stale mapping."""
        track = await _add_test_track(mass, "Sync Track 4")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncA", "SyncB"})
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncA"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 1

    async def test_sync_empty_set_removes_all(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Empty set removes all mappings."""
        track = await _add_test_track(mass, "Sync Track 5")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncX"})
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, set())
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 0

    async def test_sync_idempotent(self, mass: MusicAssistant, genre_ctrl: GenreController) -> None:
        """Second call with same set is a no-op."""
        track = await _add_test_track(mass, "Sync Track 6")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncIdem"})
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncIdem"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 1

    async def test_sync_skips_empty_names(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Empty and whitespace-only names are skipped."""
        track = await _add_test_track(mass, "Sync Track 7")
        await genre_ctrl.sync_media_item_genres(
            MediaType.TRACK, track.item_id, {"SyncValid", "", "  "}
        )
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 1

    async def test_sync_concurrent(self, mass: MusicAssistant, genre_ctrl: GenreController) -> None:
        """asyncio.gather with different sets doesn't crash."""
        track1 = await _add_test_track(mass, "Conc Track 1")
        track2 = await _add_test_track(mass, "Conc Track 2")
        await asyncio.gather(
            genre_ctrl.sync_media_item_genres(MediaType.TRACK, track1.item_id, {"ConcA"}),
            genre_ctrl.sync_media_item_genres(MediaType.TRACK, track2.item_id, {"ConcB"}),
        )

    async def test_sync_one_alias_maps_to_multiple_genres(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """One alias shared by two genres creates mappings to both (n:n)."""
        genre_a = await genre_ctrl.add_item_to_library(_make_genre("GenreA"))
        genre_b = await genre_ctrl.add_item_to_library(_make_genre("GenreB"))
        # Both genres claim "shared-alias"
        await genre_ctrl.add_alias(genre_a.item_id, "shared-alias")
        await genre_ctrl.add_alias(genre_b.item_id, "shared-alias")
        track = await _add_test_track(mass, "SharedAlias Track")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"shared-alias"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        mapped_genre_ids = {int(r["genre_id"]) for r in rows}
        assert int(genre_a.item_id) in mapped_genre_ids
        assert int(genre_b.item_id) in mapped_genre_ids


# ===================================================================
# Group F: promote_alias_to_genre (4 tests)
# ===================================================================


class TestPromoteAlias:
    """Tests for promote_alias_to_genre."""

    async def test_promote_alias(self, mass: MusicAssistant, genre_ctrl: GenreController) -> None:
        """New genre created, media mappings moved to new genre."""
        parent = await genre_ctrl.add_item_to_library(_make_genre("ParentGenre"))
        await genre_ctrl.add_alias(parent.item_id, "SubGenre")
        # Add a media mapping via the alias
        track = await _add_test_track(mass, "Promote Track")
        await genre_ctrl.add_media_mapping(
            parent.item_id, MediaType.TRACK, track.item_id, "SubGenre"
        )

        new_genre = await genre_ctrl.promote_alias_to_genre(parent.item_id, "SubGenre")
        assert new_genre.name == "SubGenre"
        assert int(new_genre.item_id) != int(parent.item_id)

        # Media mapping should have moved to new genre
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track' AND alias = 'SubGenre'",
            {"mid": int(track.item_id)},
            limit=1,
        )
        assert len(rows) == 1
        assert int(rows[0]["genre_id"]) == int(new_genre.item_id)

    async def test_promote_creates_self_alias(self, genre_ctrl: GenreController) -> None:
        """New genre has its own name as alias."""
        parent = await genre_ctrl.add_item_to_library(_make_genre("PromParent"))
        await genre_ctrl.add_alias(parent.item_id, "PromChild")

        new_genre = await genre_ctrl.promote_alias_to_genre(parent.item_id, "PromChild")
        assert new_genre.genre_aliases is not None
        assert "PromChild" in new_genre.genre_aliases

    async def test_promote_self_alias_raises(self, genre_ctrl: GenreController) -> None:
        """Raises ValueError for self-alias."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("PromSelf"))
        with pytest.raises(ValueError, match="Cannot promote self-alias"):
            await genre_ctrl.promote_alias_to_genre(genre.item_id, "PromSelf")

    async def test_promote_removes_alias_from_source(self, genre_ctrl: GenreController) -> None:
        """Alias is removed from source genre after promotion."""
        parent = await genre_ctrl.add_item_to_library(_make_genre("PromComplete"))
        await genre_ctrl.add_alias(parent.item_id, "PromAlias")

        await genre_ctrl.promote_alias_to_genre(parent.item_id, "PromAlias")
        updated_parent = await genre_ctrl.get_library_item(int(parent.item_id))
        assert updated_parent.genre_aliases is not None
        assert "PromAlias" not in updated_parent.genre_aliases
        assert "PromComplete" in updated_parent.genre_aliases


# ===================================================================
# Group F2: merge_genres (7 tests)
# ===================================================================


class TestMergeGenres:
    """Tests for merge_genres."""

    async def test_merge_transfers_aliases(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Aliases from source genres are added to the target."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeTarget"))
        source = await genre_ctrl.add_item_to_library(_make_genre("MergeSource"))
        await genre_ctrl.add_alias(source.item_id, "SourceAlias")

        result = await genre_ctrl.merge_genres([source.item_id], target.item_id)
        assert result.genre_aliases is not None
        assert "MergeTarget" in result.genre_aliases
        assert "MergeSource" in result.genre_aliases
        assert "SourceAlias" in result.genre_aliases

    async def test_merge_transfers_media_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Media mappings from source genres are moved to the target."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeMapTarget"))
        source = await genre_ctrl.add_item_to_library(_make_genre("MergeMapSource"))
        track = await _add_test_track(mass, "Merge Track")
        await genre_ctrl.add_media_mapping(
            source.item_id, MediaType.TRACK, track.item_id, "MergeMapSource"
        )

        await genre_ctrl.merge_genres([source.item_id], target.item_id)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = 'track' AND media_id = :mid",
            {"gid": int(target.item_id), "mid": int(track.item_id)},
            limit=1,
        )
        assert len(rows) == 1

    async def test_merge_deletes_source_genres(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Source genres are deleted after merge."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeDelTarget"))
        source = await genre_ctrl.add_item_to_library(_make_genre("MergeDelSource"))

        await genre_ctrl.merge_genres([source.item_id], target.item_id)
        with pytest.raises(MediaNotFoundError):
            await genre_ctrl.get_library_item(int(source.item_id))

    async def test_merge_deduplicates_aliases(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Overlapping aliases are not duplicated on the target."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeDedupTarget"))
        await genre_ctrl.add_alias(target.item_id, "SharedAlias")
        source = await genre_ctrl.add_item_to_library(_make_genre("MergeDedupSource"))
        await genre_ctrl.add_alias(source.item_id, "SharedAlias")

        result = await genre_ctrl.merge_genres([source.item_id], target.item_id)
        assert result.genre_aliases is not None
        alias_list = list(result.genre_aliases)
        norm_aliases = [a for a in alias_list if a.lower().replace(" ", "") == "sharedalias"]
        assert len(norm_aliases) == 1

    async def test_merge_deduplicates_media_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Overlapping media mappings do not create duplicates."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeDedupMapTarget"))
        source = await genre_ctrl.add_item_to_library(_make_genre("MergeDedupMapSource"))
        track = await _add_test_track(mass, "Merge Dedup Track")
        # Both genres map the same track
        await genre_ctrl.add_media_mapping(
            target.item_id, MediaType.TRACK, track.item_id, "MergeDedupMapTarget"
        )
        await genre_ctrl.add_media_mapping(
            source.item_id, MediaType.TRACK, track.item_id, "MergeDedupMapSource"
        )

        await genre_ctrl.merge_genres([source.item_id], target.item_id)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = 'track' AND media_id = :mid",
            {"gid": int(target.item_id), "mid": int(track.item_id)},
            limit=0,
        )
        assert len(rows) == 1

    async def test_merge_multiple_sources(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Multiple source genres can be merged at once."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeMultiTarget"))
        source1 = await genre_ctrl.add_item_to_library(_make_genre("MergeMultiSrc1"))
        source2 = await genre_ctrl.add_item_to_library(_make_genre("MergeMultiSrc2"))
        track1 = await _add_test_track(mass, "Multi Merge Track 1")
        track2 = await _add_test_track(mass, "Multi Merge Track 2")
        await genre_ctrl.add_media_mapping(
            source1.item_id, MediaType.TRACK, track1.item_id, "MergeMultiSrc1"
        )
        await genre_ctrl.add_media_mapping(
            source2.item_id, MediaType.TRACK, track2.item_id, "MergeMultiSrc2"
        )

        result = await genre_ctrl.merge_genres([source1.item_id, source2.item_id], target.item_id)
        assert result.genre_aliases is not None
        assert "MergeMultiSrc1" in result.genre_aliases
        assert "MergeMultiSrc2" in result.genre_aliases

        # Both tracks mapped to target
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_type = 'track'",
            {"gid": int(target.item_id)},
            limit=0,
        )
        assert len(rows) == 2

        # Both sources deleted
        for src in (source1, source2):
            with pytest.raises(MediaNotFoundError):
                await genre_ctrl.get_library_item(int(src.item_id))

    async def test_merge_target_in_source_raises(self, genre_ctrl: GenreController) -> None:
        """Raises ValueError when target is in the source list."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("MergeSelfTarget"))
        with pytest.raises(ValueError, match="Target genre cannot be in the list"):
            await genre_ctrl.merge_genres([genre.item_id], genre.item_id)

    async def test_merge_empty_source_raises(self, genre_ctrl: GenreController) -> None:
        """Raises ValueError when source list is empty."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeEmptyTarget"))
        with pytest.raises(ValueError, match="No genre IDs provided"):
            await genre_ctrl.merge_genres([], target.item_id)


# ===================================================================
# Group G: restore_default_genres (5 tests)
# ===================================================================


class TestRestoreDefaultGenres:
    """Tests for restore_default_genres."""

    async def test_restore_partial_on_empty(self, genre_ctrl: GenreController) -> None:
        """Creates genres from DEFAULT_GENRE_MAPPING with self-aliases."""
        created = await genre_ctrl.restore_default_genres(full_restore=False)
        assert len(created) > 0
        for genre in created[:3]:
            assert genre.genre_aliases is not None
            assert genre.name in genre.genre_aliases

    async def test_restore_partial_idempotent(self, genre_ctrl: GenreController) -> None:
        """Second call returns empty list (no duplicates)."""
        await genre_ctrl.restore_default_genres(full_restore=False)
        second = await genre_ctrl.restore_default_genres(full_restore=False)
        assert len(second) == 0

    async def test_restore_partial_adds_missing(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Pre-existing genres not duplicated, missing ones added."""
        first_default = DEFAULT_GENRE_MAPPING[0]["genre"]
        await genre_ctrl.add_item_to_library(_make_genre(first_default))
        before = await genre_ctrl.library_count()
        created = await genre_ctrl.restore_default_genres(full_restore=False)
        after = await genre_ctrl.library_count()
        assert len(created) == after - before

    async def test_restore_full_clears_all(self, genre_ctrl: GenreController) -> None:
        """Full restore: custom genres gone, only defaults remain."""
        await genre_ctrl.add_item_to_library(_make_genre("MyCustomGenre"))
        await genre_ctrl.restore_default_genres(full_restore=True)
        items = await genre_ctrl.library_items(limit=0, hide_empty=False)
        names = {g.name for g in items}
        assert "MyCustomGenre" not in names
        assert len(items) == len(DEFAULT_GENRE_MAPPING)

    async def test_restore_creates_configured_aliases(self, genre_ctrl: GenreController) -> None:
        """Genres have aliases from genre_mapping.json."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        entries_with_aliases = [e for e in DEFAULT_GENRE_MAPPING if e.get("aliases")]
        if not entries_with_aliases:
            pytest.skip("No default genres with aliases configured")
        entry = entries_with_aliases[0]
        items = await genre_ctrl.library_items(search=entry["genre"], hide_empty=False)
        assert len(items) > 0
        genre = items[0]
        assert genre.genre_aliases is not None
        # Self-alias should be present
        assert entry["genre"] in genre.genre_aliases
        # Configured aliases should be present
        for alias in entry["aliases"]:
            assert alias in genre.genre_aliases


# ===================================================================
# Group H: Query Methods (7 tests)
# ===================================================================


class TestQueryMethods:
    """Tests for radio_mode, mapped_media, and overview endpoints."""

    async def test_radio_mode_empty(self, genre_ctrl: GenreController) -> None:
        """No mapped tracks returns empty list."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("EmptyRadio"))
        tracks = await genre_ctrl.radio_mode_base_tracks(genre)
        assert tracks == []

    async def test_radio_mode_returns_tracks(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mapped tracks are returned."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("RadioGenre"))
        track = await _add_test_track(mass, "Radio Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "RadioGenre"
        )
        tracks = await genre_ctrl.radio_mode_base_tracks(genre)
        assert len(tracks) >= 1
        assert any(t.name == "Radio Track" for t in tracks)

    async def test_radio_mode_limit_50(self, genre_ctrl: GenreController) -> None:
        """At most 50 tracks returned (hardcoded limit in radio_mode_base_tracks)."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("RadioLimit"))
        tracks = await genre_ctrl.radio_mode_base_tracks(genre)
        assert len(tracks) <= 50

    async def test_mapped_media_returns_all_types(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns (tracks, albums, artists) tuple."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("MappedMedia"))
        result = await genre_ctrl.mapped_media(genre)
        assert isinstance(result, tuple)
        assert len(result) == 3
        tracks, albums, artists = result
        assert isinstance(tracks, list)
        assert isinstance(albums, list)
        assert isinstance(artists, list)

    async def test_mapped_media_empty(self, genre_ctrl: GenreController) -> None:
        """No mappings returns ([], [], [])."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("EmptyMapped"))
        tracks, albums, artists = await genre_ctrl.mapped_media(genre)
        assert tracks == []
        assert albums == []
        assert artists == []

    async def test_overview_returns_folders(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns RecommendationFolder items when mappings exist."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("OverviewGenre"))
        track = await _add_test_track(mass, "Overview Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "OverviewGenre"
        )
        folders = await genre_ctrl.get_overview(genre.item_id)
        assert len(folders) >= 1
        assert folders[0].name == "Tracks"

    async def test_overview_empty(self, genre_ctrl: GenreController) -> None:
        """No mappings returns empty list."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("EmptyOverview"))
        folders = await genre_ctrl.get_overview(genre.item_id)
        assert folders == []

    async def test_get_genres_for_media_item(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns genres mapped to a specific media item."""
        genre1 = await genre_ctrl.add_item_to_library(_make_genre("GenreForItem1"))
        genre2 = await genre_ctrl.add_item_to_library(_make_genre("GenreForItem2"))
        track = await _add_test_track(mass, "Track With Genres")
        await genre_ctrl.add_media_mapping(
            genre1.item_id, MediaType.TRACK, track.item_id, "GenreForItem1"
        )
        await genre_ctrl.add_media_mapping(
            genre2.item_id, MediaType.TRACK, track.item_id, "GenreForItem2"
        )
        genres = await genre_ctrl.get_genres_for_media_item(MediaType.TRACK, track.item_id)
        genre_names = {g.name for g in genres}
        assert "GenreForItem1" in genre_names
        assert "GenreForItem2" in genre_names

    async def test_get_genres_for_media_item_empty(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns empty list for unmapped media item."""
        track = await _add_test_track(mass, "Track Without Genres")
        genres = await genre_ctrl.get_genres_for_media_item(MediaType.TRACK, track.item_id)
        assert genres == []

    async def test_get_genres_for_media_item_non_integer_id(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns empty list for non-integer provider item IDs (e.g. Bandcamp compound IDs)."""
        genres = await genre_ctrl.get_genres_for_media_item(MediaType.ALBUM, "3957198221-190478553")
        assert genres == []

    async def test_library_items_hide_empty_true(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """hide_empty=True returns only genres with mappings."""
        mapped = await genre_ctrl.add_item_to_library(_make_genre("HasMappingGenre"))
        unmapped = await genre_ctrl.add_item_to_library(_make_genre("NoMappingGenre"))
        track = await _add_test_track(mass, "HasMapping Track")
        await genre_ctrl.add_media_mapping(
            mapped.item_id, MediaType.TRACK, track.item_id, "HasMappingGenre"
        )
        results = await genre_ctrl.library_items(hide_empty=True)
        result_ids = {int(g.item_id) for g in results}
        assert int(mapped.item_id) in result_ids
        assert int(unmapped.item_id) not in result_ids

    async def test_library_items_hide_empty_default(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Default (hide_empty=True) excludes unmapped genres."""
        mapped = await genre_ctrl.add_item_to_library(_make_genre("DefaultFilterMapped"))
        unmapped = await genre_ctrl.add_item_to_library(_make_genre("DefaultFilterUnmapped"))
        track = await _add_test_track(mass, "DefaultFilter Track")
        await genre_ctrl.add_media_mapping(
            mapped.item_id, MediaType.TRACK, track.item_id, "DefaultFilterMapped"
        )
        results = await genre_ctrl.library_items()
        result_ids = {int(g.item_id) for g in results}
        assert int(mapped.item_id) in result_ids
        assert int(unmapped.item_id) not in result_ids

    async def test_library_items_show_all(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """hide_empty=False returns all genres including unmapped."""
        mapped = await genre_ctrl.add_item_to_library(_make_genre("ShowAllMapped"))
        unmapped = await genre_ctrl.add_item_to_library(_make_genre("ShowAllUnmapped"))
        track = await _add_test_track(mass, "ShowAll Track")
        await genre_ctrl.add_media_mapping(
            mapped.item_id, MediaType.TRACK, track.item_id, "ShowAllMapped"
        )
        results = await genre_ctrl.library_items(hide_empty=False)
        result_ids = {int(g.item_id) for g in results}
        assert int(mapped.item_id) in result_ids
        assert int(unmapped.item_id) in result_ids


# ===================================================================
# Group I: Genre Lookup & Scanner (5 tests)
# ===================================================================


class TestGenreLookupAndScanner:
    """Tests for genre/alias lookup and scanner status."""

    async def test_find_genres_for_alias_existing(self, genre_ctrl: GenreController) -> None:
        """Finds existing genre by name."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Garage"))
        found = await genre_ctrl._find_genres_for_alias("Garage")
        assert isinstance(found, list)
        assert int(genre.item_id) in found

    async def test_find_genres_for_alias_by_alias(self, genre_ctrl: GenreController) -> None:
        """Finds existing genre by alias string in genre_aliases JSON."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Breakbeat"))
        await genre_ctrl.add_alias(genre.item_id, "Big Beat")
        found = await genre_ctrl._find_genres_for_alias("Big Beat")
        assert isinstance(found, list)
        assert int(genre.item_id) in found

    async def test_find_genres_for_alias_creates_new(self, genre_ctrl: GenreController) -> None:
        """Creates new genre when no match found."""
        found = await genre_ctrl._find_genres_for_alias("BrandNewGenre12345")
        assert isinstance(found, list)
        assert len(found) == 1
        genre = await genre_ctrl.get_library_item(found[0])
        assert genre.name == "BrandNewGenre12345"

    async def test_scanner_status(self, genre_ctrl: GenreController) -> None:
        """Returns dict with expected keys."""
        status = await genre_ctrl.get_scanner_status()
        assert "running" in status
        assert "last_scan_time" in status

    async def test_scan_mappings_trigger(self, genre_ctrl: GenreController) -> None:
        """Returns 'triggered' status."""
        result = await genre_ctrl.scan_mappings()
        assert result["status"] == "triggered"


# ===================================================================
# Group J: Base Class Integration (3 tests)
# ===================================================================


class TestBaseClassIntegration:
    """Tests for base class query patterns (genre_aliases column, pagination, favorites)."""

    async def test_genre_aliases_inline(self, genre_ctrl: GenreController) -> None:
        """genre_aliases column populates genre_aliases on fetched Genre."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("InlineTest"))
        await genre_ctrl.add_alias(genre.item_id, "Inline Alias")
        # Fetch via library_items (uses base_query)
        items = await genre_ctrl.library_items(search="InlineTest", hide_empty=False)
        assert len(items) >= 1
        fetched = items[0]
        assert fetched.genre_aliases is not None
        assert "InlineTest" in fetched.genre_aliases
        assert "Inline Alias" in fetched.genre_aliases

    async def test_pagination(self, genre_ctrl: GenreController) -> None:
        """limit/offset work correctly."""
        for i in range(5):
            await genre_ctrl.add_item_to_library(_make_genre(f"Page{i}"))
        page1 = await genre_ctrl.library_items(limit=2, offset=0, order_by="name", hide_empty=False)
        page2 = await genre_ctrl.library_items(limit=2, offset=2, order_by="name", hide_empty=False)
        assert len(page1) == 2
        assert len(page2) == 2
        ids1 = {g.item_id for g in page1}
        ids2 = {g.item_id for g in page2}
        assert ids1.isdisjoint(ids2)

    async def test_favorite_filter(self, genre_ctrl: GenreController) -> None:
        """favorite=True filters correctly."""
        await genre_ctrl.add_item_to_library(_make_genre("FavYes", favorite=True))
        await genre_ctrl.add_item_to_library(_make_genre("FavNo", favorite=False))
        favs = await genre_ctrl.library_items(favorite=True, hide_empty=False)
        assert all(g.favorite for g in favs)
        assert any(g.name == "FavYes" for g in favs)
