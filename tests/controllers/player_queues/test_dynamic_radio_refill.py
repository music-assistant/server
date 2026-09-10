"""Tests for endless-mix (dynamic radio) refills re-seeding from queue play history."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
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


async def test_fetch_dynamic_routes_radio_playlist_through_refill() -> None:
    """A Playlist served by the radio_playlist provider refills via get_dynamic_radio_refill_tracks."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    playlist = _playlist("seed-uri", "radio_playlist")
    queues.mass.get_provider.return_value = MagicMock(domain="radio_playlist")
    queues.get_dynamic_radio_refill_tracks = AsyncMock(return_value=[_track("a")])
    queues.get_dynamic_source_tracks = AsyncMock(return_value=[_track("b")])

    result = await pool._fetch_dynamic(QUEUE_ID, playlist)

    queues.get_dynamic_radio_refill_tracks.assert_awaited_once_with(QUEUE_ID, playlist)
    queues.get_dynamic_source_tracks.assert_not_called()
    assert [t.item_id for t in result] == ["a"]


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


async def test_get_dynamic_radio_refill_tracks_without_queue_returns_empty() -> None:
    """A queue removed while the refill was starting up yields no tracks."""
    ctrl, _mass = _controller(has_queue=False)
    ctrl._get_similar_tracks = AsyncMock()  # type: ignore[method-assign]

    result = await ctrl.get_dynamic_radio_refill_tracks(
        QUEUE_ID, _playlist("seed-uri", "radio_playlist")
    )

    assert result == []
    ctrl._get_similar_tracks.assert_not_awaited()
