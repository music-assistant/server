"""
Tests for tagging explicit (user-initiated) plays in the playlog.

These cover the player-queue side of the decision: which plays count as
user-initiated so they surface in the Discover "Recently played" row. The pure
decisions are exercised as plain unit tests against a bare controller instance,
mirroring ``test_play_report_dedup`` and ``test_enqueued_album_decision``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import AlbumType, MediaType
from music_assistant_models.media_items import Album, Artist, Track

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver

if TYPE_CHECKING:
    from music_assistant.controllers.player_queues.state import PlayerQueueData


def test_directly_enqueued_track_is_user_initiated() -> None:
    """A track the user pressed play on is user-initiated; an album track is not."""
    album = Album(
        item_id="ax",
        provider="library",
        name="X",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    explicit_track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    album_track = Track(
        item_id="t2", provider="library", name="T2", provider_mappings=set(), album=album
    )
    # the user explicitly played a single track and (separately) an album
    data = cast("PlayerQueueData", Mock(enqueued_media_items=[explicit_track, album]))

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    # the directly-enqueued track was explicitly chosen
    assert tracker._is_user_initiated_play(data, explicit_track) is True
    # a track that only played as part of the enqueued album was not
    assert tracker._is_user_initiated_play(data, album_track) is False


async def test_mark_album_played_is_user_initiated() -> None:
    """Crediting an enqueued album records it as a user-initiated play."""
    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    tracker.logger = Mock()
    mark = AsyncMock()
    tracker.mass = Mock()
    tracker.mass.music.mark_item_played = mark
    tracker.mass.music.resolve_library_artist_ids = AsyncMock(return_value=set())

    album = Album(
        item_id="a1",
        provider="library",
        name="A",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="t1", provider="library", name="T", provider_mappings=set())
    data = cast("PlayerQueueData", Mock(userid="u1", queue=Mock(queue_id="q1")))

    await tracker._mark_album_played(album, track, data)

    assert mark.call_args.kwargs["user_initiated"] is True


async def test_resolve_artist_marks_user_initiated() -> None:
    """Enqueuing an artist records the artist itself as a user-initiated play."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = Mock()
    resolver.mass.create_task = Mock(side_effect=lambda coro: coro)
    resolver.mass.music.mark_item_played = Mock()
    resolver.get_artist_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]

    artist = Artist(item_id="ar1", provider="library", name="Ar", provider_mappings=set())

    await resolver._resolve_media_items(artist, userid="u1", queue_id="q1")

    call = resolver.mass.music.mark_item_played.call_args
    assert call.kwargs["user_initiated"] is True
    assert call.args[0].media_type == MediaType.ARTIST
