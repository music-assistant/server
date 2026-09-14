"""Sync tests for the beets provider against a real Music Assistant library database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from tests.providers.beets.conftest import INSTANCE_ID

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

MakeProvider = Callable[..., Awaitable[BeetsProvider]]

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
) -> BeetsProvider:
    """
    Return a beets provider around the test database, loaded into the real Music Assistant.

    Attaching a new provider replaces the previous one, the way a config change reloads it.

    :param make_provider: The beets provider factory fixture.
    :param mass: The database-only Music Assistant.
    :param favorite_rating_threshold: The favorite threshold the provider is configured with.
    """
    provider = await make_provider(favorite_rating_threshold=favorite_rating_threshold)
    provider.mass = mass
    # make_provider skips Provider.__init__, which sets `available`; the library controller
    # finds providers through mass.get_provider, which only returns available ones
    provider.available = True
    mass._providers = {INSTANCE_ID: provider}
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


async def _library_track(mass: MusicAssistant, beets_item_id: int) -> Track | None:
    """Return the library track that maps a beets item of the test instance."""
    assert mass.music.database
    # get_library_item_by_prov_id also matches album mappings with the same provider item id,
    # and beets numbers items and albums independently, so read the track mapping itself
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} WHERE media_type = 'track' "
        "AND provider_instance = :instance_id AND provider_item_id = :item_id",
        {"instance_id": INSTANCE_ID, "item_id": str(beets_item_id)},
        limit=0,
    )
    if not rows:
        return None
    return await mass.music.tracks.get_library_item(rows[0]["item_id"])


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

        assert [call.args[0].item_id for call in add.await_args_list] == [str(edited)]
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

        assert [call.args[0].item_id for call in add.await_args_list] == [str(rated)]
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
        } == {str(new_id)}


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
