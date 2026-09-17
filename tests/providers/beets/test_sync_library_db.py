"""Sync tests for the beets provider against a real Music Assistant library database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant
from music_assistant.providers.beets import BeetsProvider
from tests.providers.beets.beets_db import BeetsDb, album_fields, item_fields
from tests.providers.beets.conftest import INSTANCE_ID, track_prov_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track

MakeProvider = Callable[..., Awaitable[BeetsProvider]]
LIBRARY_A = "beets--libA"
LIBRARY_B = "beets--libB"

# the sync's TaskManager runs its imports through mass.create_task, which pins tasks to
# mass.loop, so each test must run on the loop its class-scoped Music Assistant was created on
pytestmark = pytest.mark.asyncio(loop_scope="class")


@pytest.fixture(scope="class")
def library_mass(music_mass_class: MusicAssistant) -> MusicAssistant:
    """Return a database-only Music Assistant with an empty library for each test class."""
    return music_mass_class


async def _attach(
    make_provider: MakeProvider,
    mass: MusicAssistant,
    favorite_rating_threshold: float | None = None,
    instance_id: str = INSTANCE_ID,
    db_path: Path | None = None,
) -> BeetsProvider:
    """
    Return a beets provider around a test database, loaded into the real Music Assistant.

    Attaching a provider with the instance id of an attached one replaces it, the way a config
    change reloads it.

    :param make_provider: The beets provider factory fixture.
    :param mass: The database-only Music Assistant.
    :param favorite_rating_threshold: The favorite threshold the provider is configured with.
    :param instance_id: The provider instance id.
    :param db_path: The beets database to read, when not the default test database.
    """
    provider = await make_provider(
        favorite_rating_threshold=favorite_rating_threshold,
        instance_id=instance_id,
        db_path=db_path,
    )
    provider.mass = mass
    # make_provider skips Provider.__init__, which sets `available`; the library controller
    # finds providers through mass.get_provider, which only returns available ones
    provider.available = True
    mass._providers[instance_id] = provider
    return provider


async def _sync(provider: BeetsProvider) -> None:
    """Run a track sync and fail when any beets item could not be imported."""
    await provider.sync_library(MediaType.TRACK)
    provider.logger.error.assert_not_called()  # type: ignore[attr-defined]


def _add_item(beets_db: BeetsDb, album_id: int | None, title: str, **overrides: Any) -> int:
    """
    Add a beets item with its own recording ids, so the library does not merge it with others.

    :param beets_db: The beets test database.
    :param album_id: The item's album, or None for a singleton.
    :param title: The item title.
    :param overrides: Further fields to set.
    """
    fields = {
        "album_id": album_id,
        "title": title,
        "mb_trackid": str(uuid4()),
        "acoustid_id": str(uuid4()),
        "isrc": None,
        **overrides,
    }
    return beets_db.add_item(**item_fields(**fields))


def _add_album(beets_db: BeetsDb, name: str, **overrides: Any) -> int:
    """
    Add a beets album with its own release ids, so the library does not merge it with others.

    :param beets_db: The beets test database.
    :param name: The album name.
    :param overrides: Further fields to set.
    """
    fields = {
        "album": name,
        "mb_albumid": str(uuid4()),
        "mb_releasegroupid": str(uuid4()),
        "barcode": None,
        "asin": None,
        **overrides,
    }
    return beets_db.add_album(**album_fields(**fields))


def _artist_fields(name: str, mbid: str, prefix: str = "") -> dict[str, str]:
    """
    Return the beets artist fields naming a single artist.

    :param name: The artist name.
    :param mbid: The artist MusicBrainz id.
    :param prefix: "album" for an album's album artist, "" for an item's artist.
    """
    return {
        f"{prefix}artist": name,
        f"{prefix}artist_sort": name,
        f"mb_{prefix}artistid": mbid,
        f"{prefix}artists": name,
        f"{prefix}artists_sort": name,
        f"mb_{prefix}artistids": mbid,
    }


async def _library_track(
    mass: MusicAssistant, beets_item_id: int, instance_id: str = INSTANCE_ID
) -> Track | None:
    """Return the library track that maps a beets item of a beets instance."""
    assert mass.music.database
    # read the track mapping row itself, so the check does not rely on the lookups under test
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} WHERE media_type = 'track' "
        "AND provider_instance = :instance_id AND provider_item_id = :item_id",
        {"instance_id": instance_id, "item_id": track_prov_id(beets_item_id, instance_id)},
        limit=0,
    )
    if not rows:
        return None
    return await mass.music.tracks.get_library_item(rows[0]["item_id"])


async def _library_tracks(mass: MusicAssistant) -> dict[str, Track]:
    """Return every library track by name."""
    return {track.name: track for track in await mass.music.tracks.get_library_items_by_query()}


async def _library_albums(mass: MusicAssistant) -> dict[str, Album]:
    """Return every library album by name."""
    return {album.name: album for album in await mass.music.albums.get_library_items_by_query()}


def _mappings(item: Track | Album) -> set[tuple[str, str]]:
    """Return the (provider instance, provider item id) pairs of a library item."""
    return {(mapping.provider_instance, mapping.item_id) for mapping in item.provider_mappings}


class TestUnchangedLibrary:
    """A sync of a beets library that did not change leaves the Music Assistant library alone."""

    async def test_second_sync_imports_nothing(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """Stored checksums match every item, so the second sync imports nothing."""
        album_id = beets_db.add_album(**album_fields())
        first = _add_item(beets_db, album_id, "One", track=1)
        second = _add_item(beets_db, album_id, "Two", track=2)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        assert await _library_track(library_mass, first) is not None
        assert await _library_track(library_mass, second) is not None

        tracks = library_mass.music.tracks
        with patch.object(tracks, "add_item_to_library", wraps=tracks.add_item_to_library) as add:
            await _sync(provider)

        add.assert_not_awaited()


class TestEditedItem:
    """Editing an item in beets updates exactly that track."""

    async def test_edit_imports_only_that_item(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """Only the edited item is imported again and the library shows its new title."""
        album_id = beets_db.add_album(**album_fields())
        _add_item(beets_db, album_id, "One", track=1)
        edited = _add_item(beets_db, album_id, "Two", track=2)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        beets_db.update_item(edited, title="Renamed")

        tracks = library_mass.music.tracks
        with patch.object(tracks, "add_item_to_library", wraps=tracks.add_item_to_library) as add:
            await _sync(provider)

        assert [call.args[0].item_id for call in add.await_args_list] == [track_prov_id(edited)]
        library_track = await _library_track(library_mass, edited)
        assert library_track is not None
        assert library_track.name == "Renamed"


class TestFavoriteThreshold:
    """Setting the favorite threshold favorites the rated tracks of an unchanged library."""

    async def test_threshold_marks_rated_track_favorite(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """Only the item whose rating reaches the new threshold is imported and favorited."""
        album_id = beets_db.add_album(**album_fields())
        rated = _add_item(beets_db, album_id, "Rated", track=1)
        beets_db.set_item_flex(rated, "rating", "0.9")
        unrated = _add_item(beets_db, album_id, "Unrated", track=2)
        await _sync(await _attach(make_provider, library_mass))
        rated_track = await _library_track(library_mass, rated)
        assert rated_track is not None
        assert rated_track.favorite is False

        provider = await _attach(make_provider, library_mass, favorite_rating_threshold=0.8)
        tracks = library_mass.music.tracks
        with patch.object(tracks, "add_item_to_library", wraps=tracks.add_item_to_library) as add:
            await _sync(provider)

        assert [call.args[0].item_id for call in add.await_args_list] == [track_prov_id(rated)]
        rated_track = await _library_track(library_mass, rated)
        unrated_track = await _library_track(library_mass, unrated)
        assert rated_track is not None
        assert unrated_track is not None
        assert rated_track.favorite is True
        assert unrated_track.favorite is False


class TestReimportedItem:
    """A beets re-import that gives an item a new id keeps the library track it merges into."""

    async def test_reimported_item_keeps_library_track(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """The track keeps its id and favorite and its beets mapping moves to the new id."""
        album_id = beets_db.add_album(**album_fields())
        recording = {"mb_trackid": str(uuid4()), "acoustid_id": str(uuid4()), "track": 1}
        old_id = _add_item(beets_db, album_id, "Song", **recording)
        beets_db.set_item_flex(old_id, "rating", "0.9")
        # a later row keeps sqlite from handing the re-imported item the deleted item's id
        _add_item(beets_db, album_id, "Other", track=2)
        provider = await _attach(make_provider, library_mass, favorite_rating_threshold=0.8)
        await _sync(provider)
        library_track = await _library_track(library_mass, old_id)
        assert library_track is not None
        assert library_track.favorite is True

        beets_db.delete_item(old_id)
        new_id = _add_item(beets_db, album_id, "Song", **recording)
        assert new_id != old_id
        await _sync(provider)

        assert await _library_track(library_mass, old_id) is None
        reimported = await _library_track(library_mass, new_id)
        assert reimported is not None
        assert reimported.item_id == library_track.item_id
        assert reimported.favorite is True
        assert {
            mapping.item_id
            for mapping in reimported.provider_mappings
            if mapping.provider_instance == INSTANCE_ID
        } == {track_prov_id(new_id)}


class TestMergedItems:
    """Two beets items of the same recording on different albums share one library track."""

    async def test_editing_one_item_keeps_the_other_items_mapping(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """After an edit to one item, the library track still maps both beets items."""
        recording = {"mb_trackid": str(uuid4()), "acoustid_id": str(uuid4()), "track": 1}
        first_album = _add_album(beets_db, "First Album")
        second_album = _add_album(beets_db, "Second Album")
        edited = _add_item(beets_db, first_album, "Song", **recording)
        other = _add_item(beets_db, second_album, "Song", length=215.9, **recording)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        library_track = await _library_track(library_mass, edited)
        assert library_track is not None
        both = {(INSTANCE_ID, track_prov_id(edited)), (INSTANCE_ID, track_prov_id(other))}
        assert _mappings(library_track) == both

        beets_db.update_item(edited, comments="Edited")
        await _sync(provider)

        edited_track = await _library_track(library_mass, edited)
        assert edited_track is not None
        assert edited_track.item_id == library_track.item_id
        assert _mappings(edited_track) == both
        assert edited_track.metadata.description == "Edited"


class TestMergedItemRetaggedAsAnotherRecording:
    """A merged beets item retagged as a different recording splits off its own library track."""

    async def test_retagged_item_and_other_item_end_on_separate_tracks(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """Each beets item ends on its own library track with its own title."""
        recording = {"mb_trackid": str(uuid4()), "acoustid_id": str(uuid4()), "track": 1}
        first_album = _add_album(beets_db, "First Album")
        second_album = _add_album(beets_db, "Second Album")
        retagged = _add_item(beets_db, first_album, "Song", **recording)
        other = _add_item(beets_db, second_album, "Song", length=215.9, **recording)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        merged = await _library_track(library_mass, retagged)
        assert merged is not None
        assert _mappings(merged) == {
            (INSTANCE_ID, track_prov_id(retagged)),
            (INSTANCE_ID, track_prov_id(other)),
        }

        beets_db.update_item(
            retagged,
            title="Totally Different",
            mb_trackid=str(uuid4()),
            acoustid_id=str(uuid4()),
            isrc="TESTRETAG0001",
        )
        # the first sync splits the retagged item off, the second re-imports the other item
        await _sync(provider)
        await _sync(provider)

        retagged_track = await _library_track(library_mass, retagged)
        other_track = await _library_track(library_mass, other)
        assert retagged_track is not None
        assert other_track is not None
        assert retagged_track.item_id != other_track.item_id
        assert retagged_track.name == "Totally Different"
        assert other_track.name == "Song"
        assert _mappings(retagged_track) == {(INSTANCE_ID, track_prov_id(retagged))}
        assert _mappings(other_track) == {(INSTANCE_ID, track_prov_id(other))}


class TestMergedItemsChangedTogether:
    """Two merged beets items that change in the same sync both keep their current mapping."""

    async def test_both_mappings_hold_their_new_checksums(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """The library track maps both items afterwards, and a further sync imports nothing."""
        recording = {"mb_trackid": str(uuid4()), "acoustid_id": str(uuid4()), "track": 1}
        first_album = _add_album(beets_db, "First Album")
        second_album = _add_album(beets_db, "Second Album")
        first = _add_item(beets_db, first_album, "Song", **recording)
        second = _add_item(beets_db, second_album, "Song", length=215.9, **recording)
        beets_db.set_item_flex(first, "rating", "0.9")
        beets_db.set_item_flex(second, "rating", "0.9")
        await _sync(await _attach(make_provider, library_mass))

        provider = await _attach(make_provider, library_mass, favorite_rating_threshold=0.8)
        await _sync(provider)

        library_track = await _library_track(library_mass, first)
        assert library_track is not None
        assert _mappings(library_track) == {
            (INSTANCE_ID, track_prov_id(first)),
            (INSTANCE_ID, track_prov_id(second)),
        }
        tracks = library_mass.music.tracks
        with patch.object(tracks, "add_item_to_library", wraps=tracks.add_item_to_library) as add:
            await _sync(provider)
        add.assert_not_awaited()


class TestDeletedItem:
    """An item deleted from beets without a replacement leaves the library."""

    async def test_deleted_item_removes_library_track(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """The library track of the deleted item is removed and the other track stays."""
        album_id = beets_db.add_album(**album_fields())
        kept = _add_item(beets_db, album_id, "Kept", track=1)
        gone = _add_item(beets_db, album_id, "Gone", track=2)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        gone_track = await _library_track(library_mass, gone)
        assert gone_track is not None

        beets_db.delete_item(gone)
        await _sync(provider)

        assert await _library_track(library_mass, gone) is None
        with pytest.raises(MediaNotFoundError):
            await library_mass.music.tracks.get_library_item(gone_track.item_id)
        assert await _library_track(library_mass, kept) is not None


class TestDeletedAlbum:
    """An album removed from beets with all its items leaves the library with its artist."""

    async def test_deleted_album_removes_album_and_emptied_artist(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """The album, its tracks and its now-empty artist go, and the other album stays."""
        gone_artist = _artist_fields("Gone Artist", str(uuid4()))
        kept_artist = _artist_fields("Kept Artist", str(uuid4()))
        gone_album = _add_album(
            beets_db,
            "Gone Album",
            **_artist_fields("Gone Artist", gone_artist["mb_artistid"], "album"),
        )
        kept_album = _add_album(
            beets_db,
            "Kept Album",
            **_artist_fields("Kept Artist", kept_artist["mb_artistid"], "album"),
        )
        gone_items = [
            _add_item(beets_db, gone_album, f"Gone {track}", track=track, **gone_artist)
            for track in (1, 2)
        ]
        kept = _add_item(beets_db, kept_album, "Kept", track=1, **kept_artist)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        assert set(await _library_albums(library_mass)) == {"Gone Album", "Kept Album"}

        beets_db.delete_album(gone_album)
        with patch("music_assistant.providers.beets.report_current_task_failure") as report_failure:
            await _sync(provider)

        report_failure.assert_not_called()
        provider.logger.warning.assert_not_called()  # type: ignore[attr-defined]
        for item_id in gone_items:
            assert await _library_track(library_mass, item_id) is None
        assert set(await _library_albums(library_mass)) == {"Kept Album"}
        artists = await library_mass.music.artists.get_library_items_by_query()
        assert {artist.name for artist in artists} == {"Kept Artist"}
        assert await _library_track(library_mass, kept) is not None


class TestItemAndAlbumWithTheSameBeetsId:
    """A beets item and a beets album with the same number stay apart in the library."""

    async def test_track_lookup_and_deletion_ignore_the_album(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """A track's provider id finds only that track, and deleting the item removes only it."""
        album_one = _add_album(beets_db, "One")
        album_two = _add_album(beets_db, "Two")
        provider = await _attach(make_provider, library_mass)
        # one item per sync, so the library numbers deterministically: Alpha and its album Two
        # become library track 1 and album 1, Bravo and its album One track 2 and album 2
        _add_item(beets_db, album_two, "Alpha")
        await _sync(provider)
        bravo = _add_item(beets_db, album_one, "Bravo")
        await _sync(provider)
        assert bravo == album_two

        tracks = await _library_tracks(library_mass)
        albums = await _library_albums(library_mass)
        assert set(tracks) == {"Alpha", "Bravo"}
        # beets item Bravo and beets album Two share a number, while album Two's library id is
        # Alpha's library track id, so a lookup ignoring the media type finds Alpha
        assert albums["Two"].item_id != tracks["Bravo"].item_id
        assert albums["Two"].item_id == tracks["Alpha"].item_id

        for name, track in tracks.items():
            (prov_item_id,) = {
                mapping.item_id
                for mapping in track.provider_mappings
                if mapping.provider_instance == INSTANCE_ID
            }
            found = await library_mass.music.tracks.get_library_item_by_prov_id(
                prov_item_id, INSTANCE_ID
            )
            assert found is not None
            assert found.name == name

        beets_db.delete_item(bravo)
        await _sync(provider)

        remaining = await _library_tracks(library_mass)
        assert set(remaining) == {"Alpha"}
        assert remaining["Alpha"].item_id == tracks["Alpha"].item_id
        assert _mappings(remaining["Alpha"]) == _mappings(tracks["Alpha"])


class TestTwoBeetsLibraries:
    """Two beets libraries whose ids overlap keep their tracks and albums apart."""

    async def test_libraries_with_overlapping_ids_do_not_merge(
        self,
        library_mass: MusicAssistant,
        make_provider: MakeProvider,
        beets_db: BeetsDb,
        tmp_path: Path,
    ) -> None:
        """Every track stays its own library item, and deleting from one library leaves the other."""
        other_db = BeetsDb(tmp_path / "other.db")
        first_items: dict[str, int] = {}
        for library, db in (("A", beets_db), ("B", other_db)):
            artist = _artist_fields(f"{library} Artist", str(uuid4()))
            album_id = _add_album(
                db,
                f"{library} Album",
                **_artist_fields(f"{library} Artist", artist["mb_artistid"], "album"),
            )
            first_items[library] = _add_item(
                db, album_id, f"{library} One", isrc=f"TEST{library}0000001", track=1, **artist
            )
            _add_item(
                db, album_id, f"{library} Two", isrc=f"TEST{library}0000002", track=2, **artist
            )
        assert first_items == {"A": 1, "B": 1}
        library_a = await _attach(make_provider, library_mass, instance_id=LIBRARY_A)
        library_b = await _attach(
            make_provider, library_mass, instance_id=LIBRARY_B, db_path=other_db.path
        )
        await _sync(library_a)
        await _sync(library_b)

        tracks = await _library_tracks(library_mass)
        albums = await _library_albums(library_mass)
        assert set(tracks) == {"A One", "A Two", "B One", "B Two"}
        assert set(albums) == {"A Album", "B Album"}
        library_items: list[tuple[str, Track | Album]] = [*tracks.items(), *albums.items()]
        for name, item in library_items:
            expected_instance = LIBRARY_A if name.startswith("A ") else LIBRARY_B
            assert {instance for instance, _ in _mappings(item)} == {expected_instance}

        beets_db.delete_item(first_items["A"])
        await _sync(library_a)

        remaining = await _library_tracks(library_mass)
        assert set(remaining) == {"A Two", "B One", "B Two"}
        assert remaining["B One"].item_id == tracks["B One"].item_id
        assert _mappings(remaining["B One"]) == _mappings(tracks["B One"])
