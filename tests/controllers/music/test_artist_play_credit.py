"""
Tests for crediting artists and albums from track plays.

The integration tests use the ``mass`` fixture from ``tests/conftest.py`` which
creates a full MusicAssistant instance with a real SQLite database in a
temporary directory. The decision helper is covered by a plain unit test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock
from uuid import uuid4

from music_assistant_models.enums import AlbumType, MediaType
from music_assistant_models.media_items import Album, Artist, ItemMapping, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_ALBUMS, DB_TABLE_ARTISTS, DB_TABLE_PLAYLOG
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


async def _add_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal artist to the library and return the stored item."""
    return await mass.music.artists.add_item_to_library(
        Artist(item_id="0", provider="library", name=name, provider_mappings=_library_mapping())
    )


async def _add_track(mass: MusicAssistant, name: str, artists: list[Artist]) -> Track:
    """Add a minimal track with the given artists and return the (re-fetched) stored item."""
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            artists=UniqueList(artists),
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


async def _add_album(mass: MusicAssistant, name: str, artists: list[Artist]) -> Album:
    """Add a minimal album with the given album artists and return the (re-fetched) stored item."""
    added = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            album_type=AlbumType.ALBUM,
            artists=UniqueList(artists),
        )
    )
    return await mass.music.albums.get_library_item(added.item_id)


async def _play_count(mass: MusicAssistant, table: str, item_id: str) -> int:
    """Read the play_count for a library item directly from the database."""
    row = await mass.music.database.get_row(table, {"item_id": item_id})
    assert row is not None
    return int(row["play_count"])


async def _artist_user_initiated(mass: MusicAssistant, item_id: str, userid: str) -> int:
    """Read the user_initiated flag of an artist playlog row."""
    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {"media_type": MediaType.ARTIST.value, "item_id": item_id, "userid": userid},
    )
    assert row is not None
    return int(row["user_initiated"])


async def test_played_track_credits_all_track_artists(mass: MusicAssistant) -> None:
    """A fully played track credits every one of its artists (not just the primary)."""
    user = await mass.webserver.auth.create_user("trackcredit")
    primary = await _add_artist(mass, "Primary")
    featured = await _add_artist(mass, "Featured")
    track = await _add_track(mass, "Collab", [primary, featured])

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=True, userid=user.user_id
    )

    assert await _play_count(mass, DB_TABLE_ARTISTS, primary.item_id) == 1
    assert await _play_count(mass, DB_TABLE_ARTISTS, featured.item_id) == 1
    for artist in (primary, featured):
        rows = await mass.music.database.get_rows(
            DB_TABLE_PLAYLOG,
            {
                "media_type": MediaType.ARTIST.value,
                "item_id": artist.item_id,
                "userid": user.user_id,
            },
        )
        assert len(rows) == 1


async def test_album_play_credits_album_artists_with_dedup(mass: MusicAssistant) -> None:
    """Marking an album played credits its album artists, skipping any in skip_artist_ids."""
    kept = await _add_artist(mass, "Kept Artist")
    skipped = await _add_artist(mass, "Skipped Artist")
    album = await _add_album(mass, "An Album", [kept, skipped])

    await mass.music.mark_item_played(
        album, fully_played=True, user_initiated=True, skip_artist_ids=[skipped.item_id]
    )

    assert await _play_count(mass, DB_TABLE_ALBUMS, album.item_id) == 1
    assert await _play_count(mass, DB_TABLE_ARTISTS, kept.item_id) == 1
    assert await _play_count(mass, DB_TABLE_ARTISTS, skipped.item_id) == 0


async def test_track_credit_does_not_flag_artist_user_initiated(mass: MusicAssistant) -> None:
    """A track play credits its artist as a side-effect, never as user-initiated."""
    user = await mass.webserver.auth.create_user("trackcreditui")
    artist = await _add_artist(mass, "Sideeffect")
    track = await _add_track(mass, "A Song", [artist])

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=True, userid=user.user_id
    )

    assert await _artist_user_initiated(mass, artist.item_id, user.user_id) == 0


async def test_explicit_artist_play_survives_track_credit(mass: MusicAssistant) -> None:
    """An explicit artist play stays user-initiated after later side-effect credits."""
    user = await mass.webserver.auth.create_user("artiststicky")
    artist = await _add_artist(mass, "Chosen")
    track = await _add_track(mass, "Their Song", [artist])

    # user explicitly plays the artist
    await mass.music.mark_item_played(
        artist, fully_played=True, user_initiated=True, userid=user.user_id
    )
    assert await _artist_user_initiated(mass, artist.item_id, user.user_id) == 1

    # later, one of the artist's tracks auto-plays (not user-initiated)
    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=False, userid=user.user_id
    )
    assert await _artist_user_initiated(mass, artist.item_id, user.user_id) == 1


def test_enqueued_album_decision() -> None:
    """An album is credited only when enqueued and only on the first track of its run."""
    album_x = Album(
        item_id="ax",
        provider="library",
        name="X",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    album_y = Album(
        item_id="ay",
        provider="library",
        name="Y",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    t1 = Track(item_id="t1", provider="library", name="T1", provider_mappings=set(), album=album_x)
    t2 = Track(item_id="t2", provider="library", name="T2", provider_mappings=set(), album=album_x)
    t3 = Track(item_id="t3", provider="library", name="T3", provider_mappings=set(), album=album_y)
    items = [QueueItem.from_media_item("q1", track) for track in (t1, t2, t3)]

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = cast("PlayerQueue", Mock(queue_id="q1"))
    data = PlayerQueueData(queue=queue, items=items, enqueued_media_items=[album_x])
    tracker._queue_data = {"q1": data}

    # first track of the enqueued album -> credit it
    assert tracker._enqueued_album_for_track(data, items[0], t1) is album_x
    # second track shares the previous queue item's album -> already handled
    assert tracker._enqueued_album_for_track(data, items[1], t2) is None
    # a track whose album was never enqueued -> not credited
    assert tracker._enqueued_album_for_track(data, items[2], t3) is None


def test_enqueued_provider_album_credits_its_library_tracks() -> None:
    """An album picked from a provider listing is credited, though its tracks are library ones."""
    mapping = ProviderMapping(
        item_id="album-prov-1", provider_domain="spotify", provider_instance="spotify--abc"
    )
    # the album object a provider listing hands to play_media
    provider_album = Album(
        item_id="album-prov-1",
        provider="spotify--abc",
        name="Kind of Blue",
        provider_mappings={mapping},
        album_type=AlbumType.ALBUM,
    )
    # the album its tracks carry, because the album is also in the library
    library_album = Album(
        item_id="7",
        provider="library",
        name="Kind of Blue",
        provider_mappings={mapping},
        album_type=AlbumType.ALBUM,
    )
    other_album = Album(
        item_id="8",
        provider="library",
        name="Sketches of Spain",
        provider_mappings={
            ProviderMapping(
                item_id="album-prov-2",
                provider_domain="spotify",
                provider_instance="spotify--abc",
            )
        },
        album_type=AlbumType.ALBUM,
    )
    t1 = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    t2 = Track(item_id="t2", provider="library", name="T2", provider_mappings=set())
    t3 = Track(item_id="t3", provider="library", name="T3", provider_mappings=set())
    items = [QueueItem.from_media_item("q1", track) for track in (t1, t2, t3)]
    # loading an item for playback puts the full library album on it
    t1.album = library_album
    t2.album = library_album
    t3.album = other_album

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = cast("PlayerQueue", Mock(queue_id="q1"))
    data = PlayerQueueData(queue=queue, items=items, enqueued_media_items=[provider_album])
    tracker._queue_data = {"q1": data}

    # the enqueued album is credited on the first track of its run, under the library
    # identity so it shares the row an explicit library play writes
    assert tracker._enqueued_album_for_track(data, items[0], t1) is library_album
    # and only once for that run
    assert tracker._enqueued_album_for_track(data, items[1], t2) is None
    # a track from an album the user never enqueued is not credited
    assert tracker._enqueued_album_for_track(data, items[2], t3) is None


def test_album_outside_the_library_is_credited_as_the_provider_album() -> None:
    """An album that is not in the library is credited under the provider identity."""
    provider_album = Album(
        item_id="album-prov-1",
        provider="spotify--abc",
        name="Kind of Blue",
        provider_mappings={
            ProviderMapping(
                item_id="album-prov-1",
                provider_domain="spotify",
                provider_instance="spotify--abc",
            )
        },
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="t1", provider="spotify--abc", name="T1", provider_mappings=set())
    items = [QueueItem.from_media_item("q1", track)]
    # with no library album to swap in, the track keeps the provider album as a mapping
    track.album = ItemMapping.from_item(provider_album)

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = cast("PlayerQueue", Mock(queue_id="q1"))
    data = PlayerQueueData(queue=queue, items=items, enqueued_media_items=[provider_album])
    tracker._queue_data = {"q1": data}

    assert tracker._enqueued_album_for_track(data, items[0], track) is provider_album
