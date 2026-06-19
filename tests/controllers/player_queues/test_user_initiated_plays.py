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

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _controller() -> PlayerQueuesController:
    """Create a bare controller instance without running __init__."""
    return PlayerQueuesController.__new__(PlayerQueuesController)


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
    queue = cast("PlayerQueue", Mock(enqueued_media_items=[explicit_track, album]))

    ctrl = _controller()
    # the directly-enqueued track was explicitly chosen
    assert ctrl._is_user_initiated_play(queue, explicit_track) is True
    # a track that only played as part of the enqueued album was not
    assert ctrl._is_user_initiated_play(queue, album_track) is False


async def test_mark_album_played_is_user_initiated() -> None:
    """Crediting an enqueued album records it as a user-initiated play."""
    ctrl = _controller()
    ctrl.logger = Mock()
    mark = AsyncMock()
    ctrl.mass = Mock()
    ctrl.mass.music.mark_item_played = mark
    ctrl.mass.music.resolve_library_artist_ids = AsyncMock(return_value=set())

    album = Album(
        item_id="a1",
        provider="library",
        name="A",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="t1", provider="library", name="T", provider_mappings=set())
    queue = cast("PlayerQueue", Mock(queue_id="q1", userid="u1"))

    await ctrl._mark_album_played(album, track, queue)

    assert mark.call_args.kwargs["user_initiated"] is True


async def test_resolve_artist_marks_user_initiated() -> None:
    """Enqueuing an artist records the artist itself as a user-initiated play."""
    ctrl = _controller()
    ctrl.mass = Mock()
    ctrl.mass.create_task = Mock(side_effect=lambda coro: coro)
    ctrl.mass.music.mark_item_played = Mock()
    ctrl.get_artist_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]

    artist = Artist(item_id="ar1", provider="library", name="Ar", provider_mappings=set())

    await ctrl._resolve_media_items(artist, userid="u1", queue_id="q1")

    call = ctrl.mass.music.mark_item_played.call_args
    assert call.kwargs["user_initiated"] is True
    assert call.args[0].media_type == MediaType.ARTIST
