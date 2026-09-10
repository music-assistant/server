"""Tests for endless-mix (dynamic radio) refills re-seeding from queue play history."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Playlist, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


def _track(item_id: str) -> Track:
    """Build a minimal playable Track."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _playlist(item_id: str, provider_domain: str) -> Playlist:
    """Build a Playlist served by the given provider domain."""
    return Playlist(
        item_id=item_id,
        provider=provider_domain,
        name="Mix",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_domain,
                is_unique=True,
            )
        },
    )


def _controller(*, has_queue: bool = True) -> tuple[PlayerQueuesController, MagicMock]:
    """Build a bare controller with a mocked mass, exposed for stubbing get_provider."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=MagicMock())} if has_queue else {}
    mass = MagicMock()
    ctrl.mass = mass
    return ctrl, mass


async def test_fetch_dynamic_first_fetch_uses_dynamic_source_tracks() -> None:
    """The first fetch for a not-yet-seeded radio_playlist source keeps the seed's own tracks."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("a")])
    queues.get_dynamic_radio_refill_tracks = AsyncMock()

    result = await pool._fetch_dynamic(QUEUE_ID, playlist)

    queues.get_dynamic_source_tracks.assert_awaited_once_with(playlist)
    queues.get_dynamic_radio_refill_tracks.assert_not_called()
    assert [t.item_id for t in result] == ["a"]


async def test_fetch_dynamic_second_fetch_routes_through_refill() -> None:
    """Once a radio_playlist source has delivered a batch, later refills re-seed from history."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("a")])
    queues.get_dynamic_radio_refill_tracks = AsyncMock(return_value=[_track("b")])

    await pool._fetch_dynamic(QUEUE_ID, playlist)
    result = await pool._fetch_dynamic(QUEUE_ID, playlist)

    queues.get_dynamic_radio_refill_tracks.assert_awaited_once_with(QUEUE_ID, playlist)
    assert [t.item_id for t in result] == ["b"]


async def test_fetch_dynamic_empty_first_fetch_does_not_mark_seeded() -> None:
    """An empty first batch does not seed the source, so the next fetch retries the same path."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[])
    queues.get_dynamic_radio_refill_tracks = AsyncMock()

    first = await pool._fetch_dynamic(QUEUE_ID, playlist)
    second = await pool._fetch_dynamic(QUEUE_ID, playlist)

    assert first == []
    assert second == []
    assert queues.get_dynamic_source_tracks.await_count == 2
    queues.get_dynamic_radio_refill_tracks.assert_not_called()


async def test_fetch_dynamic_routes_via_domain_lookup_not_provider_string() -> None:
    """A Playlist whose provider is a radio_playlist instance id still routes via domain lookup."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist--abc")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("a")])
    queues.get_dynamic_radio_refill_tracks = AsyncMock(return_value=[_track("b")])

    first = await pool._fetch_dynamic(QUEUE_ID, playlist)
    second = await pool._fetch_dynamic(QUEUE_ID, playlist)

    assert [t.item_id for t in first] == ["a"]
    assert [t.item_id for t in second] == ["b"]
    queues.get_dynamic_radio_refill_tracks.assert_awaited_once_with(QUEUE_ID, playlist)


async def test_fetch_dynamic_routes_other_sources_unchanged() -> None:
    """A dynamic source that is not a radio_playlist playlist still uses get_dynamic_source_tracks."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("p1", "some_provider")
    queues.mass.get_provider.return_value = MagicMock(domain="some_provider")
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("b")])
    queues.get_dynamic_radio_refill_tracks = AsyncMock()

    result = await pool._fetch_dynamic(QUEUE_ID, playlist)

    queues.get_dynamic_source_tracks.assert_awaited_once_with(playlist)
    queues.get_dynamic_radio_refill_tracks.assert_not_called()
    assert [t.item_id for t in result] == ["b"]


async def test_fetch_dynamic_logs_and_returns_empty_on_failure() -> None:
    """A fetch failure (e.g. a deleted endless-mix seed) is logged and yields no tracks."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    # seed the source first so the second fetch takes the refill path, where resolve_seed
    # (inside get_dynamic_radio_refill_tracks) is what actually raises in production
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("a")])
    await pool._fetch_dynamic(QUEUE_ID, playlist)
    queues.get_dynamic_radio_refill_tracks = AsyncMock(
        side_effect=MediaNotFoundError("Radio playlist seed not found")
    )

    result = await pool._fetch_dynamic(QUEUE_ID, playlist)

    assert result == []
    cast("MagicMock", pool.logger).warning.assert_called_once()


async def test_get_similar_tracks_single_track_seed_reseeds_from_queue_history() -> None:
    """
    A single-track seed reseeds from the queue's own play history, not the fixed seed.

    Pins the real `_get_similar_tracks` rotation logic without mocking it: a deterministic
    similar-track provider would otherwise regenerate the same batch on every refill.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    played_tracks = [_track(f"played-{i}") for i in range(5)]
    items = [
        QueueItem(
            queue_id=QUEUE_ID, queue_item_id=f"item-{i}", name=t.name, duration=60, media_item=t
        )
        for i, t in enumerate(played_tracks)
    ]
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=MagicMock(), items=items, userid=None)}
    radio_prov = MagicMock()
    radio_prov.get_dynamic_tracks = AsyncMock(return_value=[])
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider.return_value = radio_prov
    seed = _track("seed")  # not part of the queue's own history

    await ctrl._get_similar_tracks(QUEUE_ID, seed_items=[seed])

    args, kwargs = radio_prov.get_dynamic_tracks.call_args
    seeds = args[0]
    assert kwargs["include_base_tracks"] is False
    assert seeds != [seed]
    assert seeds
    assert all(s in played_tracks for s in seeds)


async def test_get_dynamic_radio_refill_tracks_resolves_seed_and_delegates() -> None:
    """The playlist's seed is resolved via the radio provider and passed to _get_similar_tracks."""
    ctrl, mass = _controller()
    playlist = _playlist("seed-uri", "radio_playlist")
    seed = _track("seed")
    radio_prov = MagicMock()
    radio_prov.resolve_seed = AsyncMock(return_value=seed)
    mass.get_provider.return_value = radio_prov
    ctrl._get_similar_tracks = AsyncMock(return_value=[_track("similar")])  # type: ignore[method-assign]

    result = await ctrl.get_dynamic_radio_refill_tracks(QUEUE_ID, playlist)

    mass.get_provider.assert_called_once_with("radio_playlist")
    radio_prov.resolve_seed.assert_awaited_once_with(playlist.item_id)
    ctrl._get_similar_tracks.assert_awaited_once_with(QUEUE_ID, seed_items=[seed])
    assert [t.item_id for t in result] == ["similar"]


async def test_get_dynamic_radio_refill_tracks_without_radio_provider_returns_empty() -> None:
    """No radio_playlist provider installed means no refill tracks."""
    ctrl, mass = _controller()
    mass.get_provider.return_value = None
    ctrl._get_similar_tracks = AsyncMock()  # type: ignore[method-assign]

    result = await ctrl.get_dynamic_radio_refill_tracks(
        QUEUE_ID, _playlist("seed-uri", "radio_playlist")
    )

    assert result == []
    ctrl._get_similar_tracks.assert_not_awaited()


async def test_get_dynamic_radio_refill_tracks_queue_removed_while_seed_resolves() -> None:
    """A queue removed while the seed resolves (the awaited step) yields no tracks."""
    ctrl, mass = _controller(has_queue=False)
    playlist = _playlist("seed-uri", "radio_playlist")
    radio_prov = MagicMock()
    radio_prov.resolve_seed = AsyncMock(return_value=_track("seed"))
    mass.get_provider.return_value = radio_prov
    ctrl._get_similar_tracks = AsyncMock()  # type: ignore[method-assign]

    result = await ctrl.get_dynamic_radio_refill_tracks(QUEUE_ID, playlist)

    assert result == []
    radio_prov.resolve_seed.assert_awaited_once_with(playlist.item_id)
    ctrl._get_similar_tracks.assert_not_awaited()


async def test_idle_recovery_routes_radio_playlist_refill_through_pool_path() -> None:
    """
    Idle-recovery refills a radio_playlist source through the pool's rotating refill path.

    Without this, `_handle_end_of_queue`'s settle-or-resume closure bypasses the pool and keeps
    re-seeding the same fixed seed (see `get_dynamic_radio_refill_tracks`).
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0)
    queue.state = PlaybackState.IDLE
    queue.next_item = None
    queue.current_index = 0
    queue.flow_mode = False
    playlist = _playlist("seed-uri", "radio_playlist")
    playlist.is_dynamic = True
    queue_data = PlayerQueueData(queue=queue, source_items=[playlist])
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl._finish_queue = Mock()  # type: ignore[method-assign]
    ctrl.get_dynamic_radio_refill_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    ctrl._media_resolver = MagicMock()
    ctrl._media_resolver.get_dynamic_source_tracks = AsyncMock(return_value=[])

    prev_state: CompareState = {
        "queue_id": QUEUE_ID,
        "state": PlaybackState.PLAYING,
        "current_item_id": "prev-id",
        "next_item_id": None,
        "current_item": None,
        "elapsed_time": 0,
        "last_playing_elapsed_time": 0,
        "stream_title": None,
        "codec_type": None,
        "output_player_ids": None,
    }
    new_state: CompareState = {**prev_state, "state": PlaybackState.IDLE}

    captured_tasks: list[Any] = []
    ctrl.mass.create_task = Mock(side_effect=captured_tasks.append)

    with patch(
        "music_assistant.controllers.player_queues.playback_tracker.asyncio.sleep",
        AsyncMock(),
    ):
        ctrl._handle_end_of_queue(queue, prev_state, new_state)
        assert captured_tasks
        await captured_tasks[0]

    ctrl.get_dynamic_radio_refill_tracks.assert_awaited_once_with(QUEUE_ID, playlist)
    ctrl._media_resolver.get_dynamic_source_tracks.assert_not_awaited()
