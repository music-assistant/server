"""Tests that a track the queue's user disliked never lands in generated playback."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.controllers.player_queues.autoplay import AutoplayMode
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.queue_loader import QueueLoaderMixin

MODULE = "music_assistant.controllers.player_queues.queue_loader"
QUEUE_ID = "q1"
USER_ID = "user-a"
PROV = "spotify--1"
DISLIKED = "disliked"
LIKED = "liked"


def _track(item_id: str) -> Track:
    """Build a provider track with a single mapping."""
    return Track(
        item_id=item_id,
        provider=PROV,
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="spotify", provider_instance=PROV)
        },
    )


def _loader(*, userid: str | None) -> Any:
    """Build a queue loader stand-in whose user disliked the DISLIKED track."""
    loader = MagicMock()
    queue = SimpleNamespace(
        queue_id=QUEUE_ID,
        display_name="Queue",
        autoplay_enabled=True,
        current_index=None,
        index_in_buffer=None,
        items=0,
    )
    loader.get = MagicMock(return_value=queue)
    loader._queue_data = {
        QUEUE_ID: SimpleNamespace(
            queue=queue,
            items=[],
            enqueued_media_items=[_track("seed")],
            source_items=[],
            userid=userid,
        )
    }
    loader.load = AsyncMock()
    loader.mass.webserver.auth.get_user = AsyncMock()
    loader.mass.music.favorites.disliked_track_keys = AsyncMock(
        return_value=(set(), {(PROV, DISLIKED)})
    )
    return loader


def _appended(loader: Any) -> list[str]:
    """Return the item ids of the tracks the loader appended to the queue."""
    return [x.media_item.item_id for x in loader.load.await_args.args[1]]


def _pool() -> tuple[ManagedPool, Any]:
    """Build a managed pool over a queue stand-in whose user disliked the DISLIKED track."""
    queues = MagicMock()
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track(DISLIKED), _track(LIKED)])
    queues.mass.music.favorites.disliked_track_keys = AsyncMock(
        return_value=(set(), {(PROV, DISLIKED)})
    )
    return ManagedPool(queues), queues


async def test_a_dynamic_batch_of_the_pool_skips_a_disliked_track() -> None:
    """The batch a station or mix hands the pool, first or later, has no disliked track."""
    pool, _ = _pool()

    tracks = await pool._fetch_dynamic(MagicMock(), USER_ID)

    assert [x.item_id for x in tracks] == [LIKED]


async def test_a_dynamic_batch_of_only_disliked_tracks_is_empty() -> None:
    """The gate is hard: rather than play a dislike, the batch is empty."""
    pool, queues = _pool()
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track(DISLIKED)])

    assert await pool._fetch_dynamic(MagicMock(), USER_ID) == []


async def test_a_track_the_user_added_to_a_dynamic_queue_is_kept() -> None:
    """What the user added by name is theirs to hear, dislike or not: the loader never filters."""
    loader = _loader(userid=USER_ID)
    loader._managed_pool.fill = AsyncMock(return_value=[_track(DISLIKED), _track(LIKED)])

    await QueueLoaderMixin._fill_dynamic_tracks(loader, QUEUE_ID)

    assert _appended(loader) == [DISLIKED, LIKED]
    loader.mass.music.favorites.disliked_track_keys.assert_not_awaited()


async def test_the_autoplay_fill_skips_a_disliked_track() -> None:
    """Autoplay keeps the music going without the disliked track."""
    loader = _loader(userid=USER_ID)
    loader._autoplay.resolve_mode.return_value = AutoplayMode.LIBRARY
    loader._autoplay.get_library_tracks = AsyncMock(return_value=[_track(DISLIKED), _track(LIKED)])
    loader.mass.music.recency.snapshot = AsyncMock(return_value=MagicMock())

    with patch(f"{MODULE}.gate_tracks", side_effect=lambda tracks, *_args: tracks):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, QUEUE_ID)

    assert _appended(loader) == [LIKED]


async def test_the_similar_tracks_autoplay_skips_a_disliked_track() -> None:
    """Autoplay on similar tracks drops the disliked one before anything is queued."""
    loader = _loader(userid=USER_ID)
    loader._autoplay.resolve_mode.return_value = AutoplayMode.SIMILAR
    loader._get_similar_tracks = partial(QueueLoaderMixin._get_similar_tracks, loader)
    loader.mass.music.recency.snapshot = AsyncMock(return_value=MagicMock())
    loader.mass.get_provider = MagicMock(
        return_value=MagicMock(
            get_dynamic_tracks=AsyncMock(return_value=[_track(DISLIKED), _track(LIKED)])
        )
    )

    with (
        patch(f"{MODULE}.gate_tracks", side_effect=lambda tracks, *_args: tracks),
        patch(f"{MODULE}.playback_sources", AsyncMock(return_value=(None, None))),
    ):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, QUEUE_ID)

    assert _appended(loader) == [LIKED]
    # one lookup per top-up
    loader.mass.music.favorites.disliked_track_keys.assert_awaited_once()


async def test_an_anonymous_queue_is_not_filtered() -> None:
    """A queue nobody owns has no dislikes to apply, so nothing is looked up or dropped."""
    pool, queues = _pool()

    tracks = await pool._fetch_dynamic(MagicMock(), None)

    assert [x.item_id for x in tracks] == [DISLIKED, LIKED]
    queues.mass.music.favorites.disliked_track_keys.assert_not_awaited()
