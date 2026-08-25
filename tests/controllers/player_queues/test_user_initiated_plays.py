"""
Tests for tagging explicit (user-initiated) plays in the playlog.

These cover the player-queue side of the decision: which plays count as
user-initiated so they surface in the Discover "Recently played" row. The pure
decisions are exercised as plain unit tests against a bare controller instance,
mirroring ``test_play_report_dedup`` and ``test_enqueued_album_decision``.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import AlbumType, MediaType, QueueOption
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
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


def _resolver() -> tuple[MediaResolver, Mock]:
    """Create a bare resolver plus the Mock recording its ``mark_item_played`` calls."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = Mock()
    resolver.mass.create_task = Mock(side_effect=lambda coro: coro)
    mark = Mock()
    resolver.mass.music.mark_item_played = mark
    return resolver, mark


# the containers that are credited at resolve time, with the resolver method each one
# expands through: (container, resolve method name, expected credited media type)
_CONTAINERS = [
    (
        Playlist(item_id="pl1", provider="library", name="Pl", provider_mappings=set()),
        "get_playlist_tracks",
        MediaType.PLAYLIST,
    ),
    (
        Artist(item_id="ar1", provider="library", name="Ar", provider_mappings=set()),
        "get_artist_tracks",
        MediaType.ARTIST,
    ),
    (
        Genre(item_id="g1", provider="library", name="G", provider_mappings=set()),
        "get_genre_tracks",
        MediaType.GENRE,
    ),
    (
        Podcast(item_id="pc1", provider="library", name="Pc", provider_mappings=set()),
        "get_next_podcast_episodes",
        MediaType.PODCAST,
    ),
]


@pytest.mark.parametrize(("container", "resolve_method", "media_type"), _CONTAINERS)
async def test_resolve_container_marks_user_initiated(
    container: MediaItemType, resolve_method: str, media_type: MediaType
) -> None:
    """Enqueuing a container records the container itself as a user-initiated play."""
    resolver, mark = _resolver()
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    setattr(resolver, resolve_method, AsyncMock(return_value=[track]))

    await resolver._resolve_media_items(container, userid="u1", queue_id="q1")

    assert mark.call_args.kwargs["user_initiated"] is True
    assert mark.call_args.args[0].media_type == media_type


@pytest.mark.parametrize(
    ("container", "resolve_method"), [(item, method) for item, method, _ in _CONTAINERS]
)
async def test_resolve_empty_container_is_not_marked_played(
    container: MediaItemType, resolve_method: str
) -> None:
    """A container that resolves to nothing playable is never written to the play history."""
    resolver, mark = _resolver()
    setattr(resolver, resolve_method, AsyncMock(return_value=[]))

    assert await resolver._resolve_media_items(container, userid="u1", queue_id="q1") == []

    mark.assert_not_called()


async def test_enqueued_item_mapping_counts_as_user_initiated() -> None:
    """
    A play requested as an ItemMapping is recorded as explicitly enqueued.

    Every mapping-shaped surface -- the "Recently played" row itself, search results,
    artist/album track listings -- would otherwise never count as a user-initiated play.
    """
    track = Track(
        item_id="t1",
        provider="library",
        name="T1",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="library", provider_instance="library")
        },
    )
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.music.get_item_by_uri = AsyncMock(return_value=track)
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl._enqueue_with_option = AsyncMock()  # type: ignore[method-assign]
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(return_value=[track])
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}

    # the user pressed play on a row that hands back a lightweight mapping, not a full Track
    mapping = ItemMapping(
        item_id="t1", provider="library", name="T1", media_type=MediaType.TRACK, uri=track.uri
    )
    await ctrl._handle_play_media("q1", mapping, QueueOption.PLAY)

    # the mapping was resolved to its full item, so the play is attributable to the user
    enqueued = ctrl._queue_data["q1"].enqueued_media_items
    assert enqueued == [track]
    assert ctrl._is_user_initiated_play(ctrl._queue_data["q1"], track) is True


def test_track_picked_from_a_provider_listing_is_user_initiated() -> None:
    """A track picked from a provider listing counts as explicit once it resolves to the library."""
    mapping = ProviderMapping(
        item_id="track-prov-1", provider_domain="spotify", provider_instance="spotify--abc"
    )
    # the track object a provider listing hands to play_media
    provider_track = Track(
        item_id="track-prov-1",
        provider="spotify--abc",
        name="T",
        provider_mappings={mapping},
    )
    # the same track as it is reported once loaded for playback
    library_track = Track(item_id="12", provider="library", name="T", provider_mappings={mapping})
    data = cast("PlayerQueueData", Mock(enqueued_media_items=[provider_track]))

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._is_user_initiated_play(data, library_track) is True


def test_library_ids_are_not_matched_across_media_types() -> None:
    """The library numbers each media type from one, so an album id never matches a track id."""
    album = Album(
        item_id="7",
        provider="library",
        name="A",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="7", provider="library", name="T", provider_mappings=set())
    data = cast("PlayerQueueData", Mock(enqueued_media_items=[album]))

    tracker = PlayerQueuesController.__new__(PlayerQueuesController)
    assert tracker._is_user_initiated_play(data, track) is False


def _play_media_controller(
    media_item: MediaItemType, default_option: str
) -> PlayerQueuesController:
    """Build a bare controller whose play_media resolves its enqueue option from the config."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.music.get_item_by_uri = AsyncMock(return_value=media_item)
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.get_config_value = Mock(return_value=default_option)  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl._apply_shuffle = AsyncMock()  # type: ignore[method-assign]
    ctrl._enqueue_with_option = AsyncMock()  # type: ignore[method-assign]
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(return_value=[media_item])
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


async def test_configured_add_default_keeps_the_previously_enqueued_items() -> None:
    """A caller that leaves the enqueue option to the config still gets an add treated as one."""
    album = Album(item_id="a1", provider="library", name="A1", provider_mappings=set())
    track = Track(
        item_id="t1",
        provider="library",
        name="T1",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="library", provider_instance="library")
        },
    )
    ctrl = _play_media_controller(track, QueueOption.ADD.value)
    ctrl._queue_data["q1"].enqueued_media_items.append(album)

    await ctrl._handle_play_media("q1", track)

    # the album the queue is playing is what says its tracks belong together, so an add
    # must not drop it the way starting a new queue does
    assert ctrl._queue_data["q1"].enqueued_media_items == [album, track]


async def test_configured_replace_default_clears_the_previously_enqueued_items() -> None:
    """A config default that starts a new queue drops the parents of the previous one."""
    album = Album(item_id="a1", provider="library", name="A1", provider_mappings=set())
    track = Track(
        item_id="t1",
        provider="library",
        name="T1",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="library", provider_instance="library")
        },
    )
    ctrl = _play_media_controller(track, QueueOption.REPLACE.value)
    ctrl._queue_data["q1"].enqueued_media_items.append(album)
    ctrl._queue_data["q1"].credited_albums.add(album)

    await ctrl._handle_play_media("q1", track)

    assert ctrl._queue_data["q1"].enqueued_media_items == [track]
    # the credits only mark which enqueued albums were counted, so they go with them
    assert ctrl._queue_data["q1"].credited_albums == set()
