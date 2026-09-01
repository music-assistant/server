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


async def test_first_call_starts_a_run_and_pages() -> None:
    """The first call snapshots the show and pages through it 20 tracks at a time."""
    media = _media_with_show(track_count=30)
    page1 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page1) == 20
    page2 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page2) == 10
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_concurrent_first_calls_start_only_one_run() -> None:
    """Two concurrent first-calls for the same station snapshot only once and page in turn."""
    media = _media_with_show(track_count=30)

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
    queue = SimpleNamespace(queue_id="q1", sources=[SimpleNamespace(uri="library://radio/7")])
    media.mass.player_queues.all = MagicMock(return_value=[queue])

    await media.get_dynamic_radio_tracks("morning_show")

    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_binds_to_a_queue_sourcing_the_provider_uri() -> None:
    """A run binds to a queue whose source names the show by its provider uri."""
    media = _media_with_show(track_count=3)
    queue = SimpleNamespace(
        queue_id="q1", sources=[SimpleNamespace(uri="ai_radio://radio/morning_show")]
    )
    media.mass.player_queues.all = MagicMock(return_value=[queue])

    await media.get_dynamic_radio_tracks("morning_show")

    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_end_allows_a_fresh_run() -> None:
    """Ending a run lets a later call start a fresh snapshot instead of staying exhausted."""
    media = _media_with_show(track_count=3)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3
    assert await media.get_dynamic_radio_tracks("morning_show") == []
    media._end_show_run("morning_show")
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3


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
