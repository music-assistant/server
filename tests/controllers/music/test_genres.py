"""
Integration tests for the GenreController (V3 schema).

Uses a database-only MusicAssistant instance with a real SQLite database in a
temporary directory.
"""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    Podcast,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from PIL import Image as PILImage

from music_assistant.constants import (
    CUSTOM_IMAGES_DIRNAME,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
    DEFAULT_AUDIOBOOK_GENRE_MAPPING,
    DEFAULT_GENRE_MAPPING,
    DEFAULT_PODCAST_GENRE_MAPPING,
)
from music_assistant.controllers.music.media.genres import GenreController
from music_assistant.mass import MusicAssistant

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class", name="mass")
def mass_fixture(music_mass_class: MusicAssistant) -> MusicAssistant:
    """Return the class-scoped database-only Music Assistant fixture."""
    return music_mass_class


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


def _library_provider_mapping() -> set[ProviderMapping]:
    """Create a provider mapping set with in_library=True and a unique provider_item_id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


async def _add_test_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal artist to the library."""
    artist = Artist(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_provider_mapping(),
    )
    return await mass.music.artists.add_item_to_library(artist)


async def _add_test_track(mass: MusicAssistant, name: str) -> Track:
    """Add a minimal track to the library (creates an artist first)."""
    artist = await _add_test_artist(mass, f"Artist for {name}")
    track = Track(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_provider_mapping(),
        artists=UniqueList([artist]),
    )
    return await mass.music.tracks.add_item_to_library(track)


async def _add_test_podcast(mass: MusicAssistant, name: str) -> Podcast:
    """Add a minimal podcast to the library."""
    podcast = Podcast(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_provider_mapping(),
    )
    return await mass.music.podcasts.add_item_to_library(podcast)


async def _set_podcast_genres(mass: MusicAssistant, podcast_id: int, genres: list[str]) -> None:
    """Set metadata.genres on a podcast row directly in the DB."""
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_PODCASTS} "
        "SET metadata = json_set(metadata, '$.genres', json(:genres)) "
        "WHERE item_id = :id",
        {"genres": json.dumps(genres), "id": podcast_id},
    )
    await mass.music.database.commit()


async def _add_test_album(mass: MusicAssistant, name: str) -> Album:
    """Add a minimal album to the library."""
    album = Album(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_provider_mapping(),
        album_type=AlbumType.ALBUM,
    )
    return await mass.music.albums.add_item_to_library(album)


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

    async def test_content_type_defaults_to_none(self, genre_ctrl: GenreController) -> None:
        """A genre added without a content_type round-trips as None (music/general)."""
        created = await genre_ctrl.add_item_to_library(_make_genre("Soul"))
        fetched = await genre_ctrl.get_library_item(int(created.item_id))
        assert fetched.content_type is None

    async def test_content_type_persists_and_round_trips(self, genre_ctrl: GenreController) -> None:
        """A genre's content_type is persisted to the DB column and read back as the enum."""
        genre = Genre(
            item_id="0",
            provider="library",
            name="True Crime",
            provider_mappings=set(),
            content_type=MediaType.PODCAST,
        )
        created = await genre_ctrl.add_item_to_library(genre)
        fetched = await genre_ctrl.get_library_item(int(created.item_id))
        assert fetched.content_type is MediaType.PODCAST

    async def test_content_type_immutable_on_overwrite_update(
        self, genre_ctrl: GenreController
    ) -> None:
        """The taxonomy is set at creation and is not changed by an update, even with overwrite."""
        genre = Genre(
            item_id="0",
            provider="library",
            name="Documentary",
            provider_mappings=set(),
            content_type=MediaType.PODCAST,
        )
        created = await genre_ctrl.add_item_to_library(genre)
        update = Genre(
            item_id="0",
            provider="library",
            name="Documentary",
            provider_mappings=set(),
            content_type=MediaType.AUDIOBOOK,
        )
        updated = await genre_ctrl.update_item_in_library(created.item_id, update, overwrite=True)
        assert updated.content_type is MediaType.PODCAST
        fetched = await genre_ctrl.get_library_item(int(created.item_id))
        assert fetched.content_type is MediaType.PODCAST

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
        """Search 'country' returns Country genre but not unrelated ones like Metal."""
        await genre_ctrl.add_item_to_library(_make_genre("Metal"))
        items = await genre_ctrl.library_items(search="country", hide_empty=False)
        names = {g.name for g in items}
        assert "country" in names
        assert "Metal" not in names

    async def test_library_items_hide_empty_true(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """hide_empty=True returns only genres with media mappings."""
        mapped = await genre_ctrl.add_item_to_library(_make_genre("HideEmptyMapped"))
        await genre_ctrl.add_item_to_library(_make_genre("HideEmptyUnmapped"))
        track = await _add_test_track(mass, "HideEmpty Track")
        await genre_ctrl.add_media_mapping(
            int(mapped.item_id), MediaType.TRACK, track.item_id, "HideEmptyMapped"
        )
        items = await genre_ctrl.library_items(hide_empty=True)
        names = {g.name for g in items}
        assert "HideEmptyMapped" in names
        assert "HideEmptyUnmapped" not in names

    async def test_library_items_hide_empty_false(self, genre_ctrl: GenreController) -> None:
        """hide_empty=False returns all genres regardless of mappings."""
        await genre_ctrl.add_item_to_library(_make_genre("HideEmptyFalseGenre"))
        items = await genre_ctrl.library_items(hide_empty=False)
        names = {g.name for g in items}
        assert "HideEmptyFalseGenre" in names

    async def test_library_items_hide_empty_none_returns_default_genres(
        self, genre_ctrl: GenreController
    ) -> None:
        """
        hide_empty=None (default) returns only default genres (translation_key IS NOT NULL).

        Default genres are seeded via restore_default_genres (translation_key IS NOT NULL).
        Non-default genres created via _find_genres_for_alias mirror the library scan path
        and store translation_key=NULL in the DB.
        """
        await genre_ctrl.restore_default_genres()
        scanned_name = "ScannedNonDefaultGenreXyz"
        await genre_ctrl._find_genres_for_alias(scanned_name, None)

        default_genre_name = DEFAULT_GENRE_MAPPING[0]["genre"]
        items = await genre_ctrl.library_items(hide_empty=None)
        names = {g.name for g in items}
        assert default_genre_name in names
        assert scanned_name not in names

    async def test_library_items_default_is_hide_empty_none(
        self, genre_ctrl: GenreController
    ) -> None:
        """Calling library_items() with no hide_empty arg behaves like hide_empty=None."""
        await genre_ctrl.restore_default_genres()
        await genre_ctrl._find_genres_for_alias("DefaultArgScannedGenreXyz", None)
        default_genre_name = DEFAULT_GENRE_MAPPING[0]["genre"]
        items_default = await genre_ctrl.library_items()
        items_none = await genre_ctrl.library_items(hide_empty=None)
        assert {g.item_id for g in items_default} == {g.item_id for g in items_none}
        names = {g.name for g in items_default}
        assert default_genre_name in names
        assert "DefaultArgScannedGenreXyz" not in names

    async def test_library_items_rejects_genre_param(self, genre_ctrl: GenreController) -> None:
        """library_items(genre=1) raises ValueError."""
        with pytest.raises(ValueError, match="genre parameter is not supported"):
            await genre_ctrl.library_items(genre=1)

    async def test_library_items_media_type_filter(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        media_type filter returns all non-empty genres for that type, including non-defaults.

        Verifies:
        - Non-default genres (no translation_key) with mappings ARE returned — the default
          translation_key IS NOT NULL filter is bypassed when media_type is set.
        - Genres mapped only to another type are excluded.
        - Default genres (with translation_key) that have no mapping for the type are excluded.
        - A genre mapped to multiple types appears in results for each of those types.
        - search composing with media_type works correctly.
        - No mappings for the requested type returns an empty list.
        """
        track_genre = await genre_ctrl.add_item_to_library(_make_genre("MT_FilterTrackOnlyGenre"))
        album_genre = await genre_ctrl.add_item_to_library(_make_genre("MT_FilterAlbumOnlyGenre"))
        shared_genre = await genre_ctrl.add_item_to_library(_make_genre("MT_FilterSharedGenre"))

        track = await _add_test_track(mass, "MT Filter Track")
        album = await _add_test_album(mass, "MT Filter Album")
        await genre_ctrl.add_media_mapping(
            int(track_genre.item_id), MediaType.TRACK, track.item_id, "MT_FilterTrackOnlyGenre"
        )
        await genre_ctrl.add_media_mapping(
            int(album_genre.item_id), MediaType.ALBUM, album.item_id, "MT_FilterAlbumOnlyGenre"
        )
        # shared_genre is mapped to both tracks and albums
        await genre_ctrl.add_media_mapping(
            int(shared_genre.item_id), MediaType.TRACK, track.item_id, "MT_FilterSharedGenre"
        )
        await genre_ctrl.add_media_mapping(
            int(shared_genre.item_id), MediaType.ALBUM, album.item_id, "MT_FilterSharedGenre"
        )

        # Add a default genre (has translation_key) that has NO mappings for any type —
        # it must not appear in media_type results even though hide_empty=None would normally
        # include all defaults.
        await genre_ctrl.restore_default_genres()

        track_results = await genre_ctrl.library_items(media_type=MediaType.TRACK)
        track_names = {g.name for g in track_results}
        assert "MT_FilterTrackOnlyGenre" in track_names, (
            "track-mapped genre missing from TRACK results"
        )
        assert "MT_FilterSharedGenre" in track_names, "shared genre missing from TRACK results"
        assert "MT_FilterAlbumOnlyGenre" not in track_names, (
            "album-only genre appeared in TRACK results"
        )
        # Unmapped default genres must not bleed through —
        # media_type overrides the translation_key filter
        default_genre_name = DEFAULT_GENRE_MAPPING[0]["genre"]
        assert default_genre_name not in track_names, (
            "unmapped default genre appeared in TRACK results"
        )

        album_results = await genre_ctrl.library_items(media_type=MediaType.ALBUM)
        album_names = {g.name for g in album_results}
        assert "MT_FilterAlbumOnlyGenre" in album_names, (
            "album-mapped genre missing from ALBUM results"
        )
        assert "MT_FilterSharedGenre" in album_names, "shared genre missing from ALBUM results"
        assert "MT_FilterTrackOnlyGenre" not in album_names, (
            "track-only genre appeared in ALBUM results"
        )

        # search composes correctly with media_type
        search_results = await genre_ctrl.library_items(
            media_type=MediaType.TRACK, search="MT_FilterShared"
        )
        search_names = {g.name for g in search_results}
        assert "MT_FilterSharedGenre" in search_names
        assert "MT_FilterTrackOnlyGenre" not in search_names

        # No mappings for the requested type returns an empty list
        playlist_results = await genre_ctrl.library_items(media_type=MediaType.PLAYLIST)
        playlist_names = {g.name for g in playlist_results}
        assert "MT_FilterTrackOnlyGenre" not in playlist_names
        assert "MT_FilterAlbumOnlyGenre" not in playlist_names
        assert "MT_FilterSharedGenre" not in playlist_names

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

    async def test_add_media_mapping_sets_is_manual(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mappings created via add_media_mapping have is_manual = 1."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("IsManualPop"))
        track = await _add_test_track(mass, "IsManualPop Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "IsManualPop"
        )
        row = await mass.music.database.get_row(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre.item_id),
                "media_id": int(track.item_id),
                "media_type": MediaType.TRACK.value,
            },
        )
        assert row is not None
        assert row["is_manual"] == 1

    async def test_add_media_mapping_upgrades_existing_auto_row(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Calling add_media_mapping on a pre-existing non-manual row upgrades is_manual to 1."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("UpgradeGenre"))
        track = await _add_test_track(mass, "Upgrade Track")
        # insert row directly without is_manual (simulates scanner-created row)
        await mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre.item_id),
                "media_id": int(track.item_id),
                "media_type": MediaType.TRACK.value,
                "alias": "UpgradeGenre",
            },
        )
        row_before = await mass.music.database.get_row(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre.item_id),
                "media_id": int(track.item_id),
                "media_type": MediaType.TRACK.value,
            },
        )
        assert row_before is not None
        assert row_before["is_manual"] == 0

        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "UpgradeGenre"
        )

        row_after = await mass.music.database.get_row(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre.item_id),
                "media_id": int(track.item_id),
                "media_type": MediaType.TRACK.value,
            },
        )
        assert row_after is not None
        assert row_after["is_manual"] == 1

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
        unique_genre = "SzTestSyncGenreXYZ"
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {unique_genre})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRES} WHERE name = :name",
            {"name": unique_genre},
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

    async def test_sync_picks_up_genre_created_between_syncs(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A genre created between syncs re-routes an already-stored alias mapping."""
        electro = await genre_ctrl.add_item_to_library(_make_genre("SyncElectro"))
        await genre_ctrl.add_alias(electro.item_id, "SyncWaveAlias")
        track = await _add_test_track(mass, "Sync Track NewGenre")
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncWaveAlias"})
        # user now creates a genre whose primary name matches the stored alias;
        # primary-name resolution takes priority, so a re-sync must remap the item
        new_genre = await genre_ctrl.add_item_to_library(_make_genre("SyncWaveAlias"))
        genre_ctrl._sync_lookup_cache.clear()  # simulate the cached lookup expiring
        await genre_ctrl.sync_media_item_genres(MediaType.TRACK, track.item_id, {"SyncWaveAlias"})
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        assert {int(r["genre_id"]) for r in rows} == {int(new_genre.item_id)}


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

    async def test_promote_alias_shared_across_genres(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        Promotion handles aliases shared across multiple genres.

        Every genre that claimed the alias loses it and all mappings made via
        the alias are moved to the new genre, regardless of which source the
        caller invoked the promotion from.
        """
        folk = await genre_ctrl.add_item_to_library(_make_genre("PromFolk"))
        pop = await genre_ctrl.add_item_to_library(_make_genre("PromPop"))
        await genre_ctrl.add_alias(folk.item_id, "PromManele")
        await genre_ctrl.add_alias(pop.item_id, "PromManele")
        track = await _add_test_track(mass, "Manele Track")
        # Track is mapped to both genres via the same alias (n:n).
        await genre_ctrl.add_media_mapping(
            folk.item_id, MediaType.TRACK, track.item_id, "PromManele"
        )
        await genre_ctrl.add_media_mapping(
            pop.item_id, MediaType.TRACK, track.item_id, "PromManele"
        )

        # Invoke from one of the owning genres; both should be cleared.
        new_genre = await genre_ctrl.promote_alias_to_genre(folk.item_id, "PromManele")

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'track'",
            {"mid": int(track.item_id)},
            limit=0,
        )
        genre_ids = {int(r["genre_id"]) for r in rows}
        assert genre_ids == {int(new_genre.item_id)}

        updated_folk = await genre_ctrl.get_library_item(int(folk.item_id))
        updated_pop = await genre_ctrl.get_library_item(int(pop.item_id))
        assert "PromManele" not in (updated_folk.genre_aliases or [])
        assert "PromManele" not in (updated_pop.genre_aliases or [])

    async def test_promote_rebuilds_derived_album_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        Derived album rows are cleared from the source genre after promotion.

        Without this, propagation-derived (alias=NULL, is_derived=1) rows would
        still link the album to the source genre even though the underlying
        tracks have moved to the new one.
        """
        parent = await genre_ctrl.add_item_to_library(_make_genre("PromHipHop"))
        await genre_ctrl.add_alias(parent.item_id, "PromRap")
        track = await _add_test_track(mass, "Rap Track")
        album = await _add_test_album(mass, "Rap Album")
        await genre_ctrl.add_media_mapping(
            parent.item_id, MediaType.TRACK, track.item_id, "PromRap"
        )
        # Seed a propagation-derived album row (alias=NULL, is_derived=1) as if
        # it had been written by _propagate_genre_mappings_to_parents.
        await mass.music.database.execute(
            f"INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "(genre_id, media_id, media_type, alias, is_derived) "
            "VALUES (:gid, :mid, 'album', NULL, 1)",
            {"gid": int(parent.item_id), "mid": int(album.item_id)},
        )

        await genre_ctrl.promote_alias_to_genre(parent.item_id, "PromRap")

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_id = :mid AND media_type = 'album'",
            {"mid": int(album.item_id)},
            limit=0,
        )
        assert all(int(r["genre_id"]) != int(parent.item_id) for r in rows)

    async def test_promote_alias_inherits_content_type(self, genre_ctrl: GenreController) -> None:
        """A genre promoted from an alias stays in its source's taxonomy."""
        parent = await genre_ctrl.add_item_to_library(
            Genre(
                item_id="0",
                provider="library",
                name="PromTrueCrime",
                provider_mappings=set(),
                content_type=MediaType.PODCAST,
            )
        )
        await genre_ctrl.add_alias(parent.item_id, "PromSerialKillers")

        new_genre = await genre_ctrl.promote_alias_to_genre(parent.item_id, "PromSerialKillers")
        assert new_genre.content_type is MediaType.PODCAST
        fetched = await genre_ctrl.get_library_item(int(new_genre.item_id))
        assert fetched.content_type is MediaType.PODCAST


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

    async def test_merge_cross_taxonomy_raises(self, genre_ctrl: GenreController) -> None:
        """Raises ValueError when source and target belong to different taxonomies."""
        target = await genre_ctrl.add_item_to_library(_make_genre("MergeMusicTarget"))
        source = await genre_ctrl.add_item_to_library(
            Genre(
                item_id="0",
                provider="library",
                name="MergePodcastSource",
                provider_mappings=set(),
                content_type=MediaType.PODCAST,
            )
        )
        with pytest.raises(ValueError, match="same taxonomy"):
            await genre_ctrl.merge_genres([source.item_id], target.item_id)
        # the source genre must survive a rejected merge
        assert await genre_ctrl.get_library_item(int(source.item_id)) is not None


# ===================================================================
# Group G: restore_default_genres (5 tests)
# ===================================================================


class TestRestoreDefaultGenres:
    """Tests for restore_default_genres."""

    async def test_restore_partial_on_empty(self, genre_ctrl: GenreController) -> None:
        """Partial restore on pre-seeded DB returns empty (nothing to add)."""
        # Genres are already seeded during startup (_setup_database), so a partial
        # restore is idempotent and returns no new genres.
        created = await genre_ctrl.restore_default_genres(full_restore=False)
        assert len(created) == 0
        # Verify the default genres are actually present
        count = await genre_ctrl.library_count()
        assert count >= len(DEFAULT_GENRE_MAPPING)

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
        # full restore seeds every taxonomy (music + podcast + audiobook)
        assert len(items) == (
            len(DEFAULT_GENRE_MAPPING)
            + len(DEFAULT_PODCAST_GENRE_MAPPING)
            + len(DEFAULT_AUDIOBOOK_GENRE_MAPPING)
        )

    async def test_restore_creates_configured_aliases(self, genre_ctrl: GenreController) -> None:
        """Genres have aliases from genre_mapping.json."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        entries_with_aliases = [e for e in DEFAULT_GENRE_MAPPING if e.get("aliases")]
        if not entries_with_aliases:
            pytest.skip("No default genres with aliases configured")
        entry = entries_with_aliases[0]
        items = await genre_ctrl.library_items(
            search=entry["genre"], hide_empty=False, summary=False
        )
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
    """Tests for the tracks, albums, mapped_media, and overview endpoints."""

    async def test_genre_tracks_empty(self, genre_ctrl: GenreController) -> None:
        """A genre with no mapped tracks returns an empty list."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("EmptyGenre"))
        assert await genre_ctrl.tracks(genre.item_id) == []

    async def test_genre_tracks_returns_mapped(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Tracks mapped to a genre are returned."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("TracksGenre"))
        track = await _add_test_track(mass, "Genre Track")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.TRACK, track.item_id, "TracksGenre"
        )
        tracks = await genre_ctrl.tracks(genre.item_id)
        assert any(t.name == "Genre Track" for t in tracks)

    async def test_genre_tracks_respects_limit(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """The limit parameter caps the number of tracks returned."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("LimitGenre"))
        for i in range(3):
            track = await _add_test_track(mass, f"Limit Track {i}")
            await genre_ctrl.add_media_mapping(
                genre.item_id, MediaType.TRACK, track.item_id, "LimitGenre"
            )
        assert len(await genre_ctrl.tracks(genre.item_id, limit=2)) == 2

    async def test_genre_albums_returns_mapped(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Albums mapped to a genre are returned."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("AlbumsGenre"))
        album = await _add_test_album(mass, "Genre Album")
        await genre_ctrl.add_media_mapping(
            genre.item_id, MediaType.ALBUM, album.item_id, "AlbumsGenre"
        )
        albums = await genre_ctrl.albums(genre.item_id)
        assert any(a.name == "Genre Album" for a in albums)

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

    async def test_library_items_hide_empty_default(self, genre_ctrl: GenreController) -> None:
        """Default (hide_empty=None) returns only default genres (translation_key IS NOT NULL)."""
        await genre_ctrl.restore_default_genres()
        scanned = await genre_ctrl._find_genres_for_alias("DefaultFilterScannedXyz", None)
        assert scanned

        default_genre_name = DEFAULT_GENRE_MAPPING[0]["genre"]
        results = await genre_ctrl.library_items()
        names = {g.name for g in results}
        assert default_genre_name in names
        assert "DefaultFilterScannedXyz" not in names

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
        found = await genre_ctrl._find_genres_for_alias("Garage", None)
        assert isinstance(found, list)
        assert int(genre.item_id) in found

    async def test_find_genres_for_alias_by_alias(self, genre_ctrl: GenreController) -> None:
        """Finds existing genre by alias string in genre_aliases JSON."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Breakbeat"))
        await genre_ctrl.add_alias(genre.item_id, "Big Beat")
        found = await genre_ctrl._find_genres_for_alias("Big Beat", None)
        assert isinstance(found, list)
        assert int(genre.item_id) in found

    async def test_find_genres_for_alias_primary_name_takes_priority(
        self, genre_ctrl: GenreController
    ) -> None:
        """
        Primary name match returns only that genre, ignoring secondary alias matches.

        Regression test: a bare "pop" tag must not fan out to Rock/Punk/etc. that
        accumulated "pop" as a side-effect alias, when a dedicated Pop genre exists.
        """
        # Use the pre-seeded Pop and Rock genres (seeded during startup).
        pop_items = await genre_ctrl.library_items(search="Pop", hide_empty=False)
        rock_items = await genre_ctrl.library_items(search="Rock", hide_empty=False)
        pop_genre = next(g for g in pop_items if g.name == "pop")
        rock_genre = next(g for g in rock_items if g.name == "rock")
        # Simulate "pop" being written as a secondary alias on Rock (the bug scenario)
        await genre_ctrl.add_alias(rock_genre.item_id, "pop")

        found = await genre_ctrl._find_genres_for_alias("Pop", None)
        assert found == [int(pop_genre.item_id)]

    async def test_find_genres_for_alias_creates_new(self, genre_ctrl: GenreController) -> None:
        """Creates new genre when no match found."""
        found = await genre_ctrl._find_genres_for_alias("BrandNewGenre12345", None)
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
        # Fetch via library_items (uses base_query); request full items so genre_aliases hydrates
        items = await genre_ctrl.library_items(search="InlineTest", hide_empty=False, summary=False)
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


# ===================================================================
# Group K: _cleanup_stale_genre_mappings (7 tests)
# ===================================================================


async def _set_track_genres(mass: MusicAssistant, track_id: int, genres: list[str]) -> None:
    """Set metadata.genres on a track row directly in the DB."""
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_TRACKS} "
        "SET metadata = json_set(metadata, '$.genres', json(:genres)) "
        "WHERE item_id = :id",
        {"genres": json.dumps(genres), "id": track_id},
    )
    await mass.music.database.commit()


async def _set_album_genres(mass: MusicAssistant, album_id: int, genres: list[str]) -> None:
    """Set metadata.genres on an album row directly in the DB."""
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_ALBUMS} "
        "SET metadata = json_set(metadata, '$.genres', json(:genres)) "
        "WHERE item_id = :id",
        {"genres": json.dumps(genres), "id": album_id},
    )
    await mass.music.database.commit()


class TestCleanupStaleMappings:
    """Tests for _cleanup_stale_genre_mappings."""

    async def test_stale_mapping_removed_on_call(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A stale scanner mapping is removed when cleanup is called."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CsCountGenre"))
        track = await _add_test_track(mass, "CsCount Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)
        # insert a non-manual (scanner-style) row; track has no metadata.genres
        await mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": genre_id,
                "media_id": track_id,
                "media_type": MediaType.TRACK.value,
                "alias": "CsCountGenre",
            },
        )
        await genre_ctrl._cleanup_stale_genre_mappings()
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_empty_metadata_genres_removes_mapping(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Scanner mapping is removed when the track has no genres in metadata."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CsStale1Genre"))
        track = await _add_test_track(mass, "CsStale1 Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)
        # insert a non-manual (scanner-style) row
        await mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": genre_id,
                "media_id": track_id,
                "media_type": MediaType.TRACK.value,
                "alias": "CsStale1Genre",
            },
        )

        await genre_ctrl._cleanup_stale_genre_mappings()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_live_alias_mapping_preserved(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mapping is kept when the alias is still present in track metadata.genres."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CsLive1Genre"))
        track = await _add_test_track(mass, "CsLive1 Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)
        await _set_track_genres(mass, track_id, ["CsLive1Genre"])
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track.item_id, "CsLive1Genre")

        await genre_ctrl._cleanup_stale_genre_mappings()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(rows) == 1

    async def test_orphaned_mapping_removed(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Mapping is removed when the media item no longer exists in the DB."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CsOrphan1Genre"))
        track = await _add_test_track(mass, "CsOrphan1 Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)
        await _set_track_genres(mass, track_id, ["CsOrphan1Genre"])
        await genre_ctrl.add_media_mapping(
            genre_id, MediaType.TRACK, track.item_id, "CsOrphan1Genre"
        )

        # Delete the track directly, leaving behind an orphaned mapping row
        await mass.music.database.execute(
            f"DELETE FROM {DB_TABLE_TRACKS} WHERE item_id = :id", {"id": track_id}
        )
        await mass.music.database.commit()

        await genre_ctrl._cleanup_stale_genre_mappings()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_empty_nondefault_genre_deleted(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Non-default genre (is_default = 0) with no mappings is deleted."""
        # _find_genres_for_alias creates genres with is_default = 0
        found = await genre_ctrl._find_genres_for_alias("CsNonDefault1XYZ99", None)
        assert len(found) == 1
        genre_id = found[0]
        row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row is not None
        assert row["translation_key"] is None

        await genre_ctrl._cleanup_stale_genre_mappings()

        row_after = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row_after is None

    async def test_default_genre_without_mappings_preserved(
        self, genre_ctrl: GenreController, mass: MusicAssistant
    ) -> None:
        """Default genre (translation_key IS NOT NULL) is never deleted by cleanup."""
        await genre_ctrl.restore_default_genres(full_restore=False)
        default_entry = next(e for e in DEFAULT_GENRE_MAPPING if e.get("translation_key"))
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_GENRES} WHERE translation_key = :tk",
            {"tk": default_entry["translation_key"]},
            limit=1,
        )
        assert len(rows) == 1
        genre_id = int(rows[0]["item_id"])
        # Confirm no active mappings for this genre
        mapping_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} WHERE genre_id = :gid",
            {"gid": genre_id},
            limit=0,
        )
        if mapping_rows:
            pytest.skip("Default genre already has mappings")

        await genre_ctrl._cleanup_stale_genre_mappings()

        row_after = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row_after is not None

    async def test_nondefault_genre_with_active_mappings_preserved(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Non-default genre with at least one active mapping is NOT deleted."""
        track = await _add_test_track(mass, "CsKeep Track")
        track_id = int(track.item_id)
        await _set_track_genres(mass, track_id, ["CsKeepGenreXYZ"])
        found = await genre_ctrl._find_genres_for_alias("CsKeepGenreXYZ", None)
        genre_id = found[0]
        await genre_ctrl.add_media_mapping(
            genre_id, MediaType.TRACK, track.item_id, "CsKeepGenreXYZ"
        )

        await genre_ctrl._cleanup_stale_genre_mappings()

        row_after = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row_after is not None

    async def test_manual_mapping_preserved_when_alias_not_in_metadata(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        Manually-added mappings survive cleanup when alias is not in metadata.

        Regression for music-assistant/support#5310: a user creates a custom
        ("music type") genre via the UI and links an album to it. The album's
        metadata.genres reflects only the source-file tags and never contains
        the custom genre name, so the previous cleanup query wiped the mapping
        — and, transitively, the empty-non-default genre row.
        """
        genre = await genre_ctrl.add_item_to_library(_make_genre("MyCustomTypeXYZ"))
        genre_id = int(genre.item_id)
        album = await _add_test_album(mass, "Custom Type Album XYZ")
        album_id = int(album.item_id)
        # album source tags are unrelated to the custom genre
        await _set_album_genres(mass, album_id, ["Rock"])
        # user links album -> custom genre (no explicit alias, matches UI flow)
        await genre_ctrl.add_media_mapping(genre_id, MediaType.ALBUM, album_id)

        await genre_ctrl._cleanup_stale_genre_mappings()

        mapping_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album'",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(mapping_rows) == 1, "manual mapping was deleted by cleanup"
        genre_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert genre_row is not None, "custom genre was deleted after its mapping disappeared"

    async def test_playlog_entries_cleaned_for_deleted_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Playlog entries for a deleted empty non-default genre are removed."""
        found = await genre_ctrl._find_genres_for_alias("CsPlaylog1XYZ99", None)
        genre_id = found[0]
        # Insert a fake playlog entry for this genre
        cols = (
            "(item_id, provider, media_type, name, fully_played, seconds_played, timestamp, userid)"
        )
        await mass.music.database.execute(
            f"INSERT OR IGNORE INTO {DB_TABLE_PLAYLOG} {cols} "
            "VALUES (:item_id, 'library', :media_type, 'CsPlaylog1XYZ99', 0, 0, 0, 'testuser')",
            {"item_id": str(genre_id), "media_type": "genre"},
        )
        await mass.music.database.commit()

        await genre_ctrl._cleanup_stale_genre_mappings()

        # Both the genre and its playlog entry should be gone
        genre_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert genre_row is None
        playlog_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_PLAYLOG} WHERE media_type = 'genre' AND item_id = :id",
            {"id": str(genre_id)},
            limit=0,
        )
        assert len(playlog_rows) == 0


# ===================================================================
# Group L: Genre Exclusion (5 tests)
# ===================================================================


class TestGenreExclusion:
    """Tests for exclude_genre_from_media_item and remove_genre_exclusion."""

    async def test_exclude_inserts_row(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """exclude_genre_from_media_item inserts a row into the exclusion table."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclGenre1"))
        track = await _add_test_track(mass, "ExclTrack1")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = :mt",
            {"gid": genre_id, "mid": track_id, "mt": "track"},
            limit=0,
        )
        assert len(rows) == 1

    async def test_exclude_removes_existing_mapping(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """exclude_genre_from_media_item immediately deletes any existing mapping."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclGenre2"))
        track = await _add_test_track(mass, "ExclTrack2")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "ExclGenre2")
        pre_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(pre_rows) == 1

        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)

        post_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(post_rows) == 0

    async def test_exclude_idempotent(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Calling exclude_genre_from_media_item twice is idempotent."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclGenre3"))
        track = await _add_test_track(mass, "ExclTrack3")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)
        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = :mt",
            {"gid": genre_id, "mid": track_id, "mt": "track"},
            limit=0,
        )
        assert len(rows) == 1

    async def test_remove_exclusion_deletes_row(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """remove_genre_exclusion deletes the exclusion row."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclGenre4"))
        track = await _add_test_track(mass, "ExclTrack4")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)
        await genre_ctrl.remove_genre_exclusion(genre_id, MediaType.TRACK, track_id)

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_scanner_respects_exclusion(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """_bulk_scan_unmapped_genres does not create a mapping for an excluded pair."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclScanGenre"))
        track = await _add_test_track(mass, "ExclScan Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await _set_track_genres(mass, track_id, ["ExclScanGenre"])
        await genre_ctrl.add_alias(genre_id, "ExclScanGenre")
        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)
        await genre_ctrl._bulk_scan_unmapped_genres()

        mapping_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(mapping_rows) == 0

    async def test_full_scanner_respects_exclusion(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """_bulk_scan_media_genres does not create a mapping for an excluded pair."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("ExclFullScanGenre"))
        track = await _add_test_track(mass, "ExclFullScan Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        await _set_track_genres(mass, track_id, ["ExclFullScanGenre"])
        await genre_ctrl.add_alias(genre_id, "ExclFullScanGenre")
        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)
        await genre_ctrl._bulk_scan_media_genres()

        mapping_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid",
            {"gid": genre_id, "mid": track_id},
            limit=0,
        )
        assert len(mapping_rows) == 0

    async def test_cleanup_preserves_genre_with_exclusion(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        _cleanup_stale_genre_mappings keeps a genre that has an exclusion but no mappings.

        Verifies both that the genre row survives and that a playlog entry for it is
        also preserved (the playlog DELETE uses the same exclusion guard).
        """
        genre = await genre_ctrl.add_item_to_library(_make_genre("CleanupPreservedGenre"))
        track = await _add_test_track(mass, "CleanupPreserved Track")
        genre_id = int(genre.item_id)
        track_id = int(track.item_id)

        # Exclude the genre from the track (also removes any mapping that might exist)
        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.TRACK, track_id)

        # Insert a playlog entry for the genre so we can confirm it is also kept
        cols = (
            "(item_id, provider, media_type, name, fully_played, seconds_played, timestamp, userid)"
        )
        await mass.music.database.execute(
            f"INSERT OR IGNORE INTO {DB_TABLE_PLAYLOG} {cols} "
            "VALUES (:item_id, 'library', 'genre', :name, 0, 0, 0, 'testuser')",
            {"item_id": str(genre_id), "name": "CleanupPreservedGenre"},
        )
        await mass.music.database.commit()

        await genre_ctrl._cleanup_stale_genre_mappings()

        genre_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert genre_row is not None, "genre with an exclusion must not be deleted by cleanup"

        playlog_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_PLAYLOG} WHERE media_type = 'genre' AND item_id = :id",
            {"id": str(genre_id)},
            limit=0,
        )
        assert len(playlog_rows) == 1, "playlog entry for an excluded genre must not be deleted"

    async def test_get_genre_exclusions_for_media_item(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns genres excluded from a specific media item."""
        genre1 = await genre_ctrl.add_item_to_library(_make_genre("ExclQueryGenre1"))
        genre2 = await genre_ctrl.add_item_to_library(_make_genre("ExclQueryGenre2"))
        track = await _add_test_track(mass, "ExclQuery Track")
        await genre_ctrl.exclude_genre_from_media_item(
            genre1.item_id, MediaType.TRACK, track.item_id
        )
        await genre_ctrl.exclude_genre_from_media_item(
            genre2.item_id, MediaType.TRACK, track.item_id
        )
        result = await genre_ctrl.get_genre_exclusions_for_media_item(
            MediaType.TRACK, track.item_id
        )
        names = {g.name for g in result}
        assert "ExclQueryGenre1" in names
        assert "ExclQueryGenre2" in names

    async def test_get_genre_exclusions_for_media_item_empty(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns empty list when no genres are excluded from the media item."""
        track = await _add_test_track(mass, "ExclQuery No Exclusions Track")
        result = await genre_ctrl.get_genre_exclusions_for_media_item(
            MediaType.TRACK, track.item_id
        )
        assert result == []

    async def test_get_genre_exclusions_for_media_item_non_integer_id(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Returns empty list for non-integer media IDs."""
        result = await genre_ctrl.get_genre_exclusions_for_media_item(
            MediaType.ALBUM, "3957198221-190478553"
        )
        assert result == []


class TestPropagateGenreMappings:
    """Tests for _propagate_genre_mappings_to_parents."""

    async def test_propagation_derives_album_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Track genre mappings are propagated to the track's album."""
        instance_id = "fs_local_instance_album"
        genre = await genre_ctrl.add_item_to_library(_make_genre("PropAlbumGenre"))
        artist = await _add_test_artist(mass, "PropAlbum Artist")
        album = await _add_test_album(mass, "PropAlbum Album")
        track = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name="PropAlbum Track",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
        )
        track_id = int(track.item_id)
        album_id = int(album.item_id)
        genre_id = int(genre.item_id)

        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": track_id,
                "provider_domain": "filesystem_local",
                "provider_instance": instance_id,
                "provider_item_id": f"track_{track_id}",
            },
        )
        await mass.music.database.commit()
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "PropAlbumGenre")

        mock_provider = MagicMock()
        mock_provider.domain = "filesystem_local"
        mock_provider.instance_id = instance_id
        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album' AND is_derived = 1",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(rows) == 1

    async def test_propagation_derives_artist_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Track genre mappings are propagated to the track's artist."""
        instance_id = "fs_local_instance_artist"
        genre = await genre_ctrl.add_item_to_library(_make_genre("PropArtistGenre"))
        track = await _add_test_track(mass, "PropArtist Track")
        track_id = int(track.item_id)
        artist_id = int(track.artists[0].item_id)
        genre_id = int(genre.item_id)

        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": track_id,
                "provider_domain": "filesystem_local",
                "provider_instance": instance_id,
                "provider_item_id": f"track_{track_id}",
            },
        )
        await mass.music.database.commit()
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "PropArtistGenre")

        mock_provider = MagicMock()
        mock_provider.domain = "filesystem_local"
        mock_provider.instance_id = instance_id
        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid "
            "AND media_type = 'artist' AND is_derived = 1",
            {"gid": genre_id, "mid": artist_id},
            limit=0,
        )
        assert len(rows) == 1

    async def test_propagation_respects_exclusion(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Excluded genre-album pairs are not derived even when propagation is enabled."""
        instance_id = "fs_local_instance_excl"
        genre = await genre_ctrl.add_item_to_library(_make_genre("PropExclGenre"))
        artist = await _add_test_artist(mass, "PropExcl Artist")
        album = await _add_test_album(mass, "PropExcl Album")
        track = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name="PropExcl Track",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
        )
        track_id = int(track.item_id)
        album_id = int(album.item_id)
        genre_id = int(genre.item_id)

        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": track_id,
                "provider_domain": "filesystem_local",
                "provider_instance": instance_id,
                "provider_item_id": f"track_{track_id}",
            },
        )
        await mass.music.database.commit()
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "PropExclGenre")
        await genre_ctrl.exclude_genre_from_media_item(genre_id, MediaType.ALBUM, album_id)

        mock_provider = MagicMock()
        mock_provider.domain = "filesystem_local"
        mock_provider.instance_id = instance_id
        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album'",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(rows) == 0

    async def test_propagation_disabled_removes_derived(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Disabling propagation on all providers removes previously derived mappings."""
        instance_id = "fs_local_instance_disable"
        genre = await genre_ctrl.add_item_to_library(_make_genre("PropDisableGenre"))
        artist = await _add_test_artist(mass, "PropDisable Artist")
        album = await _add_test_album(mass, "PropDisable Album")
        track = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name="PropDisable Track",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
        )
        track_id = int(track.item_id)
        album_id = int(album.item_id)
        genre_id = int(genre.item_id)

        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": track_id,
                "provider_domain": "filesystem_local",
                "provider_instance": instance_id,
                "provider_item_id": f"track_{track_id}",
            },
        )
        await mass.music.database.commit()
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "PropDisableGenre")

        mock_provider = MagicMock()
        mock_provider.domain = "filesystem_local"
        mock_provider.instance_id = instance_id

        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        rows_after_enable = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album' AND is_derived = 1",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(rows_after_enable) == 1

        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=False)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        rows_after_disable = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album' AND is_derived = 1",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(rows_after_disable) == 0

    async def test_derived_mapping_replaced_by_direct_when_album_gains_genres(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """
        Derived mapping is replaced by direct when album gains own genre metadata.

        Verifies the mapping becomes a direct one instead of leaving the album with no mapping.
        """
        instance_id = "fs_local_instance_transition"
        genre = await genre_ctrl.add_item_to_library(_make_genre("TransitionGenre"))
        artist = await _add_test_artist(mass, "Transition Artist")
        album = await _add_test_album(mass, "Transition Album")
        track = await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name="Transition Track",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
        )
        track_id = int(track.item_id)
        album_id = int(album.item_id)
        genre_id = int(genre.item_id)

        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )
        await mass.music.database.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": "track",
                "item_id": track_id,
                "provider_domain": "filesystem_local",
                "provider_instance": instance_id,
                "provider_item_id": f"track_{track_id}",
            },
        )
        await mass.music.database.commit()
        await _set_track_genres(mass, track_id, ["TransitionGenre"])
        await genre_ctrl.add_media_mapping(genre_id, MediaType.TRACK, track_id, "TransitionGenre")

        mock_provider = MagicMock()
        mock_provider.domain = "filesystem_local"
        mock_provider.instance_id = instance_id

        # Step 1: album has no genres yet → propagation creates a derived mapping.
        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._propagate_genre_mappings_to_parents()

        derived_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album'",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(derived_rows) == 1
        assert derived_rows[0]["is_derived"] == 1

        # Step 2: album gains its own genre metadata.
        await _set_album_genres(mass, album_id, ["TransitionGenre"])

        # Step 3: incremental scan must replace the derived mapping with a direct one,
        # not leave the album with no mapping at all.
        with (
            patch.object(
                type(mass.music),
                "providers",
                new_callable=PropertyMock,
                return_value=[mock_provider],
            ),
            patch.object(
                mass.config, "get_provider_config_value", new=AsyncMock(return_value=True)
            ),
        ):
            await genre_ctrl._bulk_scan_unmapped_genres()

        final_rows = await mass.music.database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :gid AND media_id = :mid AND media_type = 'album'",
            {"gid": genre_id, "mid": album_id},
            limit=0,
        )
        assert len(final_rows) == 1
        assert final_rows[0]["is_derived"] == 0
        assert final_rows[0]["alias"] == "TransitionGenre"


# ===================================================================
# Group N: Genre Media Counts (4 tests)
# ===================================================================


class TestGetGenreMediaCounts:
    """Tests for get_genre_media_counts."""

    async def test_empty_ids_returns_empty(self, genre_ctrl: GenreController) -> None:
        """Empty input returns empty dict without hitting the database."""
        result = await genre_ctrl.get_genre_media_counts([])
        assert result == {}

    async def test_all_media_types_present_with_zero_default(
        self, genre_ctrl: GenreController
    ) -> None:
        """Result contains every MEDIA_TABLES media type, defaulting to 0."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CountDefaults"))
        gid = genre.item_id
        result = await genre_ctrl.get_genre_media_counts([gid])
        assert gid in result
        expected_keys = {"track", "album", "artist", "playlist", "radio", "audiobook", "podcast"}
        assert set(result[gid].keys()) == expected_keys
        assert all(v == 0 for v in result[gid].values())

    async def test_counts_track_mappings(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Track mappings are reflected in the track count; other types remain 0."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("CountTracks"))
        track1 = await _add_test_track(mass, "CountTrack1")
        track2 = await _add_test_track(mass, "CountTrack2")
        gid = genre.item_id
        await genre_ctrl.add_media_mapping(gid, MediaType.TRACK, track1.item_id, "CountTracks")
        await genre_ctrl.add_media_mapping(gid, MediaType.TRACK, track2.item_id, "CountTracks")
        result = await genre_ctrl.get_genre_media_counts([gid])
        assert result[gid]["track"] == 2
        assert result[gid]["album"] == 0

    async def test_counts_multiple_genres_independently(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Counts for multiple genre IDs are computed independently."""
        g1 = await genre_ctrl.add_item_to_library(_make_genre("MultiCountA"))
        g2 = await genre_ctrl.add_item_to_library(_make_genre("MultiCountB"))
        track = await _add_test_track(mass, "MultiCount Track")
        album = await _add_test_album(mass, "MultiCount Album")
        await genre_ctrl.add_media_mapping(
            g1.item_id, MediaType.TRACK, track.item_id, "MultiCountA"
        )
        await genre_ctrl.add_media_mapping(
            g2.item_id, MediaType.ALBUM, album.item_id, "MultiCountB"
        )
        result = await genre_ctrl.get_genre_media_counts([g1.item_id, g2.item_id])
        assert result[g1.item_id]["track"] == 1
        assert result[g1.item_id]["album"] == 0
        assert result[g2.item_id]["album"] == 1
        assert result[g2.item_id]["track"] == 0


# ===================================================================
# Group O: Global Genre Exclusion (9 tests)
# ===================================================================

# Two distinct default entries with a translation_key for the tests below.
_tk_entries = [e for e in DEFAULT_GENRE_MAPPING if e.get("translation_key")]
assert len(_tk_entries) >= 2, (
    "DEFAULT_GENRE_MAPPING must contain at least two entries with a translation_key "
    "for global genre exclusion tests"
)
_DEFAULT_ENTRY_A = _tk_entries[0]  # used for deletion-only test
_DEFAULT_ENTRY_B = _tk_entries[1]  # used for delete-then-restore test


class TestGlobalGenreExclusion:
    """Tests for the global genre exclusion API and scanner guard."""

    async def test_delete_sets_is_excluded_flag(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """remove_item_from_library sets is_excluded = 1 on the genre row."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("GblExcl1"))
        genre_id = int(genre.item_id)
        await genre_ctrl.remove_item_from_library(genre_id)
        row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row is not None
        assert row["is_excluded"] == 1

    async def test_get_exclusions_lists_deleted_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """get_global_genre_exclusions includes a genre after it is deleted."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("GblExcl2"))
        await genre_ctrl.remove_item_from_library(int(genre.item_id))
        exclusions = await genre_ctrl.get_global_genre_exclusions()
        names = {e["name"] for e in exclusions}
        assert "GblExcl2" in names

    async def test_default_genre_exclusion_preserves_translation_key(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Excluding a default genre preserves its translation_key on the row."""
        tk = _DEFAULT_ENTRY_A["translation_key"]
        search_name = create_safe_string(_DEFAULT_ENTRY_A["genre"], True, True)
        db_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"search_name": search_name})
        assert db_row is not None, "default genre must be seeded at startup"
        await genre_ctrl.remove_item_from_library(int(db_row["item_id"]))
        excl_row = await mass.music.database.get_row(
            DB_TABLE_GENRES, {"item_id": int(db_row["item_id"]), "is_excluded": 1}
        )
        assert excl_row is not None
        assert excl_row["translation_key"] == tk

    async def test_scanner_guard_blocks_excluded_name(self, genre_ctrl: GenreController) -> None:
        """_find_genres_for_alias returns [] for a globally excluded genre name."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("GblExclScan"))
        await genre_ctrl.remove_item_from_library(int(genre.item_id))
        result = await genre_ctrl._find_genres_for_alias("GblExclScan", None)
        assert result == []

    async def test_restore_custom_genre_is_not_default(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Restoring a custom (non-default) genre leaves is_default as 0."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("GblExclCustom"))
        genre_id = int(genre.item_id)
        await genre_ctrl.remove_item_from_library(genre_id)
        restored = await genre_ctrl.remove_global_genre_exclusion(genre_id)
        db_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert db_row is not None
        assert db_row["is_excluded"] == 0
        assert db_row["is_default"] == 0
        assert int(restored.item_id) == genre_id

    async def test_restore_default_genre_translation_key_preserved(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Restoring a default genre retains the correct translation_key (row was never deleted)."""
        tk = _DEFAULT_ENTRY_B["translation_key"]
        search_name = create_safe_string(_DEFAULT_ENTRY_B["genre"], True, True)
        db_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"search_name": search_name})
        assert db_row is not None, "default genre must be seeded at startup"
        genre_id = int(db_row["item_id"])
        await genre_ctrl.remove_item_from_library(genre_id)
        await genre_ctrl.remove_global_genre_exclusion(genre_id)
        restored_row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert restored_row is not None
        assert restored_row["is_excluded"] == 0
        assert restored_row["translation_key"] == tk

    async def test_restore_clears_exclusion_flag(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """is_excluded is set back to 0 after a successful restore."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("GblExclClean"))
        genre_id = int(genre.item_id)
        await genre_ctrl.remove_item_from_library(genre_id)
        await genre_ctrl.remove_global_genre_exclusion(genre_id)
        row = await mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": genre_id})
        assert row is not None
        assert row["is_excluded"] == 0

    async def test_restore_nonexistent_raises_key_error(self, genre_ctrl: GenreController) -> None:
        """remove_global_genre_exclusion raises KeyError for an unknown genre id."""
        with pytest.raises(KeyError):
            await genre_ctrl.remove_global_genre_exclusion(999_999_999)

    async def test_merge_does_not_exclude_source_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """merge_genres hard-deletes the source: it must not appear in the exclusion list."""
        source = await genre_ctrl.add_item_to_library(_make_genre("GblMergeSource"))
        target = await genre_ctrl.add_item_to_library(_make_genre("GblMergeTarget"))
        await genre_ctrl.merge_genres([source.item_id], target.item_id)
        exclusions = await genre_ctrl.get_global_genre_exclusions()
        names = {e["name"] for e in exclusions}
        assert "GblMergeSource" not in names


class TestGenreContentTypeNamespacing:
    """Genre resolution and mappings are scoped per content_type taxonomy (2b)."""

    async def test_find_genres_for_alias_namespaces_by_content_type(
        self, genre_ctrl: GenreController
    ) -> None:
        """The same name resolves to a distinct genre per taxonomy, each tagged correctly."""
        music = await genre_ctrl._find_genres_for_alias("Comedy", None)
        podcast = await genre_ctrl._find_genres_for_alias("Comedy", MediaType.PODCAST)
        audiobook = await genre_ctrl._find_genres_for_alias("Comedy", MediaType.AUDIOBOOK)

        assert len({music[0], podcast[0], audiobook[0]}) == 3
        assert (await genre_ctrl.get_library_item(music[0])).content_type is None
        assert (await genre_ctrl.get_library_item(podcast[0])).content_type is MediaType.PODCAST
        assert (await genre_ctrl.get_library_item(audiobook[0])).content_type is MediaType.AUDIOBOOK

    async def test_find_genres_for_alias_does_not_cross_namespaces(
        self, genre_ctrl: GenreController
    ) -> None:
        """A lookup in one taxonomy never returns a genre that lives in another."""
        music = await genre_ctrl._find_genres_for_alias("Zzklezmertest", None)
        podcast = await genre_ctrl._find_genres_for_alias("Zzklezmertest", MediaType.PODCAST)
        assert music[0] not in podcast
        assert (await genre_ctrl.get_library_item(music[0])).content_type is None

    async def test_scanner_buckets_genres_by_content_type(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """The scanner files a track's and a podcast's same-named genre as distinct entities."""
        track = await _add_test_track(mass, "NsTrack")
        await _set_track_genres(mass, int(track.item_id), ["Zmystery9"])
        podcast = await _add_test_podcast(mass, "NsPodcast")
        await _set_podcast_genres(mass, int(podcast.item_id), ["Zmystery9"])

        await genre_ctrl._bulk_scan_unmapped_genres()

        rows = await mass.music.database.get_rows_from_query(
            f"SELECT item_id, content_type FROM {DB_TABLE_GENRES} WHERE search_name = :sn",
            {"sn": create_safe_string("Zmystery9", True, True)},
            limit=0,
        )
        by_content_type = {row["content_type"]: int(row["item_id"]) for row in rows}
        # one music genre (NULL) and one podcast genre exist for the same name
        assert None in by_content_type
        assert MediaType.PODCAST.value in by_content_type
        # the podcast maps to the podcast-namespace genre, never the music one
        pod_maps = await mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_type = :mt AND media_id = :mid",
            {"mt": MediaType.PODCAST.value, "mid": int(podcast.item_id)},
            limit=0,
        )
        assert {int(r["genre_id"]) for r in pod_maps} == {by_content_type[MediaType.PODCAST.value]}

    async def test_cleanup_rehomes_legacy_cross_namespace_mapping(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Cleanup removes a legacy podcast→music-genre mapping so the item can re-home."""
        podcast = await _add_test_podcast(mass, "RehomePod")
        await _set_podcast_genres(mass, int(podcast.item_id), ["Spoken Word"])
        # simulate the pre-namespacing state: the podcast mapped to a music (NULL) genre
        music_genre = await genre_ctrl._find_genres_for_alias("Spoken Word", None)
        await mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": music_genre[0],
                "media_id": int(podcast.item_id),
                "media_type": MediaType.PODCAST.value,
                "alias": "Spoken Word",
                "is_derived": 0,
                "is_manual": 0,
            },
        )

        await genre_ctrl._cleanup_stale_genre_mappings()

        remaining = await mass.music.database.get_rows_from_query(
            f"SELECT 1 FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE media_type = :mt AND media_id = :mid AND genre_id = :gid",
            {
                "mt": MediaType.PODCAST.value,
                "mid": int(podcast.item_id),
                "gid": music_genre[0],
            },
            limit=0,
        )
        assert remaining == []

    async def test_soft_delete_restore_scoped_by_content_type(
        self, genre_ctrl: GenreController
    ) -> None:
        """
        A re-add restores only a soft-deleted genre of the same taxonomy.

        A soft-deleted podcast "Comedy" must not be revived (and re-tagged) when a music
        "Comedy" is added; the music genre is inserted as a distinct, new row.
        """
        podcast = await genre_ctrl.add_item_to_library(
            Genre(
                item_id="0",
                provider="library",
                name="ScopedComedy",
                provider_mappings=set(),
                content_type=MediaType.PODCAST,
            )
        )
        await genre_ctrl.mass.music.database.update(
            DB_TABLE_GENRES, {"item_id": int(podcast.item_id)}, {"is_excluded": 1}
        )

        music = await genre_ctrl.add_item_to_library(_make_genre("ScopedComedy"))

        assert int(music.item_id) != int(podcast.item_id)
        assert music.content_type is None
        # the podcast row is left untouched (still soft-deleted)
        pod_rows = await genre_ctrl.mass.music.database.get_rows_from_query(
            f"SELECT is_excluded FROM {DB_TABLE_GENRES} WHERE item_id = :id",
            {"id": int(podcast.item_id)},
            limit=1,
        )
        assert pod_rows[0]["is_excluded"] == 1

        # re-adding within the same taxonomy DOES restore the soft-deleted row
        revived = await genre_ctrl.add_item_to_library(
            Genre(
                item_id="0",
                provider="library",
                name="ScopedComedy",
                provider_mappings=set(),
                content_type=MediaType.PODCAST,
            )
        )
        assert int(revived.item_id) == int(podcast.item_id)


class TestDefaultTaxonomySeeding:
    """restore_default_genres seeds curated music, podcast, and audiobook taxonomies (2c)."""

    async def test_full_restore_seeds_every_taxonomy(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Full restore seeds the curated default genres of all three taxonomies."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT content_type, COUNT(*) AS cnt FROM {DB_TABLE_GENRES} "
            "WHERE is_default = 1 GROUP BY content_type",
            limit=0,
        )
        counts = {row["content_type"]: row["cnt"] for row in rows}
        assert counts.get(None) == len(DEFAULT_GENRE_MAPPING)
        assert counts.get(MediaType.PODCAST.value) == len(DEFAULT_PODCAST_GENRE_MAPPING)
        assert counts.get(MediaType.AUDIOBOOK.value) == len(DEFAULT_AUDIOBOOK_GENRE_MAPPING)

    async def test_curated_genre_carries_translation_key_and_content_type(
        self, genre_ctrl: GenreController
    ) -> None:
        """A seeded podcast default (True Crime) lands in the podcast namespace with its key."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        items = await genre_ctrl.library_items(search="True Crime", hide_empty=False)
        match = next((g for g in items if g.content_type is MediaType.PODCAST), None)
        assert match is not None
        assert match.translation_key == "true_crime"

    async def test_same_name_distinct_per_taxonomy(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A name in both spoken-word lists (History) yields one genre per taxonomy."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        rows = await mass.music.database.get_rows_from_query(
            f"SELECT content_type FROM {DB_TABLE_GENRES} WHERE search_name = :sn",
            {"sn": create_safe_string("History", True, True)},
            limit=0,
        )
        content_types = {row["content_type"] for row in rows}
        assert MediaType.PODCAST.value in content_types
        assert MediaType.AUDIOBOOK.value in content_types

    async def test_partial_restore_is_idempotent(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Re-running a partial restore does not create duplicate genres."""
        await genre_ctrl.restore_default_genres(full_restore=False)
        before = await mass.music.database.get_count(DB_TABLE_GENRES)
        await genre_ctrl.restore_default_genres(full_restore=False)
        after = await mass.music.database.get_count(DB_TABLE_GENRES)
        assert before == after

    async def test_content_type_filter_composes_with_hide_empty(
        self, genre_ctrl: GenreController
    ) -> None:
        """content_type narrows library_items to a taxonomy and composes with hide_empty."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        # default-only (hide_empty=None) within the podcast taxonomy = the curated podcast defaults
        podcast_defaults = await genre_ctrl.library_items(
            content_type="podcast", hide_empty=None, limit=0
        )
        assert len(podcast_defaults) == len(DEFAULT_PODCAST_GENRE_MAPPING)
        assert all(g.content_type is MediaType.PODCAST for g in podcast_defaults)

    async def test_targeted_restore_seeds_only_requested_taxonomy(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A non-destructive restore scoped to one taxonomy touches only that taxonomy."""

        async def default_counts() -> dict[str | None, int]:
            rows = await mass.music.database.get_rows_from_query(
                f"SELECT content_type, COUNT(*) AS cnt FROM {DB_TABLE_GENRES} "
                "WHERE is_default = 1 GROUP BY content_type",
                limit=0,
            )
            return {row["content_type"]: row["cnt"] for row in rows}

        # establish a deterministic, fully-seeded baseline, then drop one podcast default
        await genre_ctrl.restore_default_genres(full_restore=True)
        before = await default_counts()
        victim = await mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_GENRES} WHERE content_type = :ct AND is_default = 1",
            {"ct": MediaType.PODCAST.value},
            limit=1,
        )
        await mass.music.database.delete(DB_TABLE_GENRES, {"item_id": victim[0]["item_id"]})

        created = await genre_ctrl.restore_default_genres(
            full_restore=False, content_type="podcast"
        )
        assert created
        assert all(g.content_type is MediaType.PODCAST for g in created)

        after = await default_counts()
        # the podcast default was restored, the other taxonomies were left untouched
        assert after[MediaType.PODCAST.value] == before[MediaType.PODCAST.value]
        assert after.get(None) == before.get(None)
        assert after.get(MediaType.AUDIOBOOK.value) == before.get(MediaType.AUDIOBOOK.value)

    async def test_targeted_restore_unknown_taxonomy_raises(
        self, genre_ctrl: GenreController
    ) -> None:
        """An unrecognised taxonomy is rejected rather than silently restoring nothing."""
        with pytest.raises(ValueError, match="Unknown genre taxonomy"):
            await genre_ctrl.restore_default_genres(full_restore=False, content_type="bogus")
        # show-all within the taxonomy never leaks genres from another taxonomy
        podcast_all = await genre_ctrl.library_items(
            content_type="podcast", hide_empty=False, limit=0
        )
        assert podcast_all
        assert all(g.content_type is MediaType.PODCAST for g in podcast_all)

    async def test_content_type_music_filter(self, genre_ctrl: GenreController) -> None:
        """content_type="music" returns only the music/general (NULL) taxonomy genres."""
        await genre_ctrl.restore_default_genres(full_restore=True)
        music = await genre_ctrl.library_items(content_type="music", hide_empty=False, limit=0)
        assert music
        assert all(g.content_type is None for g in music)
        # spoken-word genres are distinct entities and never appear in the music taxonomy
        podcast = await genre_ctrl.library_items(content_type="podcast", hide_empty=False, limit=0)
        music_ids = {g.item_id for g in music}
        assert not any(g.item_id in music_ids for g in podcast)


class TestGenreIconMetadata:
    """_get_genre_icon_metadata prefers a taxonomy subfolder icon, falling back to flat."""

    @staticmethod
    def _make_icons(tmp_path: Path, *rel_paths: str) -> None:
        for rel in rel_paths:
            icon = tmp_path / "genres" / rel
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_text("<svg/>")

    def test_subfolder_icon_preferred(self, tmp_path: Path, monkeypatch: Any) -> None:
        """A taxonomy-specific icon wins over the flat one."""
        self._make_icons(tmp_path, "history.svg", "podcast/history.svg")
        monkeypatch.setattr(
            "music_assistant.controllers.music.media.genres.RESOURCES_DIR", tmp_path
        )
        md = GenreController._get_genre_icon_metadata("history", MediaType.PODCAST)
        assert md is not None
        assert md.images is not None
        assert md.images[0].path == "genres/podcast/history.svg"

    def test_falls_back_to_flat(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Without a taxonomy override, the flat/shared icon is used."""
        self._make_icons(tmp_path, "history.svg")
        monkeypatch.setattr(
            "music_assistant.controllers.music.media.genres.RESOURCES_DIR", tmp_path
        )
        md = GenreController._get_genre_icon_metadata("history", MediaType.PODCAST)
        assert md is not None
        assert md.images is not None
        assert md.images[0].path == "genres/history.svg"

    def test_music_uses_flat(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Music genres (content_type None) resolve to the flat path."""
        self._make_icons(tmp_path, "blues.svg")
        monkeypatch.setattr(
            "music_assistant.controllers.music.media.genres.RESOURCES_DIR", tmp_path
        )
        md = GenreController._get_genre_icon_metadata("blues", None)
        assert md is not None
        assert md.images is not None
        assert md.images[0].path == "genres/blues.svg"

    def test_missing_icon_returns_none(self, tmp_path: Path, monkeypatch: Any) -> None:
        """No matching SVG (subfolder or flat) yields no metadata."""
        self._make_icons(tmp_path)
        monkeypatch.setattr(
            "music_assistant.controllers.music.media.genres.RESOURCES_DIR", tmp_path
        )
        assert GenreController._get_genre_icon_metadata("nope", MediaType.PODCAST) is None


# ===================================================================
# Custom genre images (user uploaded)
# ===================================================================


def _png_base64() -> str:
    """Return a minimal valid PNG as base64 string."""
    buf = BytesIO()
    PILImage.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _default_genre_id(mass: MusicAssistant, translation_key: str) -> int:
    """Return the library id of a seeded default (music) genre by translation key."""
    row = await mass.music.database.get_row(DB_TABLE_GENRES, {"translation_key": translation_key})
    assert row is not None
    return int(row["item_id"])


class TestCustomGenreImages:
    """Tests for setting/removing user-uploaded custom genre images."""

    async def test_set_custom_image_on_default_genre(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A custom image overrides the builtin icon and lands on disk."""
        genre_id = await _default_genre_id(mass, "blues")
        updated = await genre_ctrl.set_item_image(genre_id, _png_base64())
        assert updated.image is not None
        assert updated.image.path.startswith(f"{CUSTOM_IMAGES_DIRNAME}/genre.{genre_id}.")
        assert updated.image.path.endswith(".png")
        assert updated.image.provider == "builtin"
        assert (Path(mass.storage_path) / updated.image.path).is_file()

    async def test_remove_restores_builtin_icon(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """Removing the custom image brings the builtin icon back and deletes the file."""
        genre_id = await _default_genre_id(mass, "jazz")
        updated = await genre_ctrl.set_item_image(genre_id, _png_base64())
        assert updated.image is not None
        custom_path = updated.image.path
        restored = await genre_ctrl.remove_item_image(genre_id)
        assert restored.image is not None
        assert restored.image.path == "genres/jazz.svg"
        assert not (Path(mass.storage_path) / custom_path).exists()

    async def test_user_genre_set_then_remove_ends_imageless(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A user-created genre has no default icon to restore."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("My Very Own Genre"))
        updated = await genre_ctrl.set_item_image(genre.item_id, _png_base64())
        assert updated.image is not None
        custom_path = updated.image.path
        removed = await genre_ctrl.remove_item_image(genre.item_id)
        assert removed.image is None
        assert not (Path(mass.storage_path) / custom_path).exists()

    async def test_partial_restore_keeps_custom_icon(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """restore_default_genres (partial) never clobbers a custom image."""
        genre_id = await _default_genre_id(mass, "rock")
        updated = await genre_ctrl.set_item_image(genre_id, _png_base64())
        assert updated.image is not None
        custom_path = updated.image.path
        await genre_ctrl.restore_default_genres(full_restore=False)
        after = await genre_ctrl.get_library_item(genre_id)
        assert after.image is not None
        assert after.image.path == custom_path

    async def test_full_restore_deletes_custom_image_file(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A full restore wipes the genres and cleans up their custom image files."""
        genre_id = await _default_genre_id(mass, "soul")
        updated = await genre_ctrl.set_item_image(genre_id, _png_base64())
        assert updated.image is not None
        custom_file = Path(mass.storage_path) / updated.image.path
        assert custom_file.is_file()
        await genre_ctrl.restore_default_genres(full_restore=True)
        assert not custom_file.exists()

    async def test_soft_delete_keeps_custom_image(
        self, mass: MusicAssistant, genre_ctrl: GenreController
    ) -> None:
        """A soft-deleted genre keeps its custom image and gets it back on re-add."""
        genre = await genre_ctrl.add_item_to_library(_make_genre("Comeback Genre"))
        updated = await genre_ctrl.set_item_image(genre.item_id, _png_base64())
        assert updated.image is not None
        custom_path = updated.image.path
        await genre_ctrl.remove_item_from_library(genre.item_id)
        assert (Path(mass.storage_path) / custom_path).is_file()
        restored = await genre_ctrl.add_item_to_library(_make_genre("Comeback Genre"))
        assert restored.image is not None
        assert restored.image.path == custom_path
