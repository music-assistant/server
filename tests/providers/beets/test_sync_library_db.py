"""Sync tests for the beets provider against a real Music Assistant library database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping

from music_assistant.constants import DB_TABLE_FAVORITES, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.mass import MusicAssistant
from music_assistant.providers.beets import BeetsProvider
from tests.providers.beets.beets_db import BeetsDb, album_fields, item_fields
from tests.providers.beets.conftest import INSTANCE_ID, track_prov_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track

MakeProvider = Callable[..., Awaitable[BeetsProvider]]
LIBRARY_A = "beets--libA"
LIBRARY_B = "beets--libB"
OTHER_INSTANCE = "other--instance"
USER_ID = "listener"

# the sync's TaskManager runs its imports through mass.create_task, which pins tasks to
# mass.loop, so each test must run on the loop its class-scoped Music Assistant was created on
pytestmark = pytest.mark.asyncio(loop_scope="class")


@pytest.fixture(scope="class")
def library_mass(music_mass_class: MusicAssistant) -> MusicAssistant:
    """Return a database-only Music Assistant with an empty library for each test class."""
    # imports store ReplayGain loudness and removals clean up audio analysis, both through
    # the audio analysis controller this database-only instance does not run
    music_mass_class.streams = MagicMock()
    music_mass_class.streams.audio_analysis.set_track_loudness = AsyncMock()
    music_mass_class.streams.audio_analysis.delete_audio_analysis = AsyncMock()
    # setting a favorite clears cached search results, and this instance sets up no cache
    music_mass_class.cache.delete = AsyncMock()  # type: ignore[method-assign]
    return music_mass_class


async def _attach(
    make_provider: MakeProvider,
    mass: MusicAssistant,
    instance_id: str = INSTANCE_ID,
    db_path: Path | None = None,
) -> BeetsProvider:
    """
    Return a beets provider around a test database, loaded into the real Music Assistant.

    Attaching a provider with the instance id of an attached one replaces it, the way a config
    change reloads it.

    :param make_provider: The beets provider factory fixture.
    :param mass: The database-only Music Assistant.
    :param instance_id: The provider instance id.
    :param db_path: The beets database to read, when not the default test database.
    """
    provider = await make_provider(instance_id=instance_id, db_path=db_path)
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


async def _is_favorite(mass: MusicAssistant, track: Track) -> bool:
    """Return whether the test user likes a library track."""
    assert mass.music.database
    rows = await mass.music.database.get_rows(
        DB_TABLE_FAVORITES,
        {"media_type": MediaType.TRACK.value, "item_id": int(track.item_id), "user_id": USER_ID},
    )
    return any(row["favorite"] for row in rows)


def _other_mapping(item_id: str) -> ProviderMapping:
    """Return a mapping of another music provider, as a saved album or followed artist has."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="other",
        provider_instance=OTHER_INSTANCE,
        in_library=True,
    )


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


class TestReimportedItem:
    """A beets re-import that gives an item a new id keeps the library track it merges into."""

    async def test_reimported_item_keeps_library_track(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """The track keeps its id and favorite and its beets mapping moves to the new id."""
        album_id = beets_db.add_album(**album_fields())
        recording = {"mb_trackid": str(uuid4()), "acoustid_id": str(uuid4()), "track": 1}
        old_id = _add_item(beets_db, album_id, "Song", **recording)
        # a later row keeps sqlite from handing the re-imported item the deleted item's id
        _add_item(beets_db, album_id, "Other", track=2)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        library_track = await _library_track(library_mass, old_id)
        assert library_track is not None
        await library_mass.music.tracks.set_favorite(library_track.item_id, True, [USER_ID])

        beets_db.delete_item(old_id)
        new_id = _add_item(beets_db, album_id, "Song", **recording)
        assert new_id != old_id
        await _sync(provider)

        assert await _library_track(library_mass, old_id) is None
        reimported = await _library_track(library_mass, new_id)
        assert reimported is not None
        assert reimported.item_id == library_track.item_id
        assert await _is_favorite(library_mass, reimported)
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
        """Each beets item ends on its own library track with its own title in one sync."""
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
        await library_mass.music.tracks.set_favorite(merged.item_id, True, [USER_ID])

        beets_db.update_item(
            retagged,
            title="Totally Different",
            mb_trackid=str(uuid4()),
            acoustid_id=str(uuid4()),
            isrc="TESTRETAG0001",
        )
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
        # the library track, with its favorite, stays with the item that did not change
        assert other_track.item_id == merged.item_id
        assert await _is_favorite(library_mass, other_track)
        assert not await _is_favorite(library_mass, retagged_track)


class TestMergedItemRematchedKeepingItsFingerprint:
    """A merged beets item re-matched to another recording splits off despite its AcoustID."""

    async def test_rematched_item_and_other_item_end_on_separate_tracks(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """A new MusicBrainz recording and ISRC outweigh the unchanged audio fingerprint."""
        recording = {
            "mb_trackid": str(uuid4()),
            "acoustid_id": str(uuid4()),
            "isrc": "TESTSHARED001",
            "track": 1,
        }
        first_album = _add_album(beets_db, "First Album")
        second_album = _add_album(beets_db, "Second Album")
        rematched = _add_item(beets_db, first_album, "Song", **recording)
        other = _add_item(beets_db, second_album, "Song", length=215.9, **recording)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        merged = await _library_track(library_mass, rematched)
        assert merged is not None
        assert _mappings(merged) == {
            (INSTANCE_ID, track_prov_id(rematched)),
            (INSTANCE_ID, track_prov_id(other)),
        }
        await library_mass.music.tracks.set_favorite(merged.item_id, True, [USER_ID])

        beets_db.update_item(
            rematched, title="Totally Different", mb_trackid=str(uuid4()), isrc="TESTREMATCH01"
        )
        await _sync(provider)

        rematched_track = await _library_track(library_mass, rematched)
        other_track = await _library_track(library_mass, other)
        assert rematched_track is not None
        assert other_track is not None
        assert rematched_track.item_id != other_track.item_id
        assert rematched_track.name == "Totally Different"
        assert other_track.name == "Song"
        assert _mappings(rematched_track) == {(INSTANCE_ID, track_prov_id(rematched))}
        assert _mappings(other_track) == {(INSTANCE_ID, track_prov_id(other))}
        # the library track, with its favorite, stays with the item that did not change
        assert other_track.item_id == merged.item_id
        assert await _is_favorite(library_mass, other_track)
        assert not await _is_favorite(library_mass, rematched_track)


class TestMergedItemsWithoutExternalIds:
    """Beets items merged on name, artist and duration alone stay merged when one is edited."""

    async def test_edit_keeps_the_other_items_mapping(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """After a comment edit to one singleton, the library track still maps both items."""
        singleton = {
            "mb_trackid": None,
            "acoustid_id": None,
            "isrc": None,
            "track": 0,
            "disc": 0,
            "length": 215.0,
        }
        edited = _add_item(beets_db, None, "Song", path=b"Singles/Song.flac", **singleton)
        other = _add_item(beets_db, None, "Song", path=b"Singles/Song (copy).flac", **singleton)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        library_track = await _library_track(library_mass, edited)
        assert library_track is not None
        assert not library_track.external_ids
        both = {(INSTANCE_ID, track_prov_id(edited)), (INSTANCE_ID, track_prov_id(other))}
        assert _mappings(library_track) == both

        beets_db.update_item(edited, comments="Edited")
        await _sync(provider)

        edited_track = await _library_track(library_mass, edited)
        assert edited_track is not None
        assert edited_track.item_id == library_track.item_id
        assert _mappings(edited_track) == both
        assert edited_track.metadata.description == "Edited"


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
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)

        beets_db.set_item_flex(first, "mood", "happy")
        beets_db.set_item_flex(second, "mood", "happy")
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


class TestDeletedAlbumAlsoOnAnotherProvider:
    """An album and artist another provider also maps survive their beets album's deletion."""

    async def test_other_providers_mappings_keep_album_and_artist(
        self, library_mass: MusicAssistant, make_provider: MakeProvider, beets_db: BeetsDb
    ) -> None:
        """Only the beets mappings go; the saved album and followed artist stay in the library."""
        artist = _artist_fields("Shared Artist", str(uuid4()))
        album_id = _add_album(
            beets_db,
            "Shared Album",
            **_artist_fields("Shared Artist", artist["mb_artistid"], "album"),
        )
        item_id = _add_item(beets_db, album_id, "Song", track=1, **artist)
        # a later album keeps the library from looking emptied, which aborts the sync
        _add_item(beets_db, _add_album(beets_db, "Other Album"), "Other", track=1)
        provider = await _attach(make_provider, library_mass)
        await _sync(provider)
        library_album = (await _library_albums(library_mass))["Shared Album"]
        library_artist = next(
            artist
            for artist in await library_mass.music.artists.get_library_items_by_query()
            if artist.name == "Shared Artist"
        )
        await library_mass.music.albums.add_provider_mapping(
            library_album.item_id, _other_mapping("other-album")
        )
        await library_mass.music.artists.add_provider_mapping(
            library_artist.item_id, _other_mapping("other-artist")
        )

        beets_db.delete_album(album_id)
        await _sync(provider)

        assert await _library_track(library_mass, item_id) is None
        album = await library_mass.music.albums.get_library_item(library_album.item_id)
        artist_item = await library_mass.music.artists.get_library_item(library_artist.item_id)
        assert _mappings(album) == {(OTHER_INSTANCE, "other-album")}
        assert {
            (mapping.provider_instance, mapping.item_id)
            for mapping in artist_item.provider_mappings
        } == {(OTHER_INSTANCE, "other-artist")}


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
