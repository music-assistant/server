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
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PLAY_HISTORY,
)
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


def _queue_data(items: list[QueueItem]) -> PlayerQueueData:
    """Build the queue record the album-credit decision reads (only its items matter)."""
    return PlayerQueueData(queue=cast("PlayerQueue", Mock(queue_id="q1")), items=list(items))


def _library_album(item_id: str, name: str) -> Album:
    """Build a minimal library album."""
    return Album(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )


def _track(item_id: str, album: Album | ItemMapping) -> Track:
    """Build a minimal library track carrying the given album."""
    return Track(
        item_id=item_id, provider="library", name=item_id, provider_mappings=set(), album=album
    )


def _played(track: Track, container: MediaItemType | None = None) -> QueueItem:
    """Build the queue item of a track played from the given container, or on its own."""
    return QueueItem(
        queue_id="q1",
        queue_item_id=uuid4().hex,
        name=track.name,
        duration=None,
        media_item=track,
        origin=QueueItemOrigin(container=ItemMapping.from_item(container)) if container else None,
    )


async def _play_count(mass: MusicAssistant, table: str, item_id: str) -> int:
    """Read the play_count for a library item directly from the database."""
    row = await mass.music.database.get_row(table, {"item_id": item_id})
    assert row is not None
    return int(row["play_count"])


async def _artist_user_initiated(mass: MusicAssistant, item_id: str, userid: str) -> int:
    """Read the user_initiated flag of an artist playlog row."""
    row = await mass.music.database.get_row(
        DB_TABLE_MEDIA_PROGRESS,
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
        track, fully_played=True, queue_id="q1", user_initiated=True, userid=user.user_id
    )

    assert await _play_count(mass, DB_TABLE_ARTISTS, primary.item_id) == 1
    assert await _play_count(mass, DB_TABLE_ARTISTS, featured.item_id) == 1
    for artist in (primary, featured):
        rows = await mass.music.database.get_rows(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "media_type": MediaType.ARTIST.value,
                "item_id": artist.item_id,
                "userid": user.user_id,
            },
        )
        assert len(rows) == 1


async def test_album_play_credits_album_artists_with_dedup(mass: MusicAssistant) -> None:
    """Marking an album played credits its album artists, skipping any in skip_artist_ids."""
    user = await mass.webserver.auth.create_user("albumcreditdedup")
    kept = await _add_artist(mass, "Kept Artist")
    skipped = await _add_artist(mass, "Skipped Artist")
    album = await _add_album(mass, "An Album", [kept, skipped])

    await mass.music.mark_item_played(
        album,
        fully_played=True,
        queue_id="q1",
        user_initiated=True,
        skip_artist_ids=[skipped.item_id],
        userid=user.user_id,
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
        track, fully_played=True, queue_id="q1", user_initiated=True, userid=user.user_id
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
        track,
        fully_played=True,
        queue_id="q1",
        user_initiated=False,
        userid=user.user_id,
    )
    assert await _artist_user_initiated(mass, artist.item_id, user.user_id) == 1


async def test_track_and_album_play_write_separate_history_media_types(
    mass: MusicAssistant,
) -> None:
    """A completed album credit creates its own album event beside the track event."""
    user = await mass.webserver.auth.create_user("trackalbumhistory")
    artist = await _add_artist(mass, "History Artist")
    track = await _add_track(mass, "History Track", [artist])
    album = await _add_album(mass, "History Album", [artist])

    await mass.music.mark_item_played(
        track,
        fully_played=True,
        queue_id="history-queue",
        user_initiated=True,
        userid=user.user_id,
    )
    await mass.music.mark_item_played(
        album,
        fully_played=True,
        queue_id="history-queue",
        user_initiated=True,
        skip_artist_ids=[artist.item_id],
        userid=user.user_id,
    )

    rows = await mass.music.database.get_rows(DB_TABLE_PLAY_HISTORY, {"userid": user.user_id})
    assert {(row["media_type"], row["item_id"]) for row in rows} == {
        (MediaType.TRACK.value, track.item_id),
        (MediaType.ALBUM.value, album.item_id),
    }


def test_album_play_is_credited_once() -> None:
    """The first completed track played from an album credits it, the later ones do not."""
    album_x = _library_album("ax", "X")
    album_y = _library_album("ay", "Y")
    x1 = _played(_track("x1", album_x), album_x)
    x2 = _played(_track("x2", album_x), album_x)
    y1 = _played(_track("y1", album_y), album_y)
    data = _queue_data([x1, x2, y1])

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._claim_album_credit(data, x1) is album_x
    # crediting another album in between keeps the credit of one still on the queue
    assert tracker._claim_album_credit(data, y1) is album_y
    assert tracker._claim_album_credit(data, x2) is None


def test_album_tracks_not_played_from_the_album_credit_nothing() -> None:
    """A track of an album that came from a playlist, or from autoplay, does not credit it."""
    album = _library_album("ax", "X")
    playlist = Playlist(item_id="pl1", provider="library", name="Mix", provider_mappings=set())
    from_playlist = _played(_track("x1", album), playlist)
    from_autoplay = _played(_track("x2", album))
    data = _queue_data([from_playlist, from_autoplay])

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._claim_album_credit(data, from_playlist) is None
    assert tracker._claim_album_credit(data, from_autoplay) is None
    assert data.credited_albums == set()


def test_album_play_credited_once_however_its_tracks_are_ordered() -> None:
    """Album tracks split up or reordered in the queue still credit the album exactly once."""
    album_x = _library_album("ax", "X")
    x_items = [_played(_track(f"x{i}", album_x), album_x) for i in range(4)]
    # a track of another album the user picked to play next, so played on its own
    play_next = _played(_track("y1", _library_album("ay", "Y")))

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    # 'play next' on a track from another album splits the album's run in two
    played = [x_items[0], x_items[1], play_next, x_items[2], x_items[3]]
    data = _queue_data(played)
    credited = [tracker._claim_album_credit(data, item) for item in played]
    assert [c for c in credited if c is not None] == [album_x]

    # the album's first track is skipped, so it never completes and the claim arrives from a
    # later track that sits behind a same-album predecessor; the album is still credited once
    data = _queue_data(x_items)
    credited = [tracker._claim_album_credit(data, item) for item in x_items[1:]]
    assert [c for c in credited if c is not None] == [album_x]


def test_provider_album_play_credits_the_library_album_its_tracks_carry() -> None:
    """An album played from a provider listing is credited as the library album."""
    mapping = ProviderMapping(
        item_id="album-prov-1", provider_domain="spotify", provider_instance="spotify--abc"
    )
    provider_album = Album(
        item_id="album-prov-1",
        provider="spotify--abc",
        name="Kind of Blue",
        provider_mappings={mapping},
        album_type=AlbumType.ALBUM,
    )
    library_album = Album(
        item_id="7",
        provider="library",
        name="Kind of Blue",
        provider_mappings={mapping},
        album_type=AlbumType.ALBUM,
    )
    # loading an item for playback puts the full library album on it
    first = _played(_track("t1", library_album), provider_album)
    second = _played(_track("t2", library_album), provider_album)
    data = _queue_data([first, second])

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    # under the library identity, so it shares the row an explicit library play writes
    assert tracker._claim_album_credit(data, first) is library_album
    assert tracker._claim_album_credit(data, second) is None


def test_track_carrying_another_album_credits_the_album_it_was_played_from() -> None:
    """The album a track was played from is credited, not another album the track carries."""
    played_album = _library_album("7", "Kind of Blue")
    item = _played(_track("t1", _library_album("8", "Sketches of Spain")), played_album)
    data = _queue_data([item])

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._claim_album_credit(data, item) == ItemMapping.from_item(played_album)


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
    # with no library album to swap in, the track keeps the provider album as a mapping
    track = _track("t1", ItemMapping.from_item(provider_album))
    item = _played(track, provider_album)
    data = _queue_data([item])

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._claim_album_credit(data, item) == ItemMapping.from_item(provider_album)


def test_credits_of_albums_that_left_the_queue_are_dropped() -> None:
    """Claiming a credit forgets the credits of albums with no tracks left on the queue."""
    album_x = _library_album("ax", "X")
    album_y = _library_album("ay", "Y")
    y1 = _played(_track("y1", album_y), album_y)
    data = _queue_data([y1])
    data.credited_albums.add(cast("str", album_x.uri))

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._claim_album_credit(data, y1) is album_y
    assert data.credited_albums == {album_y.uri}
