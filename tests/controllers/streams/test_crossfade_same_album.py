"""Tests for the crossfade guard that keeps tracks of one album gapless."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import CrossfadeMode, MediaType
from music_assistant_models.media_items import Album, ItemMapping, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin

from music_assistant.controllers.streams.audio import StreamsAudio

PROVIDER_ALBUM = ItemMapping(
    media_type=MediaType.ALBUM,
    item_id="album-prov-1",
    provider="spotify--abc",
    name="Kind of Blue",
)
OTHER_PROVIDER_ALBUM = ItemMapping(
    media_type=MediaType.ALBUM,
    item_id="album-prov-2",
    provider="spotify--abc",
    name="Sketches of Spain",
)
LIBRARY_ALBUM = Album(
    item_id="7",
    provider="library",
    name="Kind of Blue",
    provider_mappings={
        ProviderMapping(
            item_id="album-prov-1",
            provider_domain="spotify",
            provider_instance="spotify--abc",
        )
    },
)
# played from the library album, or from the folder that holds it
ALBUM_ORIGIN = QueueItemOrigin(container=ItemMapping.from_item(LIBRARY_ALBUM))
OTHER_ALBUM_ORIGIN = QueueItemOrigin(
    container=ItemMapping(
        media_type=MediaType.ALBUM, item_id="8", provider="library", name="Sketches of Spain"
    )
)
# the library numbers each media type from one, so a playlist can share the album's id
PLAYLIST_ORIGIN = QueueItemOrigin(
    container=ItemMapping(
        media_type=MediaType.PLAYLIST, item_id="7", provider="library", name="Jazz essentials"
    )
)


def _queue_item(
    item_id: str, album: Album | ItemMapping | None, origin: QueueItemOrigin | None = None
) -> QueueItem:
    """Build a queue item holding a track on the given album, played from the given origin."""
    return QueueItem(
        queue_id="queue-1",
        queue_item_id=item_id,
        name=item_id,
        duration=300,
        media_item=Track(
            item_id=item_id,
            provider="spotify--abc",
            name=item_id,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain="spotify",
                    provider_instance="spotify--abc",
                )
            },
            album=album,
        ),
        origin=origin,
    )


def _audio(next_item: QueueItem, allow_same_album: bool = False) -> StreamsAudio:
    """Build a StreamsAudio whose queue reports the given (unloaded) next item."""
    mass = MagicMock()
    mass.player_queues.get.return_value = MagicMock()
    mass.players.get_player.return_value = MagicMock()
    mass.player_queues.get_next_item.return_value = next_item
    mass.config.get_raw_core_config_value.return_value = allow_same_album
    return StreamsAudio(cast("Any", mass))


def _crossfade_allowed(current: QueueItem, next_item: QueueItem, **kwargs: Any) -> bool:
    """Run the guard the way the flow stream does: only the current item is loaded."""
    return _audio(next_item, **kwargs).crossfade_allowed(
        current,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        player_id="queue-1",
        flow_mode=True,
    )


def test_same_album_from_one_provider_is_not_crossfaded() -> None:
    """Two tracks that both still carry the provider album are recognised as one album."""
    assert not _crossfade_allowed(
        _queue_item("track-1", PROVIDER_ALBUM), _queue_item("track-2", PROVIDER_ALBUM)
    )


def test_library_album_matches_the_provider_album_of_an_unloaded_next_item() -> None:
    """
    A loaded item carries the library album while the next item still carries the provider one.

    Both describe the same album, so the boundary must stay gapless.
    """
    assert not _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM), _queue_item("track-2", PROVIDER_ALBUM)
    )


def test_different_albums_are_crossfaded() -> None:
    """A boundary between two genuinely different albums still gets its crossfade."""
    assert _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM), _queue_item("track-2", OTHER_PROVIDER_ALBUM)
    )


def test_same_album_is_crossfaded_when_the_user_allows_it() -> None:
    """The same-album guard stays overridable by the streams config option."""
    assert _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
        allow_same_album=True,
    )


def test_tracks_played_from_one_album_are_not_crossfaded() -> None:
    """Tracks played from one album, or its folder, stay gapless whatever album they carry."""
    assert not _crossfade_allowed(
        _queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", None, origin=ALBUM_ORIGIN),
    )


def test_tracks_played_from_different_albums_are_crossfaded() -> None:
    """The boundary between two album plays still gets its crossfade."""
    assert _crossfade_allowed(
        _queue_item("track-1", None, origin=ALBUM_ORIGIN),
        _queue_item("track-2", None, origin=OTHER_ALBUM_ORIGIN),
    )


def test_album_and_playlist_sharing_a_library_id_are_crossfaded() -> None:
    """An album play followed by a playlist play is crossfaded, even when their ids match."""
    assert _crossfade_allowed(
        _queue_item("track-1", None, origin=ALBUM_ORIGIN),
        _queue_item("track-2", None, origin=PLAYLIST_ORIGIN),
    )


def test_tracks_of_one_album_in_a_playlist_are_not_crossfaded() -> None:
    """A playlist that plays an album's tracks in a row keeps them gapless."""
    assert not _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM, origin=PLAYLIST_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM, origin=PLAYLIST_ORIGIN),
    )


def test_playlist_moving_on_to_another_album_is_crossfaded() -> None:
    """Only an album play keeps its tracks gapless, not any container that holds them."""
    assert _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM, origin=PLAYLIST_ORIGIN),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM, origin=PLAYLIST_ORIGIN),
    )


def test_item_without_an_origin_is_compared_on_its_album() -> None:
    """A track played on its own after the album it belongs to stays gapless."""
    assert not _crossfade_allowed(
        _queue_item("track-1", LIBRARY_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM),
    )


def test_album_play_is_crossfaded_when_the_user_allows_it() -> None:
    """The streams config option also overrides the guard for tracks played from one album."""
    assert _crossfade_allowed(
        _queue_item("track-1", None, origin=ALBUM_ORIGIN),
        _queue_item("track-2", None, origin=ALBUM_ORIGIN),
        allow_same_album=True,
    )
