"""Tests for the AI Radio media item surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.ai_radio.media import AIRadioMediaMixin

STATION = {
    "id": "morning_show",
    "name": "Morning Show",
    "source_playlist_id": "42",
    "source_playlist_provider": "library",
    "default_player_id": "",
    "max_duration_minutes": 0.0,
    "shuffle_source_tracks": True,
    "host_id": "amy",
}


class _Media(AIRadioMediaMixin):
    """Bare mixin harness."""

    def __init__(self, stations: dict[str, dict[str, Any]]) -> None:
        """Stamp the attrs AIRadioMediaMixin reads, skipping real provider init."""
        self._stations = stations
        self._show_runs: dict[str, Any] = {}
        self._show_runs_lock = asyncio.Lock()
        self._show_library_ids: dict[str, str] = {}
        self._hosts: dict[str, Any] = {}
        self.instance_id = "ai_radio"
        self.domain = "ai_radio"
        self.mass: MagicMock = MagicMock()
        self.logger = MagicMock()

    def _ai_radio_cover_image_path(self) -> str:
        """Return a fake cover image path."""
        return "/tmp/air.png"  # noqa: S108


async def test_get_radio_builds_dynamic_radio() -> None:
    """get_radio builds a dynamic Radio item with a unique provider mapping."""
    media = _Media({"morning_show": STATION})
    radio = await media.get_radio("morning_show")
    assert radio.item_id == "morning_show"
    assert radio.provider == "ai_radio"
    assert radio.is_dynamic is True
    assert radio.uri == "ai_radio://radio/morning_show"
    mapping = next(iter(radio.provider_mappings))
    assert mapping.is_unique is True


async def test_get_radio_unknown_station_raises() -> None:
    """get_radio raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_radio("nope")


async def test_library_upkeep_adds_missing_show() -> None:
    """_sync_show_library_items adds a show that has no library item yet."""
    media = _Media({"morning_show": STATION})
    radio_ctrl = media.mass.music.radio
    radio_ctrl.get_library_item_by_prov_mappings = AsyncMock(return_value=None)
    added = MagicMock(item_id="7", name="Morning Show")
    radio_ctrl.add_item_to_library = AsyncMock(return_value=added)

    async def _no_items() -> AsyncGenerator[Any]:
        return
        yield

    radio_ctrl.iter_library_items = MagicMock(return_value=_no_items())
    await media._sync_show_library_items()
    radio_ctrl.add_item_to_library.assert_awaited_once()
    # the sync also rebuilds the map that resolves a library row back to its station
    assert media._show_library_ids == {"7": "morning_show"}


def _track(item_id: str) -> Track:
    """Build a minimal Track with one provider mapping."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="library",
                provider_instance="library",
            )
        },
    )


def _media_with_show(track_count: int, max_duration_minutes: float = 0.0) -> _Media:
    """Build a _Media harness with a station whose source playlist has track_count tracks."""
    station = {**STATION, "max_duration_minutes": max_duration_minutes}
    media = _Media({"morning_show": station})
    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [
                {
                    "index": i,
                    "item_id": f"t{i}",
                    "duration": 210,
                    "media_item": _track(f"t{i}"),
                }
                for i in range(track_count)
            ],
            "My Playlist",
        )
    )
    media.mass.player_queues.all = MagicMock(return_value=[])
    return media


def _attach_show_queue(
    media: _Media,
    uri: str = "ai_radio://radio/morning_show",
    items: int = 0,
    current_index: int | None = None,
) -> SimpleNamespace:
    """Register a fake queue playing the show, in a consuming state by default."""
    queue = SimpleNamespace(
        queue_id="q1",
        sources=[SimpleNamespace(uri=uri)],
        items=items,
        current_index=current_index,
    )
    media.mass.player_queues.all = MagicMock(return_value=[queue])
    media.mass.player_queues.get = MagicMock(
        side_effect=lambda queue_id: queue if queue_id == queue.queue_id else None
    )
    return queue


async def test_first_call_starts_a_run_and_pages() -> None:
    """The first call for a playing queue starts a run and pages 20 tracks at a time."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)
    page1 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page1) == 20
    page2 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page2) == 10
    # the run is exhausted: the empty batch is what ends the show's feed
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_concurrent_first_calls_start_only_one_run() -> None:
    """Two concurrent first-calls for the same station snapshot only once and page in turn."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)

    async def _fetch_source_tracks(
        _station: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        # yields control so the second concurrent call can race the first
        await asyncio.sleep(0)
        return (
            [
                {"index": i, "item_id": f"t{i}", "duration": 210, "media_item": _track(f"t{i}")}
                for i in range(30)
            ],
            "My Playlist",
        )

    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=_fetch_source_tracks
    )
    page1, page2 = await asyncio.gather(
        media.get_dynamic_radio_tracks("morning_show"),
        media.get_dynamic_radio_tracks("morning_show"),
    )
    media._fetch_source_tracks.assert_awaited_once()
    assert {len(page1), len(page2)} == {20, 10}


async def test_run_binds_to_a_queue_sourcing_the_shows_library_uri() -> None:
    """A run binds to a queue whose source names the show by its library identity."""
    media = _media_with_show(track_count=3)
    media._show_library_ids = {"7": "morning_show"}
    _attach_show_queue(media, uri="library://radio/7")

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page) == 3
    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_binds_to_a_queue_sourcing_the_provider_uri() -> None:
    """A run binds to a queue whose source names the show by its provider uri."""
    media = _media_with_show(track_count=3)
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page) == 3
    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_end_allows_a_fresh_run() -> None:
    """Ending a run lets a later call start a fresh snapshot instead of staying exhausted."""
    media = _media_with_show(track_count=3)
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3
    assert await media.get_dynamic_radio_tracks("morning_show") == []
    media._end_show_run("morning_show")
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3


async def test_sample_without_a_playing_queue_is_stateless() -> None:
    """A details-view sample serves a preview without creating or consuming a run."""
    media = _media_with_show(track_count=30)

    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    assert media._show_runs == {}
    # a repeated sample snapshots afresh instead of paging through hidden state
    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    assert media._show_runs == {}
    assert media._fetch_source_tracks.await_count == 2  # type: ignore[attr-defined]


async def test_sample_during_a_live_show_leaves_the_run_untouched() -> None:
    """A details-view sample while the show plays must not move the run's cursor."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    run = media._show_runs["morning_show"]
    assert run.cursor == 20

    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20

    assert media._show_runs["morning_show"] is run
    assert run.cursor == 20


async def test_playback_after_a_sample_serves_the_full_show() -> None:
    """A sample before pressing play must not eat into the playback run's pages."""
    media = _media_with_show(track_count=30)
    # the user opens the show's details page first: a stateless sample
    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    # then presses play: the queue stores the show as source before the first feed call
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 10
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_consume_without_a_queue_serves_a_one_off_batch() -> None:
    """A consume call with no queue sourcing the show serves a batch but binds no run."""
    media = _media_with_show(track_count=30)

    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    assert media._show_runs == {}


async def test_duration_cap_trims_snapshot() -> None:
    """The run's snapshot is trimmed to the station's configured maximum duration."""
    media = _media_with_show(track_count=10, max_duration_minutes=7.0)
    page = await media.get_dynamic_radio_tracks("morning_show")
    # 10 tracks of 210s; the cap keeps tracks until >= 7 minutes (2 tracks)
    assert len(page) == 2


async def test_unknown_station_raises() -> None:
    """get_dynamic_radio_tracks raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_dynamic_radio_tracks("nope")
